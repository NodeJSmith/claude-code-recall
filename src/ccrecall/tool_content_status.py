"""Read-only status helpers for ``messages.tool_content`` coverage."""

import sqlite3

from ccrecall.hooks.tool_content_eligibility import ELIGIBILITY_FROM, days_modifier, eligibility_clause
from ccrecall.import_log_ops import import_log_source_index


def count_eligible(cursor: sqlite3.Cursor, days: int | None) -> int:
    where, params = eligibility_clause(days)
    return cursor.execute(f"SELECT COUNT(DISTINCT s.id) {ELIGIBILITY_FROM} {where}", params).fetchone()[0]


def count_pending_missing_jsonl(cursor: sqlite3.Cursor, days: int | None) -> int:
    """Count pending sessions with at least one missing source transcript file."""
    where, params = eligibility_clause(days)
    pending = cursor.execute(f"SELECT DISTINCT s.id, s.uuid {ELIGIBILITY_FROM} {where}", params).fetchall()

    sources = import_log_source_index(cursor)
    return sum(1 for _session_id, session_uuid in pending if sources.get(session_uuid, {}).get("missing"))


def count_total_sessions(cursor: sqlite3.Cursor, days: int | None) -> int:
    """Count every session with messages (the backfill's universe), for status."""
    where = "WHERE 1=1"
    params: list = []
    if days is not None:
        where += " AND b.ended_at > datetime('now', ?)"
        params.append(days_modifier(days))
    return cursor.execute(f"SELECT COUNT(DISTINCT s.id) {ELIGIBILITY_FROM} {where}", params).fetchone()[0]
