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
import hashlib
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
from ccrecall.db import chunk_vec_queryable, fetch_branch_messages, write_chunk_embedding
from ccrecall.embeddings import EMBEDDING_MODEL, EMBEDDING_VERSION, cap_for_embedding, embed_text
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
from ccrecall.summarizer import SUMMARY_VERSION, build_exchange_pairs, compute_context_summary


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


# Maximum number of exchanges embedded per sync on the write path. Version-stale
# chunks (those only needing an EMBEDDING_VERSION bump) are deliberately left to
# the background backfill — only new or content-changed exchanges are eligible
# here. This cap bounds the detached sync-current process's worst case even for a
# first-sync of a long imported session or a rewind with many fresh exchanges.
MAX_WRITE_PATH_EMBEDS_PER_SYNC = 8


def embed_branch_chunks(
    cursor: sqlite3.Cursor,
    branch_db_id: int,
    branch_msgs: list[dict],
    is_active: bool,
    vec_writable: bool,
    max_embeds: int | None = MAX_WRITE_PATH_EMBEDS_PER_SYNC,
) -> int:
    """Embed per-exchange chunks for an active-leaf branch (incremental write path).

    Implements the clear-first/set-last watermark protocol:
    - If any exchanges need embedding (new or content-changed), the branch
      watermark is cleared to 0 BEFORE the embed loop (step 5a), then set to
      EMBEDDING_VERSION only after every exchange has a current-version chunk
      (step 8).
    - Version-stale chunks are deliberately left to the background backfill;
      this path embeds only new or content-changed exchanges.

    ``max_embeds`` bounds how many exchanges this call embeds. It defaults to
    MAX_WRITE_PATH_EMBEDS_PER_SYNC so the detached Stop-sync write path stays
    bounded even right after an EMBEDDING_VERSION bump. The off-hot-path backfill
    passes ``max_embeds=None`` (no cap) so a single call fully embeds a branch of
    any length — otherwise a branch with more exchanges than the cap would stay
    eligible and trip the backfill's no-progress guard.

    Returns the number of exchanges embedded by this call (the inference count) —
    the backfill uses it for accurate progress/ETA without recomputing exchanges.

    Raises on failure — callers (sync_branch) must wrap in
    contextlib.suppress(Exception). Does not commit; the single commit at
    sync_current.py:137 owns the transaction.
    """
    if not (is_active and vec_writable and branch_msgs):
        return 0

    exchanges = build_exchange_pairs(branch_msgs)
    if not exchanges:
        return 0

    # Step 3 — compute embedded text, content hash, and bounded display text per exchange.
    # Display columns use the same head+tail cap per turn so the shown excerpt aligns
    # with the embedded region (design.md challenge M14).
    exchange_data = []
    for ex in exchanges:
        user = ex.get("user") or ""
        assistant = ex.get("assistant") or ""
        combined = f"{user}\n\n{assistant}"
        text, was_capped = cap_for_embedding(combined)
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        user_text, _ = cap_for_embedding(user)
        assistant_text, _ = cap_for_embedding(assistant)
        exchange_data.append(
            {
                "index": ex["index"],
                "text": text,
                "was_capped": was_capped,
                "content_hash": content_hash,
                "timestamp": ex.get("timestamp"),
                "first_message_uuid": ex.get("first_message_uuid"),
                "user_text": user_text,
                "assistant_text": assistant_text,
            }
        )

    # Load existing chunk rows for this branch.
    cursor.execute(
        "SELECT exchange_index, content_hash, embedding_version, embedding_model FROM chunks WHERE branch_id = ?",
        (branch_db_id,),
    )
    existing_chunks: dict[int, dict] = {
        row[0]: {"content_hash": row[1], "embedding_version": row[2], "embedding_model": row[3]}
        for row in cursor.fetchall()
    }

    # Step 5 — diff: eligible = no chunk row OR content_hash changed.
    # Version-stale (embedding_version < EMBEDDING_VERSION) but content-unchanged
    # chunks are deliberately excluded — those are backfill's job (design H6).
    current_indices = {ed["index"] for ed in exchange_data}
    needing_embed_full = [
        ed
        for ed in exchange_data
        if ed["index"] not in existing_chunks or existing_chunks[ed["index"]]["content_hash"] != ed["content_hash"]
    ]
    indices_to_prune = set(existing_chunks) - current_indices

    # Early return: nothing to embed and nothing to prune
    if not needing_embed_full and not indices_to_prune:
        # Idempotent watermark repair: set to EMBEDDING_VERSION iff every existing
        # chunk is already version-current (repairs a prior failed step 8).
        if exchange_data and all(
            existing_chunks.get(ed["index"], {}).get("embedding_version") == EMBEDDING_VERSION for ed in exchange_data
        ):
            cursor.execute(
                "UPDATE branches SET embedding_version = ?, embedding_model = ? WHERE id = ?",
                (EMBEDDING_VERSION, EMBEDDING_MODEL, branch_db_id),
            )
        return 0

    # Step 5a — clear-first: if any exchange needs embedding, clear the watermark
    # BEFORE the loop so a mid-loop exception leaves the branch stale, never
    # stale-but-true (single commit — sync_current.py:137 — persists this state).
    if needing_embed_full:
        cursor.execute("UPDATE branches SET embedding_version = 0 WHERE id = ?", (branch_db_id,))

    # Cap the embed loop to bound per-sync inference cost (write path); the
    # backfill passes max_embeds=None to embed the whole branch in one call.
    needing_embed = needing_embed_full if max_embeds is None else needing_embed_full[:max_embeds]

    # Step 6 — embed loop: for each needing-embed exchange, upsert the chunks row,
    # embed the text, then write the vector (order invariant: vector FIRST,
    # bookkeeping LAST — so a mid-loop exception leaves the chunk eligible for
    # backfill rather than marked done-without-vector).
    for ed in needing_embed:
        # Upsert chunks row via DELETE+INSERT (vec0 rejects INSERT OR REPLACE)
        cursor.execute(
            "DELETE FROM chunks WHERE branch_id = ? AND exchange_index = ?",
            (branch_db_id, ed["index"]),
        )
        cursor.execute(
            """
            INSERT INTO chunks (
                branch_id, exchange_index, content_hash, first_message_uuid,
                timestamp, user_text, assistant_text, was_capped,
                embedding_version, embedding_model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
            """,
            (
                branch_db_id,
                ed["index"],
                ed["content_hash"],
                ed["first_message_uuid"],
                ed["timestamp"],
                ed["user_text"],
                ed["assistant_text"],
                int(ed["was_capped"]),
            ),
        )
        chunk_id = cursor.lastrowid
        assert chunk_id is not None  # noqa: S101 — lastrowid is non-None after a successful INSERT
        # Vector FIRST (order invariant), bookkeeping LAST
        vec = embed_text(ed["text"])
        write_chunk_embedding(cursor, chunk_id, vec, EMBEDDING_VERSION, EMBEDDING_MODEL)

    # Step 7 — prune: delete chunks whose exchange_index no longer exists.
    # The chunks_vec_ad cascade trigger removes their chunk_vec rows automatically.
    if indices_to_prune:
        ph = ",".join("?" * len(indices_to_prune))
        cursor.execute(
            f"DELETE FROM chunks WHERE branch_id = ? AND exchange_index IN ({ph})",
            (branch_db_id, *indices_to_prune),
        )

    # Step 8 — set watermark iff every exchange now has a current-version chunk
    # with the correct content_hash. Checks both version AND content_hash so that
    # content-changed exchanges beyond the cap (left for backfill) don't falsely
    # satisfy the predicate.
    embedded_indices = {ed["index"] for ed in needing_embed}
    all_current = True
    for ed in exchange_data:
        idx = ed["index"]
        if idx in embedded_indices:
            continue  # just embedded at EMBEDDING_VERSION with correct content_hash
        existing = existing_chunks.get(idx)
        if (
            existing is None
            or existing["embedding_version"] != EMBEDDING_VERSION
            or existing["content_hash"] != ed["content_hash"]
        ):
            all_current = False
            break
    if all_current:
        cursor.execute(
            "UPDATE branches SET embedding_version = ?, embedding_model = ? WHERE id = ?",
            (EMBEDDING_VERSION, EMBEDDING_MODEL, branch_db_id),
        )

    return len(needing_embed)


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

    write_branch_summary(cursor, branch_db_id)
    # fetch_branch_messages returns flat {role, content, timestamp, uuid} dicts — the
    # format build_exchange_pairs expects. branch_msgs (raw JSONL) is the right input
    # for metadata computation above but not for embedding.
    with contextlib.suppress(Exception):
        embed_msgs = fetch_branch_messages(cursor, branch_db_id, include_notifications=False)
        embed_branch_chunks(cursor, branch_db_id, embed_msgs, is_active, vec_writable)


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

    # Probe vec persistence once: if sqlite-vec didn't load, chunk_vec doesn't
    # exist and embed_branch_chunks would raise. Skip embed-on-write entirely
    # in that case rather than paying for embed_text inference on every active
    # leaf just to have the write swallowed.
    vec_writable = chunk_vec_queryable(conn)

    for branch in branches:
        sync_branch(cursor, branch, messages, uuid_to_msg_id, existing_branches, session_id, vec_writable)

    if write_import_log:
        upsert_import_log(cursor, filepath, session_id, file_hash, log_row)

    return new_count
