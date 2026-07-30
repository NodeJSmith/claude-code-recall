"""Read-only transcript-vs-DB ingestion coverage diagnostics."""

import time
from pathlib import Path
from sqlite3 import Connection

from ccrecall.content import extract_text_content, is_tool_result
from ccrecall.import_log_ops import import_log_source_index
from ccrecall.parsing import is_insertable_message, parse_all_with_uuids

STALE_TAIL_SECONDS = 15 * 60


def _entry_expects_message(entry: dict) -> bool:
    """Mirror message_ops.build_message_row's content-based skip rules."""
    if not is_insertable_message(entry):
        return False
    content = entry.get("message", {}).get("content", "")
    if entry.get("type") == "user" and is_tool_result(content):
        return False
    text, _has_tool_use, _has_thinking, _tool_summary, tool_content = extract_text_content(content)
    return bool(text or tool_content)


def _expected_uuids(filepaths: list[Path]) -> list[str]:
    """Return ordered active-branch UUIDs expected to have messages rows."""
    entries: list[dict] = []
    for filepath in filepaths:
        entries.extend(parse_all_with_uuids(filepath))
    uuid_to_entry = {entry["uuid"]: entry for entry in entries if entry.get("uuid")}
    if not uuid_to_entry:
        return []

    latest = max(uuid_to_entry.values(), key=lambda entry: entry.get("timestamp") or "")
    ordered_branch: list[dict] = []
    current_uuid: str | None = latest["uuid"]
    while current_uuid:
        entry = uuid_to_entry.get(current_uuid)
        if entry is None:
            break
        ordered_branch.append(entry)
        current_uuid = entry.get("parentUuid")
    ordered_branch.reverse()
    return [entry["uuid"] for entry in ordered_branch if _entry_expects_message(entry)]


def _is_contiguous_suffix(indices: list[int], total: int) -> bool:
    if not indices:
        return False
    return indices == list(range(indices[0], total))


def summarize_ingestion(conn: Connection, *, stale_tail_seconds: int = STALE_TAIL_SECONDS) -> dict[str, int]:
    """Classify transcript ingestion gaps by comparing JSONL UUID order to DB rows.

    ``pending_tail`` means the DB is missing only a contiguous suffix from an
    existing transcript that was modified recently, which is normal while Claude
    Code is still writing the session. ``stale_tail`` is the same shape after the
    grace window. ``ingestion_gap`` means missing UUIDs are in the middle of the
    expected active branch and should be recoverable by import/sync. A session
    with import-log rows but no surviving JSONL is counted as ``missing_source``.
    """
    cursor = conn.cursor()
    sources = import_log_source_index(cursor)

    summary = {
        "sessions_checked": 0,
        "ok_sessions": 0,
        "pending_tail_sessions": 0,
        "pending_tail_turns": 0,
        "stale_tail_sessions": 0,
        "stale_tail_turns": 0,
        "ingestion_gap_sessions": 0,
        "ingestion_gap_turns": 0,
        "missing_source_sessions": 0,
    }

    for session_uuid, paths in sources.items():
        if not paths["missing"]:
            continue
        if cursor.execute("SELECT 1 FROM sessions WHERE uuid = ?", (session_uuid,)).fetchone() is not None:
            summary["sessions_checked"] += 1
            summary["missing_source_sessions"] += 1

    now = time.time()
    for session_uuid, paths in sources.items():
        if paths["missing"]:
            continue
        filepaths = paths["existing"]
        session_row = cursor.execute("SELECT id FROM sessions WHERE uuid = ?", (session_uuid,)).fetchone()
        if session_row is None:
            continue
        summary["sessions_checked"] += 1
        session_id = session_row[0]
        existing_msg_uuids = {
            row[0]
            for row in cursor.execute(
                "SELECT uuid FROM messages WHERE session_id = ? AND uuid IS NOT NULL",
                (session_id,),
            ).fetchall()
        }

        expected = _expected_uuids(filepaths)
        missing_indices = [i for i, uuid in enumerate(expected) if uuid not in existing_msg_uuids]
        if not missing_indices:
            summary["ok_sessions"] += 1
            continue

        if _is_contiguous_suffix(missing_indices, len(expected)):
            newest_mtime = max(path.stat().st_mtime for path in filepaths)
            if now - newest_mtime <= stale_tail_seconds:
                summary["pending_tail_sessions"] += 1
                summary["pending_tail_turns"] += len(missing_indices)
            else:
                summary["stale_tail_sessions"] += 1
                summary["stale_tail_turns"] += len(missing_indices)
        else:
            summary["ingestion_gap_sessions"] += 1
            summary["ingestion_gap_turns"] += len(missing_indices)

    return summary
