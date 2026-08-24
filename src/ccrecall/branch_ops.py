"""Branch metadata and message-link operations for session sync/import.

``upsert_branch`` maintains the session-keyed branches row; `diff_branch_messages`
diffs the branch_messages link table; ``sync_branch`` is the per-branch
coordinator that ties metadata, links, summary, and embedding together.
"""

import json
import logging
import sqlite3

from ccrecall.db_vec import fetch_branch_messages
from ccrecall.embed_ops import embed_branch_chunks, write_branch_summary
from ccrecall.embeddings import MODEL_TOKEN_LIMIT, SYNC_PATH_TOKEN_LIMIT
from ccrecall.hooks.subprocess_utils import reclaim_memory, try_load_libc
from ccrecall.models import LOGGER_NAME
from ccrecall.parsing import build_aggregated_content, compute_branch_metadata, extract_session_metadata

log = logging.getLogger(LOGGER_NAME)


def _to_json_or_none(value) -> str | None:
    """Serialize a non-empty value to JSON, or return None."""
    return json.dumps(value) if value else None


def update_branch_row(
    cursor: sqlite3.Cursor,
    branch_db_id: int,
    leaf_uuid: str,
    is_active: bool,
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
            leaf_uuid = ?,
            is_active = ?,
            started_at = ?,
            ended_at = ?,
            exchange_count = ?,
            files_modified = ?,
            commits = ?,
            tool_counts = ?
        WHERE id = ?
        """,
        (
            leaf_uuid,
            int(is_active),
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
            (session_id, leaf_uuid, is_active,
             started_at, ended_at, exchange_count, files_modified, commits, tool_counts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            leaf_uuid,
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


def upsert_branch(
    cursor: sqlite3.Cursor,
    branch: dict,
    branch_meta: dict,
    exchange_count: int,
    files_json: str | None,
    commits_json: str | None,
    tool_counts_json: str | None,
    session_id: int,
) -> int:
    """INSERT or UPDATE the branches row, keyed on session_id; return branch_db_id.

    Session-keyed identity: at most one row per session, looked up directly by
    session_id and updated in place on every sync — no leaf_uuid dict needed,
    and no separate step to deactivate other rows since there's only ever one.
    leaf_uuid is still written each sync as a diagnostic field (the latest
    message UUID) but is no longer part of the identity key.
    """
    leaf_uuid = branch["leaf_uuid"]
    is_active = branch["is_active"]

    cursor.execute(
        "SELECT id FROM branches WHERE session_id = ? AND is_active = 1",
        (session_id,),
    )
    row = cursor.fetchone()

    if row:
        branch_db_id = row[0]
        update_branch_row(
            cursor,
            branch_db_id,
            leaf_uuid,
            is_active,
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
            is_active,
            branch_meta,
            exchange_count,
            files_json,
            commits_json,
            tool_counts_json,
        )
    # branch_db_id is set on both paths above: row[0] (UPDATE) or lastrowid (INSERT,
    # non-None after a successful insert). Narrow for the type checker.
    assert branch_db_id is not None  # noqa: S101 — type-checker narrowing; set on both branches above

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
        placeholders = ",".join("?" * len(to_remove))
        cursor.execute(
            f"DELETE FROM branch_messages WHERE branch_id = ? AND message_id IN ({placeholders})",
            (branch_db_id, *to_remove),
        )
    if to_add:
        cursor.executemany(
            "INSERT OR IGNORE INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
            [(branch_db_id, mid) for mid in to_add],
        )


def sync_branch(
    cursor: sqlite3.Cursor,
    branch: dict,
    messages: list[dict],
    uuid_to_msg_id: dict[str, int],
    session_id: int,
    vec_writable: bool,
    sync_path_token_limit: int | None = None,
) -> None:
    """Process one branch: upsert metadata, diff links, compute summary, embed.

    ``sync_path_token_limit`` is the raw, unclamped ``sync_path_token_limit``
    setting from config (threaded through from ``sync_session``). Clamped here
    to ``[1, MODEL_TOKEN_LIMIT]`` before being passed to ``embed_branch_chunks``
    as ``cap_limit`` — this module already imports ``embeddings``, so the clamp
    lives here rather than in session_ops.py (which must stay free of
    fastembed/onnxruntime imports on the hook hot path) or config.py (which
    must stay free of heavy deps entirely). See design/specs/015.
    """
    libc = try_load_libc()

    branch_uuids = branch["uuids"]
    is_active = branch["is_active"]

    branch_msgs = [m for m in messages if m.get("uuid") in branch_uuids]
    branch_msgs.sort(key=lambda e: e.get("timestamp") or "")

    branch_meta = extract_session_metadata(branch_msgs)
    exchange_count, files, commits, tool_counts = compute_branch_metadata(branch_msgs)

    files_json = _to_json_or_none(files)
    commits_json = _to_json_or_none(commits)
    tool_counts_json = _to_json_or_none(tool_counts)

    branch_db_id = upsert_branch(
        cursor,
        branch,
        branch_meta,
        exchange_count,
        files_json,
        commits_json,
        tool_counts_json,
        session_id,
    )

    diff_branch_messages(cursor, branch_db_id, branch_uuids, uuid_to_msg_id)

    # Aggregate branch content for FTS — SET (recompute from scratch, not append)
    # Includes: message text + deduplicated full file paths + commit text
    agg_content = build_aggregated_content(cursor, branch_db_id, files, commits)
    cursor.execute(
        "UPDATE branches SET aggregated_content = ?, summary_version = NULL WHERE id = ?",
        (agg_content, branch_db_id),
    )
    reclaim_memory(libc)

    write_branch_summary(cursor, branch_db_id)
    reclaim_memory(libc)

    # fetch_branch_messages returns flat {role, content, timestamp, uuid} dicts — the
    # format build_exchange_pairs expects. branch_msgs (raw JSONL) is the right input
    # for metadata computation above but not for embedding.
    raw_cap = sync_path_token_limit
    if isinstance(raw_cap, bool) or not isinstance(raw_cap, int):
        raw_cap = SYNC_PATH_TOKEN_LIMIT
    cap = min(max(raw_cap, 1), MODEL_TOKEN_LIMIT)
    reclaim_memory(libc)
    try:
        embed_msgs = fetch_branch_messages(cursor, branch_db_id, include_notifications=False)
        embed_branch_chunks(cursor, branch_db_id, embed_msgs, is_active, vec_writable, cap_limit=cap)
    except Exception:
        # embed_branch_chunks raises on failure by design (see its docstring) and
        # leaves the embedding watermark stale, so the background backfill will
        # pick this branch back up — the suppression itself is intentional. But a
        # broad Exception catch here is still a genuine I/O/inference boundary
        # (DB read + embed_batch), so log it rather than let it vanish silently.
        log.exception(
            "embedding failed for branch; watermark left stale for backfill",
            extra={"branch_id": branch_db_id},
        )
