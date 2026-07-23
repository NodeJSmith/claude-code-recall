"""Session and message row operations for session sync/import.

``upsert_session`` writes the sessions row; ``build_message_row`` /
``insert_new_messages`` handle per-entry filtering, UUID dedup, and INSERT of
new messages.
"""

import sqlite3

from ccrecall.content import (
    extract_text_content,
    is_task_notification,
    is_teammate_message,
    is_tool_result,
    parse_origin,
)


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
    UUIDs, entries with neither text nor tool content, and already-inserted UUIDs.
    A tool-only assistant turn (no prose, just tool_use blocks) still produces a
    row — content is '' and tool_content carries the searchable marker text.
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
    text, _has_tool_use, has_thinking, _tool_summary, tool_content = extract_text_content(content)
    if not text and not tool_content:
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
        tool_content,
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
                (session_id, uuid, parent_uuid, timestamp, role, content, has_thinking, is_notification, origin,
                 tool_content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, uuid) DO NOTHING
            """,
            row,
        )
        if cursor.rowcount > 0:
            new_count += 1
            existing_uuids.add(row[1])
    return new_count
