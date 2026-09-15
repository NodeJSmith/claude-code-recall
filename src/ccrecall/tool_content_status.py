"""Read-only status helpers for ``messages.tool_content`` coverage."""

import sqlite3
from pathlib import Path

from ccrecall.hooks.tool_content_eligibility import ELIGIBILITY_FROM, days_modifier, eligibility_clause
from ccrecall.import_log_ops import (
    import_log_source_index,
    pending_tool_content_uuids,
    transcript_pending_tool_content_uuids,
)
from ccrecall.parsing import find_all_branches, parse_all_with_uuids

# The no-pending-sessions shape of classify_pending_sessions's return value, for
# callers that short-circuit the (expensive, import_log-scanning) classification
# scan entirely when there's nothing pending — see collect_status/run_tool_content_status.
EMPTY_CLASSIFICATION = {"missing": 0, "no_usable_branch": 0}


def count_eligible(cursor: sqlite3.Cursor, days: int | None) -> int:
    where, params = eligibility_clause(days)
    return cursor.execute(f"SELECT COUNT(DISTINCT s.id) {ELIGIBILITY_FROM} {where}", params).fetchone()[0]


def classify_pending_sessions(
    cursor: sqlite3.Cursor,
    days: int | None,
    sources: dict[str, dict[str, list[Path]]] | None = None,
) -> dict[str, int]:
    """Classify pending sessions into mutually exclusive `missing`/`no_usable_branch` buckets.

    One pass per session, `missing` decided first: a session whose NULL
    tool_content rows can't be recovered from surviving sources (no existing
    file, or the existing ones don't cover every pending uuid) is `missing`
    and is never also checked for `no_usable_branch` — checking both
    independently let a session with mixed source availability (some paths
    missing, remaining existing path empty/invalid) land in both buckets,
    driving `pending_backfillable_sessions` negative (#207 review).

    A session that clears the `missing` check is then checked against
    ``backfill_session``'s no-op conditions (zero parsed entries, or
    ``find_all_branches`` finds none) — those sessions never leave the
    eligible set no matter how many times the backfill runs, so `--status`
    must not lump them into ``pending_backfillable_sessions`` alongside
    sessions the backfill can actually resolve.
    """
    where, params = eligibility_clause(days)
    pending = cursor.execute(f"SELECT DISTINCT s.id, s.uuid {ELIGIBILITY_FROM} {where}", params).fetchall()

    if sources is None:
        sources = import_log_source_index(cursor)

    missing = 0
    no_usable_branch = 0
    for _session_id, session_uuid in pending:
        paths = sources.get(session_uuid, {"existing": [], "missing": []})

        is_missing = False
        if paths["missing"]:
            if not paths["existing"]:
                is_missing = True
            else:
                pending_uuids = pending_tool_content_uuids(cursor, session_uuid)
                recoverable_uuids: set[str] = set()
                for path in paths["existing"]:
                    recoverable_uuids.update(transcript_pending_tool_content_uuids(path, pending_uuids))
                is_missing = bool(pending_uuids - recoverable_uuids)
        if is_missing:
            missing += 1
            continue

        if not paths["existing"]:
            continue
        all_entries: list[dict] = []
        for path in paths["existing"]:
            all_entries.extend(parse_all_with_uuids(path))
        if not all_entries or not find_all_branches(all_entries):
            no_usable_branch += 1

    return {"missing": missing, "no_usable_branch": no_usable_branch}


def count_total_sessions(cursor: sqlite3.Cursor, days: int | None) -> int:
    """Count every session with messages (the backfill's universe), for status."""
    where = "WHERE 1=1"
    params: list = []
    if days is not None:
        where += " AND b.ended_at > datetime('now', ?)"
        params.append(days_modifier(days))
    return cursor.execute(f"SELECT COUNT(DISTINCT s.id) {ELIGIBILITY_FROM} {where}", params).fetchone()[0]
