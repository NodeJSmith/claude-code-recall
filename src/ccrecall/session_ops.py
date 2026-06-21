"""
Shared session import logic for sync and import pipelines.

Both ``sync_current.py`` and ``import_conversations.py`` delegate to
``sync_session()`` here.  The two callers differ only in how they obtain their
input (stdin vs. directory scan) and in what they write to ``import_log``:

  - **sync path**: ``write_import_log=True, file_hash=None``  — signals "this
    session has been synced but not yet hashed" so the batch import knows to
    re-process and fill in the real hash.
  - **import path**: ``write_import_log=True, file_hash=<md5>``  — stores the
    real hash so identical re-runs are skipped.

A ``NULL``-hash import_log entry is treated as stale: when the import path sees
``file_hash is None`` (or a hash mismatch with the stored value), it processes
the file and updates the row.
"""

import contextlib
import json
import logging
import sqlite3
from pathlib import Path

from ccrecall.content import (
    extract_text_content,
    is_task_notification,
    is_teammate_message,
    is_tool_result,
    parse_origin,
)
from ccrecall.db import branch_vec_queryable, write_branch_embedding
from ccrecall.embeddings import embed_text
from ccrecall.formatting import normalize_project_key
from ccrecall.models import LOGGER_NAME
from ccrecall.parsing import (
    build_aggregated_content,
    compute_branch_metadata,
    extract_session_metadata,
    extract_session_uuid,
    find_all_branches,
    parse_all_with_uuids,
    parse_jsonl_file,
)
from ccrecall.project_ops import upsert_project
from ccrecall.summarizer import SUMMARY_VERSION, compute_context_summary


def import_log_skip_check(
    cursor: sqlite3.Cursor,
    filepath: Path,
    write_import_log: bool,
    file_hash: str | None,
) -> tuple[tuple | None, bool]:
    """Probe import_log for an existing row; return (log_row, should_skip).

    Returns (log_row, True) when write_import_log is set, file_hash is provided,
    and the stored hash is non-NULL and matches — caller should return -1.
    Returns (log_row, False) otherwise (NULL-hash stale or no row).
    Preserves the NULL-hash-stale asymmetry: a stored NULL is never a match.
    """
    if write_import_log:
        cursor.execute(
            "SELECT id, file_hash FROM import_log WHERE file_path = ?",
            (str(filepath),),
        )
        log_row = cursor.fetchone()
        if log_row and log_row[1] is not None and log_row[1] == file_hash:
            return log_row, True
        return log_row, False
    return None, False


def upsert_session(
    cursor: sqlite3.Cursor,
    session_uuid: str,
    project_id: int,
    meta: dict,
) -> int:
    """INSERT or UPDATE the sessions row; return the session id."""
    cursor.execute(
        """
        INSERT INTO sessions (uuid, project_id, git_branch, cwd)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET
            git_branch = COALESCE(excluded.git_branch, sessions.git_branch),
            cwd = COALESCE(excluded.cwd, sessions.cwd)
        """,
        (session_uuid, project_id, meta["git_branch"], meta["cwd"]),
    )
    cursor.execute("SELECT id FROM sessions WHERE uuid = ?", (session_uuid,))
    return cursor.fetchone()[0]


def build_message_row(
    entry: dict,
    session_id: int,
    valid_branch_uuids: set[str],
    existing_uuids: set[str],
) -> tuple | None:
    """Build the INSERT params for one message entry, or return None to skip.

    Skips: non-user/assistant types, tool-result user entries, missing/unclaimed
    UUIDs, empty extracted text, and already-inserted UUIDs.
    """
    entry_type = entry.get("type")
    if entry_type not in ("user", "assistant"):
        return None
    content = entry.get("message", {}).get("content", "")
    if entry_type == "user" and is_tool_result(content):
        return None
    uuid = entry.get("uuid")
    if not uuid or uuid not in valid_branch_uuids or uuid in existing_uuids:
        return None
    text, _has_tool_use, has_thinking, _tool_summary = extract_text_content(content)
    if not text:
        return None
    is_notification = entry_type == "user" and (is_task_notification(content) or is_teammate_message(content))
    return (
        session_id,
        uuid,
        entry.get("parentUuid"),
        entry.get("timestamp"),
        entry_type,
        text,
        has_thinking,
        int(is_notification),
        parse_origin(entry),
    )


def insert_new_messages(
    cursor: sqlite3.Cursor,
    session_id: int,
    messages: list[dict],
    valid_branch_uuids: set[str],
    existing_uuids: set[str],
) -> int:
    """Insert messages not yet in the DB; return the count of new rows.

    Adds each inserted UUID to existing_uuids so later entries in the same call
    dedup against rows written earlier in this loop.
    """
    new_count = 0
    for entry in messages:
        row = build_message_row(entry, session_id, valid_branch_uuids, existing_uuids)
        if row is None:
            continue
        cursor.execute(
            """
            INSERT INTO messages
                (session_id, uuid, parent_uuid, timestamp, role, content, has_thinking, is_notification, origin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, uuid) DO NOTHING
            """,
            row,
        )
        if cursor.rowcount > 0:
            new_count += 1
            existing_uuids.add(row[1])
    return new_count


def update_branch_row(
    cursor: sqlite3.Cursor,
    branch_db_id: int,
    is_active: bool,
    fork_point_uuid: str | None,
    branch_meta: dict,
    exchange_count: int,
    files_json: str | None,
    commits_json: str | None,
    tool_counts_json: str | None,
) -> None:
    """UPDATE an existing branches row in place."""
    cursor.execute(
        """
        UPDATE branches SET
            is_active = ?,
            fork_point_uuid = ?,
            started_at = ?,
            ended_at = ?,
            exchange_count = ?,
            files_modified = ?,
            commits = ?,
            tool_counts = ?
        WHERE id = ?
        """,
        (
            int(is_active),
            fork_point_uuid,
            branch_meta["started_at"],
            branch_meta["ended_at"],
            exchange_count,
            files_json,
            commits_json,
            tool_counts_json,
            branch_db_id,
        ),
    )


def insert_branch_row(
    cursor: sqlite3.Cursor,
    session_id: int,
    leaf_uuid: str,
    fork_point_uuid: str | None,
    is_active: bool,
    branch_meta: dict,
    exchange_count: int,
    files_json: str | None,
    commits_json: str | None,
    tool_counts_json: str | None,
) -> int | None:
    """INSERT a new branches row; return lastrowid (None only if the INSERT did not run)."""
    cursor.execute(
        """
        INSERT INTO branches
            (session_id, leaf_uuid, fork_point_uuid, is_active,
             started_at, ended_at, exchange_count, files_modified, commits, tool_counts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            leaf_uuid,
            fork_point_uuid,
            int(is_active),
            branch_meta["started_at"],
            branch_meta["ended_at"],
            exchange_count,
            files_json,
            commits_json,
            tool_counts_json,
        ),
    )
    return cursor.lastrowid


def enforce_single_active_branch(cursor: sqlite3.Cursor, session_id: int, branch_db_id: int) -> None:
    """Deactivate every other branch in the session so only branch_db_id stays active."""
    cursor.execute(
        """
        UPDATE branches SET is_active = 0
        WHERE session_id = ? AND id != ? AND is_active = 1
        """,
        (session_id, branch_db_id),
    )


def upsert_branch(
    cursor: sqlite3.Cursor,
    branch: dict,
    branch_meta: dict,
    exchange_count: int,
    files_json: str | None,
    commits_json: str | None,
    tool_counts_json: str | None,
    session_id: int,
    existing_branches: dict[str, int],
) -> int:
    """INSERT or UPDATE the branches row; enforce single-active-branch; return branch_db_id."""
    leaf_uuid = branch["leaf_uuid"]
    fork_point_uuid = branch.get("fork_point_uuid")
    is_active = branch["is_active"]

    if leaf_uuid in existing_branches:
        branch_db_id = existing_branches[leaf_uuid]
        update_branch_row(
            cursor,
            branch_db_id,
            is_active,
            fork_point_uuid,
            branch_meta,
            exchange_count,
            files_json,
            commits_json,
            tool_counts_json,
        )
    else:
        branch_db_id = insert_branch_row(
            cursor,
            session_id,
            leaf_uuid,
            fork_point_uuid,
            is_active,
            branch_meta,
            exchange_count,
            files_json,
            commits_json,
            tool_counts_json,
        )
    # branch_db_id is set on both paths above: existing_branches[leaf_uuid] (UPDATE) or
    # lastrowid (INSERT, non-None after a successful insert). Narrow for the type checker.
    assert branch_db_id is not None  # noqa: S101 — type-checker narrowing; set on both branches above

    if is_active:
        enforce_single_active_branch(cursor, session_id, branch_db_id)

    return branch_db_id


def diff_branch_messages(
    cursor: sqlite3.Cursor,
    branch_db_id: int,
    branch_uuids: list[str],
    uuid_to_msg_id: dict[str, int],
) -> None:
    """Diff branch_messages links: add missing, remove stale (not messages themselves)."""
    cursor.execute(
        "SELECT message_id FROM branch_messages WHERE branch_id = ?",
        (branch_db_id,),
    )
    existing_bm_ids = {row[0] for row in cursor.fetchall()}

    desired_bm_ids: set[int] = set()
    for uuid in branch_uuids:
        msg_id = uuid_to_msg_id.get(uuid)
        if msg_id:
            desired_bm_ids.add(msg_id)

    to_add = desired_bm_ids - existing_bm_ids
    to_remove = existing_bm_ids - desired_bm_ids

    if to_remove:
        ph = ",".join("?" * len(to_remove))
        cursor.execute(
            f"DELETE FROM branch_messages WHERE branch_id = ? AND message_id IN ({ph})",
            (branch_db_id, *to_remove),
        )
    if to_add:
        cursor.executemany(
            "INSERT OR IGNORE INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
            [(branch_db_id, mid) for mid in to_add],
        )


def write_branch_summary(cursor: sqlite3.Cursor, branch_db_id: int) -> str | None:
    """Compute and store context summary for a branch; return summary_md or None.

    Classifies failures three ways — moved wholesale from sync_session:
    - (ValueError, TypeError, KeyError): content error — skip without logging.
    - sqlite3.Error: infra error — log and skip.
    - Any other exception: propagates (genuine bug, not masked).
    """
    summary_md = None
    try:
        summary_md, summary_json = compute_context_summary(cursor, branch_db_id)
        cursor.execute(
            """
            UPDATE branches SET context_summary = ?, context_summary_json = ?, summary_version = ?
            WHERE id = ?
            """,
            (summary_md, summary_json, SUMMARY_VERSION, branch_db_id),
        )
    except (ValueError, TypeError, KeyError):
        # Content error (malformed summary data) — same classification as
        # backfill_summaries: skip this branch's summary without failing the
        # sync/import. A real bug (e.g. AttributeError) still propagates.
        summary_md = None
    except sqlite3.Error:
        # Infra error (locked/failed DB write): log and skip the summary
        # rather than aborting the whole import (this runs per branch with no
        # outer handler in the import loop). The branch stays eligible for
        # backfill, and the failure is observable in the log instead of being
        # silently swallowed.
        logging.getLogger(LOGGER_NAME).exception("sync: summary write failed for branch %s", branch_db_id)
        summary_md = None
    return summary_md


def embed_branch(
    cursor: sqlite3.Cursor,
    branch_db_id: int,
    summary_md: str | None,
    is_active: bool,
    vec_writable: bool,
) -> None:
    """Embed-on-write for an active leaf with a successful summary and queryable vec table.

    Order is load-bearing: vec0 upsert FIRST, version columns LAST.
    If the upsert raises and is swallowed, version columns stay at 0
    so the branch remains eligible for backfill (no "version done, no vector").
    """
    if not (summary_md and is_active and vec_writable):
        return
    # Deliberately broad: embed_text wraps a third-party model stack
    # (fastembed/onnxruntime) whose failure modes aren't a fixed type, and
    # embedding is non-essential here — a failure just leaves the branch
    # eligible for backfill rather than failing the sync/import.
    with contextlib.suppress(Exception):
        vec = embed_text(summary_md)
        write_branch_embedding(cursor, branch_db_id, vec, SUMMARY_VERSION)


def sync_branch(
    cursor: sqlite3.Cursor,
    branch: dict,
    messages: list[dict],
    uuid_to_msg_id: dict[str, int],
    existing_branches: dict[str, int],
    session_id: int,
    vec_writable: bool,
) -> None:
    """Process one branch: upsert metadata, diff links, compute summary, embed."""
    branch_uuids = branch["uuids"]
    is_active = branch["is_active"]

    branch_msgs = [m for m in messages if m.get("uuid") in branch_uuids]
    branch_msgs.sort(key=lambda e: e.get("timestamp") or "")

    branch_meta = extract_session_metadata(branch_msgs)
    exchange_count, files, commits, tool_counts = compute_branch_metadata(branch_msgs)

    files_json = json.dumps(files) if files else None
    commits_json = json.dumps(commits) if commits else None
    tool_counts_json = json.dumps(tool_counts) if tool_counts else None

    branch_db_id = upsert_branch(
        cursor,
        branch,
        branch_meta,
        exchange_count,
        files_json,
        commits_json,
        tool_counts_json,
        session_id,
        existing_branches,
    )

    diff_branch_messages(cursor, branch_db_id, branch_uuids, uuid_to_msg_id)

    # Aggregate branch content for FTS — SET (recompute from scratch, not append)
    # Includes: message text + deduplicated full file paths + commit text
    agg_content = build_aggregated_content(cursor, branch_db_id, files, commits)
    cursor.execute(
        "UPDATE branches SET aggregated_content = ? WHERE id = ?",
        (agg_content, branch_db_id),
    )

    summary_md = write_branch_summary(cursor, branch_db_id)
    embed_branch(cursor, branch_db_id, summary_md, is_active, vec_writable)


def upsert_import_log(
    cursor: sqlite3.Cursor,
    filepath: Path,
    session_id: int,
    file_hash: str | None,
    log_row: tuple | None,
) -> None:
    """UPDATE or INSERT the import_log row for this file."""
    cursor.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,))
    total_messages = cursor.fetchone()[0]

    if log_row:
        cursor.execute(
            """
            UPDATE import_log
            SET file_hash = ?, imported_at = CURRENT_TIMESTAMP, messages_imported = ?
            WHERE file_path = ?
            """,
            (file_hash, total_messages, str(filepath)),
        )
    else:
        cursor.execute(
            "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, ?, ?)",
            (str(filepath), file_hash, total_messages),
        )


def sync_session(
    conn: sqlite3.Connection,
    filepath: Path,
    project_dir: Path,
    write_import_log: bool = False,
    file_hash: str | None = None,
    _project_id: int | None = None,
) -> int:
    """Import a single JSONL session file, returning the count of new messages inserted (or -1 if skipped).

    Handles session upsert, message insertion with UUID dedup (without
    ``has_tool_use`` / ``tool_summary`` in the INSERT), branch detection via
    ``find_all_branches``, branch metadata computation, branch_messages diff
    (add/remove), aggregated content assembly, and context summary computation.

    ``write_import_log`` controls the ``import_log`` row, and ``file_hash``
    drives its dedup: when ``write_import_log`` is set and ``file_hash`` matches
    an existing *non-NULL* hash, the file is unchanged and the function returns
    -1; a stored ``NULL`` hash (a sync-written placeholder) with a provided
    ``file_hash`` is treated as stale and re-processed. ``_project_id``, when
    provided by the import_conversations.py adapter, is used directly so the
    project upsert step is skipped and no second DB lookup runs.
    """
    cursor = conn.cursor()

    log_row, should_skip = import_log_skip_check(cursor, filepath, write_import_log, file_hash)
    if should_skip:
        return -1

    # Parse the JSONL
    all_entries = list(parse_all_with_uuids(filepath))
    if not all_entries:
        return 0

    branches = find_all_branches(all_entries)
    if not branches:
        return 0

    messages = list(parse_jsonl_file(filepath))
    if not messages:
        return 0

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

    # Get existing branch leaf_uuids for this session
    cursor.execute("SELECT id, leaf_uuid FROM branches WHERE session_id = ?", (session_id,))
    existing_branches = {row[1]: row[0] for row in cursor.fetchall()}

    # Probe vec persistence once: if sqlite-vec didn't load, branch_vec doesn't
    # exist and write_branch_embedding would raise. Skip embed-on-write entirely
    # in that case rather than paying for embed_text inference on every active
    # leaf just to have the write swallowed.
    vec_writable = branch_vec_queryable(conn)

    for branch in branches:
        sync_branch(cursor, branch, messages, uuid_to_msg_id, existing_branches, session_id, vec_writable)

    if write_import_log:
        upsert_import_log(cursor, filepath, session_id, file_hash, log_row)

    return new_count
