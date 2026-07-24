"""Shared "still needs tool_content backfill" predicate.

Single source of truth for the eligibility predicate used by both
backfill_tool_content.py (the actual backfill CLI: count_eligible,
select_batch, count_total_sessions) and context_alerts.py (the SessionStart
proactive-alert sampling check), so the two can't drift.

Deliberately dependency-free (no ccrecall.db / ccrecall.embeddings) — safe to
import on the SessionStart hot path. context_alerts.py imports
eligibility_clause directly; importing it from backfill_tool_content.py
instead would drag in that module's ccrecall.db import chain (which reaches
fastembed/onnxruntime via ccrecall.embeddings), violating the hot-path
invariant documented in this repo's CLAUDE.md.

days_modifier lives here too (rather than backfill_query.py, which has heavy
imports) since it's dependency-free and eligibility_clause needs it;
backfill_query.py imports it back for its own --days handling so the
chunk-embedding and tool-content domains still share one recency-bound
formatter without either pulling in the other's heavy deps.
"""

# SQLite's default bound-parameter limit is 999; leave headroom.
MAX_SQL_PARAMS = 900

# Shared FROM clause for every "sessions needing tool_content backfill" query
# (the eligible-count, the per-batch selection, --status, and the SessionStart
# sampling check) so the join shape can't drift between them.
ELIGIBILITY_FROM = """
    FROM messages m
    JOIN sessions s ON s.id = m.session_id
    JOIN branches b ON b.session_id = s.id AND b.is_active = 1
"""


def days_modifier(days: int) -> str:
    """SQLite datetime() modifier for an N-day lookback (days=7 -> '-7 days').

    Single source of truth for the --days recency bound so build_selection()
    (chunk-embedding eligibility), eligibility_clause() (tool-content
    eligibility), and count_status() (progress) can't construct it differently.
    """
    return f"-{days} days"


def eligibility_clause(days: int | None, exclude_ids: set[int] | None = None) -> tuple[str, list]:
    """WHERE clause (+ params) for "sessions still needing tool_content backfill".

    Single source of truth for the one-time eligible count, the per-batch
    selection query, and the SessionStart sampling check, mirroring
    backfill_query.build_selection's pattern so none of them can drift.
    ``exclude_ids`` removes sessions this run already attempted (succeeded,
    errored, or had no on-disk file) so a stalled session can't force the
    no-progress guard to fire on every batch.

    The NOT IN clause is chunked to stay under SQLite's bound-parameter limit.
    """
    where = "WHERE m.tool_content IS NULL"
    params: list = []
    if exclude_ids:
        ids = sorted(exclude_ids)
        not_in_parts: list[str] = []
        for i in range(0, len(ids), MAX_SQL_PARAMS):
            chunk = ids[i : i + MAX_SQL_PARAMS]
            placeholders = ",".join("?" * len(chunk))
            not_in_parts.append(f"s.id NOT IN ({placeholders})")
            params.extend(chunk)
        where += " AND " + " AND ".join(not_in_parts)
    if days is not None:
        where += " AND b.ended_at > datetime('now', ?)"
        params.append(days_modifier(days))
    return where, params
