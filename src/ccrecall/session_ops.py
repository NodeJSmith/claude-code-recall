"""Shared session import logic for sync and import pipelines.

Both ``sync_current.py`` and ``import_conversations.py`` delegate to
``sync_session()`` here.  The two callers differ only in how they obtain their
input (stdin vs. directory scan) and in what they write to ``import_log``:

  - **sync path**: ``file_hash=None``  — signals "this session has been synced
    but not yet hashed" so the batch import knows to re-process and fill in the
    real hash.
  - **import path**: ``file_hash=<md5>``  — stores the real hash so identical
    re-runs are skipped.

A ``NULL``-hash import_log entry is treated as stale: when the import path sees
``file_hash is None`` (or a hash mismatch with the stored value), it processes
the file and updates the row.

``sync_session`` is the top-level orchestrator; the per-concern work it
delegates to lives in ``import_log_ops.py`` (import_log bookkeeping),
``message_ops.py`` (session/message rows), ``branch_ops.py`` (branch metadata,
links, and the per-branch coordinator), and ``embed_ops.py`` (summary +
chunk-embedding).
"""

import logging
import sqlite3
from pathlib import Path

from ccrecall.branch_ops import sync_branch
from ccrecall.db import chunk_vec_queryable
from ccrecall.formatting import normalize_project_key
from ccrecall.import_log_ops import import_log_skip_check, upsert_import_log
from ccrecall.message_ops import insert_new_messages, upsert_session
from ccrecall.models import LOGGER_NAME
from ccrecall.parsing import extract_session_metadata, extract_session_uuid, find_all_branches, parse_all_with_uuids
from ccrecall.project_ops import upsert_project

log = logging.getLogger(LOGGER_NAME)


def sync_session(
    conn: sqlite3.Connection,
    filepath: Path,
    project_dir: Path,
    file_hash: str | None = None,
    _project_id: int | None = None,
    embed: bool = True,
    file_size: int | None = None,
    file_mtime: float | None = None,
) -> int:
    """Import a single JSONL session file, returning the count of new messages inserted (or -1 if skipped).

    Handles session upsert, message insertion with UUID dedup (without
    ``has_tool_use`` / ``tool_summary`` in the INSERT), branch detection via
    ``find_all_branches``, branch metadata computation, branch_messages diff
    (add/remove), aggregated content assembly, and context summary computation.

    Always writes an ``import_log`` row. ``file_hash`` drives dedup: when it
    matches an existing *non-NULL* hash, the file is unchanged and the function
    returns -1; a stored ``NULL`` hash (a sync-written placeholder) with a
    provided ``file_hash`` is treated as stale and re-processed.
    ``_project_id``, when provided by the import_conversations.py adapter, is
    used directly so the project upsert step is skipped and no second DB lookup
    runs. ``embed`` controls whether chunk embeddings are written — the import
    path passes False to load vec (for trigger support) without paying for
    inference.
    """
    cursor = conn.cursor()

    log_row, should_skip = import_log_skip_check(cursor, filepath, file_hash)
    if should_skip:
        log.debug("sync_session skip %s (import_log hash match)", filepath.name)
        return -1

    def record_and_return_empty() -> int:
        upsert_import_log(cursor, filepath, 0, file_hash, log_row, file_size, file_mtime)
        return 0

    # Parse the JSONL — single pass; derive messages by filtering to user/assistant.
    all_entries = list(parse_all_with_uuids(filepath))
    if not all_entries:
        return record_and_return_empty()

    branches = find_all_branches(all_entries)
    if not branches:
        return record_and_return_empty()

    messages = [
        e
        for e in all_entries
        if e.get("type") in ("user", "assistant") and not (e.get("isMeta") and not e.get("origin"))
    ]
    if not messages:
        return record_and_return_empty()

    # Extract session-level metadata
    meta = extract_session_metadata(all_entries)
    session_uuid = extract_session_uuid(filepath)

    # Project upsert (skip when caller pre-resolved project_id)
    if _project_id is not None:
        project_id = _project_id
    else:
        project_key = normalize_project_key(project_dir.name)
        project_id = upsert_project(cursor, project_key, cwd=meta.get("cwd"))

    session_id = upsert_session(cursor, session_uuid, project_id, meta)

    # Build set of UUIDs claimed by any branch
    valid_branch_uuids: set[str] = set()
    for branch in branches:
        valid_branch_uuids.update(branch["uuids"])

    # Message insertion with UUID dedup
    cursor.execute(
        "SELECT uuid FROM messages WHERE session_id = ? AND uuid IS NOT NULL",
        (session_id,),
    )
    existing_uuids = {row[0] for row in cursor.fetchall()}

    new_count = insert_new_messages(cursor, session_id, messages, valid_branch_uuids, existing_uuids)

    # Build uuid -> message_id mapping
    cursor.execute(
        "SELECT id, uuid FROM messages WHERE session_id = ? AND uuid IS NOT NULL",
        (session_id,),
    )
    uuid_to_msg_id = {row[1]: row[0] for row in cursor.fetchall()}

    # Probe vec persistence once: if sqlite-vec didn't load, chunk_vec doesn't
    # exist and embed_branch_chunks would raise. Skip embed-on-write entirely
    # in that case rather than paying for embed_text inference on every active
    # leaf just to have the write swallowed. The import path passes embed=False
    # to load vec (for trigger support) without paying for inference.
    vec_writable = embed and chunk_vec_queryable(conn)

    for branch in branches:
        sync_branch(cursor, branch, messages, uuid_to_msg_id, session_id, vec_writable)

    log.debug(
        "sync_session writing import_log for %s: hash=%s size=%s mtime=%s log_row=%s",
        filepath.name,
        file_hash,
        file_size,
        file_mtime,
        log_row,
    )
    upsert_import_log(cursor, filepath, session_id, file_hash, log_row, file_size, file_mtime)

    log.debug("sync_session %s: new_count=%d", filepath.name, new_count)
    return new_count
