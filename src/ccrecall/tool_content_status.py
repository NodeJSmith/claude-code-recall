"""Read-only status helpers for ``messages.tool_content`` coverage."""

import logging
import sqlite3
from pathlib import Path

from ccrecall.hooks.tool_content_eligibility import ELIGIBILITY_FROM, days_modifier, eligibility_clause
from ccrecall.import_log_ops import (
    import_log_source_index,
    pending_tool_content_uuids,
    transcript_pending_tool_content_uuids,
)
from ccrecall.models import LOGGER_NAME
from ccrecall.parsing import find_all_branches, parse_all_with_uuids

log = logging.getLogger(LOGGER_NAME)

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
    file, the existing ones don't cover every pending uuid, or a source
    recorded as existing raises `OSError` when actually read — a TOCTOU race
    or permissions change since the existence check) is `missing` and is
    never also checked for `no_usable_branch` — checking both independently
    let a session with mixed source availability (some paths missing,
    remaining existing path empty/invalid) land in both buckets, driving
    `pending_backfillable_sessions` negative (#207 review).

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

        # Wraps both `transcript_pending_tool_content_uuids` below and
        # `parse_all_with_uuids` further down — the two calls in this block that
        # actually open a transcript file and can raise on a path
        # `import_log_source_index()` had recorded as existing. The `is_missing`/
        # `continue` logic in between is pure bookkeeping and can't raise; it's
        # inside the `try` only because it sits between those two calls.
        try:
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
        except OSError:
            # A path recorded as "existing" by import_log_source_index() can still
            # vanish or become unreadable before we open it here (TOCTOU race, or a
            # permissions change) — treat that the same as a missing source file
            # instead of letting the exception abort the whole status report (#207
            # review: it previously reached ccrecall status's top-level
            # FileNotFoundError handler and was misreported as the database itself
            # missing).
            log.warning(
                "transcript unreadable during status classification; session will be counted as missing",
                exc_info=True,
                extra={"session_uuid": session_uuid},
            )
            missing += 1
            continue

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
