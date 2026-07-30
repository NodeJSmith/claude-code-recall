"""Read-only status helpers for ``messages.tool_content`` coverage."""

import sqlite3
from pathlib import Path

from ccrecall.hooks.tool_content_eligibility import ELIGIBILITY_FROM, days_modifier, eligibility_clause
from ccrecall.import_log_ops import (
    import_log_source_index,
    pending_tool_content_uuids,
    transcript_pending_tool_content_uuids,
)


def count_eligible(cursor: sqlite3.Cursor, days: int | None) -> int:
    where, params = eligibility_clause(days)
    return cursor.execute(f"SELECT COUNT(DISTINCT s.id) {ELIGIBILITY_FROM} {where}", params).fetchone()[0]


def count_pending_missing_jsonl(
    cursor: sqlite3.Cursor,
    days: int | None,
    sources: dict[str, dict[str, list[Path]]] | None = None,
) -> int:
    """Count pending sessions whose NULL tool_content rows cannot be recovered from surviving sources."""
    where, params = eligibility_clause(days)
    pending = cursor.execute(f"SELECT DISTINCT s.id, s.uuid {ELIGIBILITY_FROM} {where}", params).fetchall()

    if sources is None:
        sources = import_log_source_index(cursor)

    missing = 0
    for _session_id, session_uuid in pending:
        paths = sources.get(session_uuid, {"existing": [], "missing": []})
        if not paths["missing"]:
            continue
        if not paths["existing"]:
            missing += 1
            continue
        pending_uuids = pending_tool_content_uuids(cursor, session_uuid)
        recoverable_uuids: set[str] = set()
        for path in paths["existing"]:
            recoverable_uuids.update(transcript_pending_tool_content_uuids(path, pending_uuids))
        if pending_uuids - recoverable_uuids:
            missing += 1
    return missing


def count_total_sessions(cursor: sqlite3.Cursor, days: int | None) -> int:
    """Count every session with messages (the backfill's universe), for status."""
    where = "WHERE 1=1"
    params: list = []
    if days is not None:
        where += " AND b.ended_at > datetime('now', ?)"
        params.append(days_modifier(days))
    return cursor.execute(f"SELECT COUNT(DISTINCT s.id) {ELIGIBILITY_FROM} {where}", params).fetchone()[0]
