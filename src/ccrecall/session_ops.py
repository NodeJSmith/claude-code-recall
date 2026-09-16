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
from collections.abc import Mapping
from pathlib import Path

from ccrecall.branch_ops import sync_branch
from ccrecall.db_vec import chunk_vec_queryable
from ccrecall.formatting import normalize_project_key
from ccrecall.import_log_ops import import_log_skip_check, pending_tool_content_uuids, upsert_import_log
from ccrecall.message_ops import insert_new_messages, update_missing_tool_content, upsert_session
from ccrecall.models import LOGGER_NAME
from ccrecall.parsing import extract_session_metadata, extract_session_uuid, find_all_branches, parse_all_with_uuids
from ccrecall.project_ops import upsert_project

log = logging.getLogger(LOGGER_NAME)


def _extract_branches_and_messages(all_entries: list[dict]) -> tuple[list[dict], list[dict], dict] | None:
    """Compute branches, insertable messages, and session metadata from one
    session's already-parsed entries.

    Split out from the DB-writing half (``_sync_branches_and_messages``) so
    callers can drop their reference to ``all_entries`` before entering the
    branch/message sync — see that function's docstring for why the ordering
    matters. Returns ``None`` when there is nothing to sync (no branches, or
    no insertable messages) so the caller can skip straight to its
    empty-session bookkeeping.
    """
    branches = find_all_branches(all_entries)
    if not branches:
        return None

    messages = [
        e
        for e in all_entries
        if e.get("type") in ("user", "assistant") and not (e.get("isMeta") and not e.get("origin"))
    ]
    if not messages:
        return None

    meta = extract_session_metadata(all_entries)
    return branches, messages, meta


def _sync_branches_and_messages(
    conn: sqlite3.Connection,
    session_uuid: str,
    meta: dict,
    branches: list[dict],
    messages: list[dict],
    project_dir: Path,
    _project_id: int | None,
    embed: bool,
    settings: dict | None,
) -> tuple[int, int, int]:
    """Core branch/message sync given already-extracted branches/messages/meta.

    Shared by ``sync_session`` (single file) and ``sync_session_group``
    (multi-file repair candidates) so the ``branch_messages`` diff is defined
    exactly once, over whichever set of entries the caller assembled — the
    union of every file in a multi-file candidate for ``sync_session_group``,
    or one file's entries for ``sync_session``. See design/specs/016-stale
    -tail-import-repair Finding 1: a multi-file candidate's
    ``branch_messages`` diff must see every file's entries in a single pass,
    never one file at a time, or a message linked only via a sibling file
    gets silently unlinked.

    Takes pre-extracted ``branches``/``messages``/``meta`` (via
    ``_extract_branches_and_messages``) rather than raw ``all_entries`` so
    each caller can drop its reference to the full parsed transcript before
    calling in — ``messages`` shares dict references with the entries that
    matter, but the much larger raw entry list (tool results, notifications,
    etc.) becomes collectible before this function's branch loop runs, not
    after it returns.

    Returns ``(session_id, new_count, repaired_tool_content)``.
    """
    cursor = conn.cursor()

    # Project upsert (skip when caller pre-resolved project_id)
    if _project_id is not None:
        project_id = _project_id
    else:
        project_key = normalize_project_key(project_dir.name)
        project_id, _ = upsert_project(cursor, project_key, cwd=meta.get("cwd"))

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

    # The repair loop is only needed for pre-v4 rows whose tool_content is still
    # NULL. Query the pending set first (cheap — hits idx_messages_tool_content_null)
    # so the steady state (every session created post-v4) skips the per-message
    # extract_text_content + UPDATE work entirely instead of paying for it, at
    # rowcount 0, on every sync.
    pending_uuids = pending_tool_content_uuids(cursor, session_uuid)
    if pending_uuids:
        pending_messages = [e for e in messages if e.get("uuid") in pending_uuids]
        repaired_tool_content = update_missing_tool_content(cursor, session_id, pending_messages, existing_uuids)
    else:
        repaired_tool_content = 0
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
    sync_path_token_limit = settings.get("sync_path_token_limit") if settings else None

    for branch in branches:
        sync_branch(
            cursor,
            branch,
            messages,
            uuid_to_msg_id,
            session_id,
            vec_writable,
            sync_path_token_limit=sync_path_token_limit,
        )

    return session_id, new_count, repaired_tool_content


def sync_session(
    conn: sqlite3.Connection,
    filepath: Path,
    project_dir: Path,
    file_hash: str | None = None,
    _project_id: int | None = None,
    embed: bool = True,
    file_size: int | None = None,
    file_mtime: float | None = None,
    settings: dict | None = None,
    *,
    force: bool = False,
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
    ``force=True`` bypasses the import_log hash-match skip and reprocesses the
    transcript.
    ``_project_id``, when provided by the import_conversations.py adapter, is
    used directly so the project upsert step is skipped and no second DB lookup
    runs. ``embed`` controls whether chunk embeddings are written — the import
    path passes False to load vec (for trigger support) without paying for
    inference.
    ``settings``, when provided, supplies the raw ``sync_path_token_limit``
    config value threaded to ``sync_branch``. This module must not import from
    ``embeddings.py`` (the hook hot path — see architecture invariant 2), so
    the value is passed through unclamped; ``branch_ops.sync_branch`` (which
    already imports ``embeddings``) does the clamping.

    This function only ever sees one file's entries. For a multi-file
    candidate (a parent session plus its ``agent-*.jsonl`` subagent
    transcripts) whose ``branch_messages`` links must be diffed against the
    union of every file at once, use ``sync_session_group`` instead — see its
    docstring and design/specs/016-stale-tail-import-repair Finding 1.
    """
    cursor = conn.cursor()

    log_row, should_skip = import_log_skip_check(cursor, filepath, file_hash, force=force)
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

    session_uuid = extract_session_uuid(filepath)
    extracted = _extract_branches_and_messages(all_entries)

    # all_entries is no longer needed past this point — branches/messages/meta
    # have been extracted. Free the list container and any entries not
    # carried forward in `messages` (e.g. notifications) *before* the
    # branch/message sync below, not after — messages shares dict references
    # with all_entries's user/assistant entries, so this frees only the list
    # container and the non-message entries, not the bulk of the transcript
    # data, but it must happen before sync_branch's per-branch work runs.
    del all_entries

    if extracted is None:
        return record_and_return_empty()
    branches, messages, meta = extracted

    session_id, new_count, repaired_tool_content = _sync_branches_and_messages(
        conn, session_uuid, meta, branches, messages, project_dir, _project_id, embed, settings
    )

    upsert_import_log(cursor, filepath, session_id, file_hash, log_row, file_size, file_mtime)

    log.debug("sync_session %s: new_count=%d repaired_tool_content=%d", filepath.name, new_count, repaired_tool_content)
    return new_count


def sync_session_group(
    conn: sqlite3.Connection,
    filepaths: list[Path],
    project_dir: Path,
    *,
    file_hashes: Mapping[Path, str | None],
    file_stats: Mapping[Path, tuple[int | None, float | None]],
    _project_id: int | None = None,
    embed: bool = True,
    settings: dict | None = None,
) -> int:
    """Force-reimport a multi-file session transcript as one merged unit.

    Parses and syncs the union of every file's entries in a single
    ``_sync_branches_and_messages`` pass, so the ``branch_messages`` diff sees
    the whole candidate at once. Calling ``sync_session`` once per file instead
    (the pre-fix behavior) computes branch membership from each file's
    entries in isolation, so a later file's diff drops links that only
    exist via an earlier sibling file — see design/specs/016-stale-tail
    -import-repair Finding 1.

    Unlike ``sync_session``, this always force-processes: there is no
    import_log skip-check, matching the ``force=True`` repair use case this
    exists for (``hooks/import_repair.py``'s ``repair_sessions``). Writes
    one ``import_log`` row per file afterward — each with its own
    ``file_hash``/``file_size``/``file_mtime`` — via ``file_hashes`` and
    ``file_stats`` (keyed by path, computed by the caller since hashing is
    an I/O-bound operation this module shouldn't duplicate the hashing
    helper for) so a later ordinary (non-force) import of any of these files
    skips correctly instead of re-parsing every run.

    Returns the count of newly inserted messages (session-wide — a
    multi-file candidate has no meaningful per-file split once merged).
    """
    cursor = conn.cursor()

    all_entries: list[dict] = []
    for fp in filepaths:
        all_entries.extend(parse_all_with_uuids(fp))

    def write_import_logs(session_id: int) -> None:
        for fp in filepaths:
            log_row = cursor.execute("SELECT id, file_hash FROM import_log WHERE file_path = ?", (str(fp),)).fetchone()
            size, mtime = file_stats[fp]
            upsert_import_log(cursor, fp, session_id, file_hashes[fp], log_row, size, mtime)

    if not all_entries:
        write_import_logs(0)
        return 0

    session_uuid = extract_session_uuid(filepaths[0])
    extracted = _extract_branches_and_messages(all_entries)
    del all_entries

    if extracted is None:
        write_import_logs(0)
        return 0
    branches, messages, meta = extracted

    session_id, new_count, repaired_tool_content = _sync_branches_and_messages(
        conn, session_uuid, meta, branches, messages, project_dir, _project_id, embed, settings
    )

    write_import_logs(session_id)

    log.debug(
        "sync_session_group %s: new_count=%d repaired_tool_content=%d",
        [fp.name for fp in filepaths],
        new_count,
        repaired_tool_content,
    )
    return new_count
