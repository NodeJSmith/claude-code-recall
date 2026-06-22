"""
token_output — Dashboard JSON output assembly.
Queries the token analytics database and builds the full output dict
consumed by the HTML dashboard and the slim JSON printed to stdout.
"""

import sqlite3
from itertools import groupby

from whenever import Instant

from ccrecall.token_insights import build_insights_and_trends
from ccrecall.token_parser import (
    _BASH_ANTIPATTERN_PREDICATE,
    project_slug,
    row_cost,
)

# SQL join fragment used by edit_retries detail and total queries.
_EDIT_RETRY_JOIN = """
    FROM turn_tool_calls tc1
    JOIN turns t1 ON tc1.turn_id = t1.id
    JOIN turns t2 ON t1.session_id = t2.session_id AND t2.turn_index = t1.turn_index + 1
    JOIN turn_tool_calls tc2 ON tc2.turn_id = t2.id
        AND tc2.file_path = tc1.file_path
        AND tc1.tool_name IN ('Edit', 'Write')
        AND tc2.tool_name IN ('Edit', 'Write')
        AND tc1.is_error = 1
    JOIN session_metrics sm ON tc1.session_id = sm.session_id AND sm.is_sidechain = 0
"""


def query_session_totals(cur: sqlite3.Cursor) -> dict:
    """Fetch and unpack the aggregate token/session totals row."""
    row = cur.execute("""
        SELECT COUNT(*), SUM(turn_count), SUM(total_output_tokens),
               SUM(total_cache_read), SUM(total_cache_creation),
               SUM(cache_cliff_count), SUM(max_tokens_stops),
               SUM(tool_error_count), SUM(total_input_tokens),
               SUM(total_thinking),
               SUM(total_ephem_5m), SUM(total_ephem_1h)
        FROM session_metrics WHERE is_sidechain = 0
    """).fetchone()
    return {
        "total_sessions": row[0] or 0,
        "total_turns": row[1] or 0,
        "total_output": row[2] or 0,
        "total_cache_read": row[3] or 0,
        "total_cache_creation": row[4] or 0,
        "total_cache_cliffs": row[5] or 0,
        "total_max_token_stops": row[6] or 0,
        "total_tool_errors": row[7] or 0,
        "total_input": row[8] or 0,
        "total_thinking": row[9] or 0,
        "total_ephem_5m": row[10] or 0,
        "total_ephem_1h": row[11] or 0,
    }


def derive_cache_metrics(totals: dict) -> tuple[str, float]:
    """Derive dominant_cache_tier and global_cache_ratio from session totals."""
    e5, e1 = totals["total_ephem_5m"], totals["total_ephem_1h"]
    no_ephem_data = e5 == 0 and e1 == 0
    dominant_cache_tier = "5m" if (no_ephem_data or e5 > e1) else "1h"
    cache_denom = totals["total_cache_read"] + totals["total_cache_creation"]
    global_cache_ratio = round(totals["total_cache_read"] / cache_denom, 4) if cache_denom > 0 else 0.0
    return dominant_cache_tier, global_cache_ratio


def build_kpis(cur: sqlite3.Cursor) -> dict:
    """Query top-level KPI totals and return them plus all intermediates."""
    totals = query_session_totals(cur)
    dominant_cache_tier, global_cache_ratio = derive_cache_metrics(totals)

    total_tool_calls = (
        cur.execute("""
        SELECT COUNT(*) FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id AND sm.is_sidechain = 0
    """).fetchone()[0]
        or 0
    )

    date_row = cur.execute("""
        SELECT MIN(first_turn_ts), MAX(last_turn_ts)
        FROM session_metrics WHERE is_sidechain = 0
    """).fetchone()

    return {
        **totals,
        "total_tool_calls": total_tool_calls,
        "global_cache_ratio": global_cache_ratio,
        "dominant_cache_tier": dominant_cache_tier,
        "date_range": {
            "earliest": date_row[0][:10] if date_row and date_row[0] else None,
            "latest": date_row[1][:10] if date_row and date_row[1] else None,
        },
        # Partial kpis dict — total_cost_usd and bash_antipatterns added later
        "kpis_partial": {
            "total_sessions": totals["total_sessions"],
            "total_turns": totals["total_turns"],
            "total_output_tokens": totals["total_output"],
            "global_cache_ratio": global_cache_ratio,
            "cache_cliffs": totals["total_cache_cliffs"],
            "max_token_stops": totals["total_max_token_stops"],
        },
    }


def build_sessions_by_day(cur: sqlite3.Cursor) -> list[dict]:
    """Sessions aggregated by calendar day."""
    return [
        {
            "date": row[0],
            "session_count": row[1],
            "input_tokens": row[2],
            "output_tokens": row[3],
            "cache_read": row[4],
            "cache_creation": row[5],
        }
        for row in cur.execute("""
        SELECT DATE(first_turn_ts) as day, COUNT(*),
               SUM(total_input_tokens), SUM(total_output_tokens),
               SUM(total_cache_read), SUM(total_cache_creation)
        FROM session_metrics WHERE is_sidechain = 0 AND first_turn_ts IS NOT NULL
        GROUP BY day ORDER BY day
    """)
    ]


def build_top_tools(cur: sqlite3.Cursor) -> list[dict]:
    """Top 15 tool calls by frequency."""
    return [
        {"tool": row[0], "count": row[1]}
        for row in cur.execute("""
        SELECT tool_name, COUNT(*) as cnt
        FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id AND sm.is_sidechain = 0
        GROUP BY tool_name ORDER BY cnt DESC LIMIT 15
    """)
    ]


def build_model_split(cur: sqlite3.Cursor) -> list[dict]:
    """Cost and token totals broken down by model."""
    model_split = []
    for row in cur.execute("""
        SELECT model, SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(thinking_tokens) as think,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
               SUM(ephem_5m_tokens) as e5, SUM(ephem_1h_tokens) as e1
        FROM turns t
        JOIN session_metrics sm ON t.session_id = sm.session_id AND sm.is_sidechain = 0
        WHERE model IS NOT NULL
        GROUP BY model ORDER BY inp + out DESC
    """):
        cost = row_cost(row, model_idx=0, token_indices=[1, 2, 4, 5, 6, 7])
        model_split.append(
            {
                "model": row[0],
                "input_tokens": row[1],
                "output_tokens": row[2],
                "thinking_tokens": row[3],
                "cost_usd": round(cost, 4),
            }
        )
    return model_split


def build_cost_by_day(cur: sqlite3.Cursor) -> list[dict]:
    """Dollar cost per calendar day."""
    cost_by_day: dict[str, float] = {}
    for row in cur.execute("""
        SELECT DATE(t.timestamp) as day, t.model,
               SUM(t.input_tokens), SUM(t.output_tokens),
               SUM(t.cache_read_tokens), SUM(t.cache_creation_tokens),
               SUM(t.ephem_5m_tokens), SUM(t.ephem_1h_tokens)
        FROM turns t
        JOIN session_metrics sm ON t.session_id = sm.session_id AND sm.is_sidechain = 0
        WHERE t.timestamp IS NOT NULL
        GROUP BY day, t.model
    """):
        day = row[0]
        day_cost = row_cost(row, model_idx=1, token_indices=[2, 3, 4, 5, 6, 7])
        cost_by_day[day] = cost_by_day.get(day, 0.0) + day_cost
    return [{"date": d, "cost_usd": round(c, 4)} for d, c in sorted(cost_by_day.items())]


def build_cost_by_project(cur: sqlite3.Cursor) -> list[dict]:
    """Dollar cost per project (top 10), sorted descending."""
    cost_by_project: dict[str, float] = {}
    for row in cur.execute("""
        SELECT sm.project_path, t.model,
               SUM(t.input_tokens), SUM(t.output_tokens),
               SUM(t.cache_read_tokens), SUM(t.cache_creation_tokens),
               SUM(t.ephem_5m_tokens), SUM(t.ephem_1h_tokens)
        FROM turns t
        JOIN session_metrics sm ON t.session_id = sm.session_id AND sm.is_sidechain = 0
        GROUP BY sm.project_path, t.model
    """):
        slug = project_slug(row[0])
        proj_cost = row_cost(row, model_idx=1, token_indices=[2, 3, 4, 5, 6, 7])
        cost_by_project[slug] = cost_by_project.get(slug, 0.0) + proj_cost
    return sorted(
        [{"project": p, "cost_usd": round(c, 4)} for p, c in cost_by_project.items()],
        key=lambda x: x["cost_usd"],
        reverse=True,
    )[:10]


def build_cache_trajectory(cur: sqlite3.Cursor) -> list[dict]:
    """Cache read/creation ratio per turn for the 5 most cache-heavy sessions."""
    cache_trajectory = []
    trajectory_sessions = cur.execute("""
        SELECT session_id FROM session_metrics
        WHERE is_sidechain = 0 AND total_cache_read + total_cache_creation > 0
        ORDER BY total_cache_read + total_cache_creation DESC LIMIT 5
    """).fetchall()
    for (tsid,) in trajectory_sessions:
        turns_data = [
            {
                "turn": row[0],
                "ratio": row[1],
                "read": row[2],
                "creation": row[3],
                "gap_ms": row[4],
            }
            for row in cur.execute(
                """
            SELECT turn_index, cache_read_ratio, cache_read_tokens, cache_creation_tokens, user_gap_ms
            FROM turns WHERE session_id = ? ORDER BY turn_index LIMIT 30
        """,
                (tsid,),
            )
        ]
        proj = cur.execute("SELECT project_path FROM session_metrics WHERE session_id = ?", (tsid,)).fetchone()
        cache_trajectory.append(
            {
                "session_id": tsid[:8],
                "project": project_slug(proj[0] if proj else None),
                "turns": turns_data,
            }
        )
    return cache_trajectory


def accumulate_seg_buckets(all_turns: list, buckets: dict[int, dict[str, list[float]]]) -> None:
    """Accumulate per-turn base/history/tool percentages into buckets in-place."""
    for _sid, session_turns in groupby(all_turns, key=lambda r: r[0]):
        rows = list(session_turns)
        if not rows or rows[0][2] <= 0:
            continue
        base_ctx = rows[0][2]
        cumul_output = 0
        for _, turn_idx, total_ctx, out_tok in rows:
            if total_ctx <= 0:
                cumul_output += out_tok
                continue
            base = min(base_ctx, total_ctx)
            history = min(cumul_output, total_ctx - base)
            tool_user = max(0, total_ctx - base - history)
            bucket = buckets.setdefault(turn_idx, {"base": [], "hist": [], "tool": []})
            bucket["base"].append(base / total_ctx * 100)
            bucket["hist"].append(history / total_ctx * 100)
            bucket["tool"].append(tool_user / total_ctx * 100)
            cumul_output += out_tok


def compute_seg_curve(
    cur: sqlite3.Cursor, session_ids: list[str], max_turns: int = 60, min_sessions: int = 3
) -> list[dict]:
    """Compute per-turn context segmentation curve for the given session IDs."""
    if not session_ids:
        return []
    placeholders = ",".join("?" * len(session_ids))
    all_turns = cur.execute(
        f"""
        SELECT session_id, turn_index,
               input_tokens + cache_read_tokens + cache_creation_tokens as total_ctx,
               output_tokens
        FROM turns
        WHERE session_id IN ({placeholders})
        ORDER BY session_id, turn_index
    """,
        session_ids,
    ).fetchall()

    buckets: dict[int, dict[str, list[float]]] = {}
    accumulate_seg_buckets(all_turns, buckets)

    curve = []
    for t_idx in sorted(buckets.keys())[:max_turns]:
        b = buckets[t_idx]
        n = len(b["base"])
        if n < min_sessions:
            continue
        curve.append(
            {
                "turn": t_idx,
                "sessions": n,
                "base_pct": round(sum(b["base"]) / n, 1),
                "history_pct": round(sum(b["hist"]) / n, 1),
                "tool_user_pct": round(sum(b["tool"]) / n, 1),
            }
        )
    return curve


def query_seg_summary(cur: sqlite3.Cursor, recent_count: int) -> dict:
    """Aggregate base-overhead stats across all long sessions (turn_count >= 8)."""
    seg_agg = cur.execute("""
        SELECT
            SUM(t1_ctx * tc),
            SUM(total_all_ctx),
            COUNT(*),
            AVG(t1_ctx)
        FROM (
            SELECT sm.session_id, sm.turn_count as tc,
                   (SELECT input_tokens + cache_read_tokens + cache_creation_tokens
                    FROM turns WHERE session_id = sm.session_id AND turn_index = 1) as t1_ctx,
                   (SELECT SUM(input_tokens + cache_read_tokens + cache_creation_tokens)
                    FROM turns WHERE session_id = sm.session_id) as total_all_ctx
            FROM session_metrics sm
            WHERE sm.is_sidechain = 0 AND sm.turn_count >= 8
        )
        WHERE t1_ctx > 0 AND total_all_ctx > 0
    """).fetchone()
    base_overhead_paid = seg_agg[0] or 0
    total_ctx_paid = seg_agg[1] or 1
    return {
        "sessions_analyzed": seg_agg[2] or 0,
        "avg_base_ctx": round(seg_agg[3] or 0),
        "base_overhead_pct": round(base_overhead_paid / total_ctx_paid * 100, 1),
        "recent_sessions_count": recent_count,
    }


def build_context_segments(cur: sqlite3.Cursor) -> dict:
    """Context segmentation curves plus summary aggregation."""
    all_seg_sids = [
        r[0]
        for r in cur.execute("""
        SELECT session_id FROM session_metrics
        WHERE is_sidechain = 0 AND turn_count >= 8
    """).fetchall()
    ]
    context_segments = compute_seg_curve(cur, all_seg_sids)

    recent_seg_sids = [
        r[0]
        for r in cur.execute("""
        SELECT session_id FROM session_metrics
        WHERE is_sidechain = 0 AND turn_count >= 8
              AND first_turn_ts >= datetime('now', '-7 days')
    """).fetchall()
    ]
    context_segments_recent = compute_seg_curve(cur, recent_seg_sids, min_sessions=2)
    context_seg_summary = query_seg_summary(cur, len(recent_seg_sids))

    return {
        "context_segments": context_segments,
        "context_segments_recent": context_segments_recent,
        "context_seg_summary": context_seg_summary,
    }


def build_tool_footprint(cur: sqlite3.Cursor) -> list[dict]:
    """Average token footprint added per tool call (tools with >=10 calls)."""
    return [
        {"tool": row[0], "calls": row[1], "avg_tokens": int(row[2])}
        for row in cur.execute("""
        WITH single_tool_turns AS (
            SELECT tc.turn_id, tc.tool_name, tc.session_id
            FROM turn_tool_calls tc
            GROUP BY tc.turn_id
            HAVING COUNT(*) = 1
        )
        SELECT stt.tool_name, COUNT(*) as cnt,
               ROUND(AVG(
                 (tn.input_tokens + tn.cache_read_tokens + tn.cache_creation_tokens) -
                 (t.input_tokens + t.cache_read_tokens + t.cache_creation_tokens) -
                 t.output_tokens
               )) as avg_footprint
        FROM single_tool_turns stt
        JOIN turns t ON stt.turn_id = t.id
        JOIN turns tn ON tn.session_id = t.session_id AND tn.turn_index = t.turn_index + 1
        JOIN session_metrics sm ON stt.session_id = sm.session_id AND sm.is_sidechain = 0
        GROUP BY stt.tool_name
        HAVING cnt >= 10 AND avg_footprint > 0
        ORDER BY avg_footprint DESC LIMIT 12
    """)
    ]


def build_ephem_split(cur: sqlite3.Cursor) -> list[dict]:
    """Ephemeral cache tier split by project (top 8)."""
    return [
        {"project": project_slug(row[0]), "ephem_5m": row[1], "ephem_1h": row[2]}
        for row in cur.execute("""
        SELECT sm.project_path, SUM(t.ephem_5m_tokens) as e5, SUM(t.ephem_1h_tokens) as e1
        FROM turns t
        JOIN session_metrics sm ON t.session_id = sm.session_id AND sm.is_sidechain = 0
        GROUP BY sm.project_path
        HAVING e5 + e1 > 0
        ORDER BY e5 + e1 DESC LIMIT 8
    """)
    ]


def build_bash_antipatterns(cur: sqlite3.Cursor) -> tuple[list[dict], int]:
    """Bash antipatterns by project (top 10) and global total."""
    detail = [
        {
            "project": project_slug(row[0]),
            "antipatterns": row[1],
            "total_bash": row[2],
        }
        for row in cur.execute(f"""
        SELECT sm.project_path,
               SUM(CASE WHEN {_BASH_ANTIPATTERN_PREDICATE} THEN 1 ELSE 0 END) as antipatterns,
               SUM(CASE WHEN tc.tool_name = 'Bash' THEN 1 ELSE 0 END) as total_bash
        FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id AND sm.is_sidechain = 0
        GROUP BY sm.project_path
        HAVING antipatterns > 0
        ORDER BY antipatterns DESC LIMIT 10
    """)
    ]
    total = (
        cur.execute(f"""
        SELECT SUM(CASE WHEN {_BASH_ANTIPATTERN_PREDICATE} THEN 1 ELSE 0 END)
        FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id AND sm.is_sidechain = 0
    """).fetchone()[0]
        or 0
    )
    return detail, total


def build_tool_errors_by_tool(cur: sqlite3.Cursor) -> list[dict]:
    """Tool error rate per tool name (top 10 by error count)."""
    return [
        {
            "tool": row[0],
            "errors": row[1],
            "total": row[2],
            "rate": round(row[1] / row[2], 4) if row[2] else 0,
        }
        for row in cur.execute("""
        SELECT tool_name, SUM(is_error) as errors, COUNT(*) as total
        FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id AND sm.is_sidechain = 0
        GROUP BY tool_name
        HAVING errors > 0
        ORDER BY errors DESC LIMIT 10
    """)
    ]


def build_redundant_reads(cur: sqlite3.Cursor) -> tuple[list[dict], int]:
    """Redundant Read hotspots (top 20) and total excess read count."""
    detail = [
        {
            "session_id": row[0][:8],
            "file": row[1].rsplit("/", 1)[-1] if row[1] else "?",
            "count": row[2],
        }
        for row in cur.execute("""
        SELECT tc.session_id, tc.file_path, COUNT(*) as cnt
        FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id AND sm.is_sidechain = 0
        WHERE tc.tool_name = 'Read' AND tc.file_path IS NOT NULL
        GROUP BY tc.session_id, tc.file_path
        HAVING cnt > 2
        ORDER BY cnt DESC LIMIT 20
    """)
    ]
    total = (
        cur.execute("""
        SELECT SUM(cnt - 1) FROM (
            SELECT COUNT(*) as cnt
            FROM turn_tool_calls tc
            JOIN session_metrics sm ON tc.session_id = sm.session_id AND sm.is_sidechain = 0
            WHERE tc.tool_name = 'Read' AND tc.file_path IS NOT NULL
            GROUP BY tc.session_id, tc.file_path
            HAVING cnt > 2
        )
    """).fetchone()[0]
        or 0
    )
    return detail, total


def build_edit_retries(cur: sqlite3.Cursor) -> tuple[list[dict], int]:
    """Edit retry chains by project (top 10) and global total."""
    detail = [
        {"project": project_slug(row[0]), "retries": row[1]}
        for row in cur.execute(f"""
        SELECT sm.project_path, COUNT(*) as retries
        {_EDIT_RETRY_JOIN}
        GROUP BY sm.project_path
        HAVING retries > 0
        ORDER BY retries DESC LIMIT 10
    """)
    ]
    total = (
        cur.execute(f"""
        SELECT COUNT(*)
        {_EDIT_RETRY_JOIN}
    """).fetchone()[0]
        or 0
    )
    return detail, total


def build_agent_cost(cur: sqlite3.Cursor) -> list[dict]:
    """Agent vs parent cost attribution by project (top 10)."""
    return [
        {
            "project": project_slug(row[0]),
            "parent_cost": row[1],
            "agent_cost": row[2],
        }
        for row in cur.execute("""
        SELECT parent.project_path,
               SUM(CASE WHEN child.is_sidechain = 0
                        THEN child.total_input_tokens + child.total_cache_creation ELSE 0 END) as parent_cost,
               SUM(CASE WHEN child.is_sidechain = 1
                        THEN child.total_input_tokens + child.total_cache_creation ELSE 0 END) as agent_cost
        FROM session_metrics parent
        JOIN session_metrics child
          ON child.parent_session_id = parent.session_id OR child.session_id = parent.session_id
        WHERE parent.is_sidechain = 0 AND parent.uses_agent = 1
        GROUP BY parent.project_path
        ORDER BY agent_cost DESC LIMIT 10
    """)
    ]


def build_turn_complexity(cur: sqlite3.Cursor) -> tuple[dict, dict]:
    """Turn complexity buckets and average thinking tokens per bucket."""
    turn_complexity = {"minimal": 0, "light": 0, "medium": 0, "heavy": 0, "runaway": 0}
    thinking_sum_complexity = {
        "minimal": 0,
        "light": 0,
        "medium": 0,
        "heavy": 0,
        "runaway": 0,
    }
    for row in cur.execute("""
        SELECT t.output_tokens, t.thinking_tokens
        FROM turns t
        JOIN session_metrics sm ON t.session_id = sm.session_id AND sm.is_sidechain = 0
    """):
        out, think = row[0] or 0, row[1] or 0
        if out < 100:
            bucket = "minimal"
        elif out < 500:
            bucket = "light"
        elif out < 2000:
            bucket = "medium"
        elif out < 8000:
            bucket = "heavy"
        else:
            bucket = "runaway"
        turn_complexity[bucket] += 1
        thinking_sum_complexity[bucket] += think
    thinking_in_complexity = {
        k: round(thinking_sum_complexity[k] / turn_complexity[k]) if turn_complexity[k] > 0 else 0
        for k in turn_complexity
    }
    return turn_complexity, thinking_in_complexity


def build_response_time_dist(cur: sqlite3.Cursor) -> dict:
    """User response time distribution bucketed by duration."""
    response_time_dist = {
        "under_30s": 0,
        "30s_2m": 0,
        "2m_5m": 0,
        "5m_15m": 0,
        "15m_1h": 0,
        "over_1h": 0,
    }
    for row in cur.execute("""
        SELECT user_gap_ms FROM turns t
        JOIN session_metrics sm ON t.session_id = sm.session_id AND sm.is_sidechain = 0
        WHERE user_gap_ms IS NOT NULL AND user_gap_ms > 0
    """):
        gap_s = (row[0] or 0) / 1000
        if gap_s < 30:
            response_time_dist["under_30s"] += 1
        elif gap_s < 120:
            response_time_dist["30s_2m"] += 1
        elif gap_s < 300:
            response_time_dist["2m_5m"] += 1
        elif gap_s < 900:
            response_time_dist["5m_15m"] += 1
        elif gap_s < 3600:
            response_time_dist["15m_1h"] += 1
        else:
            response_time_dist["over_1h"] += 1
    return response_time_dist


def build_hook_overhead(cur: sqlite3.Cursor) -> list[dict]:
    """Hook overhead per project (top 10 by total ms)."""
    return [
        {
            "project": project_slug(row[0]),
            "hook_ms": row[1],
            "sessions": row[2],
            "avg_hook_ms": round(row[1] / row[2]) if row[2] else 0,
        }
        for row in cur.execute("""
        SELECT project_path, SUM(total_hook_ms) as hook_ms, COUNT(*) as sessions
        FROM session_metrics WHERE is_sidechain = 0 AND total_hook_ms > 0
        GROUP BY project_path
        ORDER BY hook_ms DESC LIMIT 10
    """)
    ]


def build_project_spend(cur: sqlite3.Cursor) -> list[dict]:
    """Per-project token spend (top 10 by input+cache_creation)."""
    return [
        {
            "project": project_slug(row[0]),
            "input_tokens": row[1],
            "output_tokens": row[2],
            "cache_creation": row[3],
            "cache_read": row[4],
            "sessions": row[5],
        }
        for row in cur.execute("""
        SELECT project_path,
               SUM(total_input_tokens) as inp,
               SUM(total_output_tokens) as out,
               SUM(total_cache_creation) as cc,
               SUM(total_cache_read) as cr,
               COUNT(*) as sessions
        FROM session_metrics WHERE is_sidechain = 0
        GROUP BY project_path
        ORDER BY inp + cc DESC LIMIT 10
    """)
    ]


def build_project_tool_profile(cur: sqlite3.Cursor, project_spend: list[dict]) -> list[dict]:
    """Per-tool call counts for the top 5 most expensive projects."""
    project_tool_profile = []
    top5_projects = [p["project"] for p in project_spend[:5]]
    if not top5_projects:
        return project_tool_profile

    project_paths = {}
    for row in cur.execute("""
        SELECT project_path, SUM(total_input_tokens + total_cache_creation) as cost
        FROM session_metrics WHERE is_sidechain = 0
        GROUP BY project_path ORDER BY cost DESC LIMIT 5
    """):
        project_paths[project_slug(row[0])] = row[0]

    for proj_slug, proj_path in project_paths.items():
        tools = {}
        for row in cur.execute(
            """
            SELECT tc.tool_name, COUNT(*)
            FROM turn_tool_calls tc
            JOIN session_metrics sm ON tc.session_id = sm.session_id
            WHERE sm.project_path = ? AND sm.is_sidechain = 0
            GROUP BY tc.tool_name
            ORDER BY COUNT(*) DESC LIMIT 8
        """,
            (proj_path,),
        ):
            tools[row[0]] = row[1]
        project_tool_profile.append({"project": proj_slug, "tools": tools})

    return project_tool_profile


def build_skill_usage(cur: sqlite3.Cursor) -> tuple[list[dict], list[dict]]:
    """Skill usage totals and per-day breakdown."""
    skill_usage = [
        {"skill": row[0], "count": row[1], "errors": row[2]}
        for row in cur.execute("""
        SELECT skill_name, COUNT(*) as cnt, SUM(is_error) as errs
        FROM turn_tool_calls
        WHERE skill_name IS NOT NULL
        GROUP BY skill_name ORDER BY cnt DESC
    """)
    ]
    skill_usage_by_day = [
        {"date": row[0], "skill": row[1], "count": row[2]}
        for row in cur.execute("""
        SELECT DATE(t.timestamp) as day, tc.skill_name, COUNT(*) as cnt
        FROM turn_tool_calls tc
        JOIN turns t ON tc.turn_id = t.id
        WHERE tc.skill_name IS NOT NULL
        GROUP BY day, tc.skill_name ORDER BY day
    """)
    ]
    return skill_usage, skill_usage_by_day


def build_agent_delegation(cur: sqlite3.Cursor) -> tuple[list[dict], list[dict]]:
    """Agent delegation counts by subagent type and model distribution."""
    agent_delegation = [
        {"subagent_type": row[0], "count": row[1], "errors": row[2]}
        for row in cur.execute("""
        SELECT tc.subagent_type, COUNT(*) as cnt, SUM(tc.is_error) as errs
        FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id AND sm.is_sidechain = 0
        WHERE tc.subagent_type IS NOT NULL
        GROUP BY tc.subagent_type ORDER BY cnt DESC
    """)
    ]
    agent_model_dist = [
        {"model": row[0], "count": row[1]}
        for row in cur.execute("""
        SELECT tc.agent_model, COUNT(*) as cnt
        FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id AND sm.is_sidechain = 0
        WHERE tc.agent_model IS NOT NULL
        GROUP BY tc.agent_model ORDER BY cnt DESC
    """)
    ]
    return agent_delegation, agent_model_dist


def build_hook_performance(cur: sqlite3.Cursor) -> list[dict]:
    """Hook execution stats by command (all hooks, sorted by total ms)."""
    return [
        {
            "hook_command": row[0],
            "runs": row[1],
            "total_ms": row[2],
            "avg_ms": row[3],
            "errors": row[4],
        }
        for row in cur.execute("""
        SELECT he.hook_command, COUNT(*) as runs,
               SUM(he.duration_ms) as total_ms,
               ROUND(AVG(he.duration_ms)) as avg_ms,
               SUM(he.is_error) as errs
        FROM hook_executions he
        JOIN session_metrics sm ON he.session_id = sm.session_id AND sm.is_sidechain = 0
        GROUP BY he.hook_command ORDER BY total_ms DESC
    """)
    ]


def build_output(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()

    kpi_data = build_kpis(cur)
    total_sessions = kpi_data["total_sessions"]
    total_output = kpi_data["total_output"]
    total_input = kpi_data["total_input"]
    total_cache_cliffs = kpi_data["total_cache_cliffs"]
    total_max_token_stops = kpi_data["total_max_token_stops"]
    total_thinking = kpi_data["total_thinking"]
    total_tool_errors = kpi_data["total_tool_errors"]
    total_tool_calls = kpi_data["total_tool_calls"]
    global_cache_ratio = kpi_data["global_cache_ratio"]
    dominant_cache_tier = kpi_data["dominant_cache_tier"]
    date_range = kpi_data["date_range"]
    kpis_partial = kpi_data["kpis_partial"]

    sessions_by_day = build_sessions_by_day(cur)
    top_tools = build_top_tools(cur)
    model_split = build_model_split(cur)
    total_cost_usd = sum(m["cost_usd"] for m in model_split)
    cost_by_day = build_cost_by_day(cur)
    cost_by_project = build_cost_by_project(cur)
    cache_trajectory = build_cache_trajectory(cur)

    ctx_seg = build_context_segments(cur)
    context_segments = ctx_seg["context_segments"]
    context_segments_recent = ctx_seg["context_segments_recent"]
    context_seg_summary = ctx_seg["context_seg_summary"]

    tool_footprint = build_tool_footprint(cur)
    ephem_split = build_ephem_split(cur)

    bash_antipatterns, total_bash_antipatterns = build_bash_antipatterns(cur)
    tool_errors_by_tool = build_tool_errors_by_tool(cur)
    redundant_reads, total_redundant_reads = build_redundant_reads(cur)
    edit_retries, total_edit_retries = build_edit_retries(cur)

    agent_cost = build_agent_cost(cur)
    turn_complexity, thinking_in_complexity = build_turn_complexity(cur)
    response_time_dist = build_response_time_dist(cur)
    hook_overhead = build_hook_overhead(cur)

    project_spend = build_project_spend(cur)
    project_tool_profile = build_project_tool_profile(cur, project_spend)

    skill_usage, skill_usage_by_day = build_skill_usage(cur)
    agent_delegation, agent_model_dist = build_agent_delegation(cur)
    hook_performance = build_hook_performance(cur)

    kpis = {
        **kpis_partial,
        "bash_antipatterns": total_bash_antipatterns,
        "tool_error_rate": round(total_tool_errors / total_tool_calls, 4) if total_tool_calls else 0,
        "total_cost_usd": round(total_cost_usd, 2),
    }

    insight_data = build_insights_and_trends(
        conn,
        total_output=total_output,
        total_input=total_input,
        cache_cliffs=total_cache_cliffs,
        max_token_stops=total_max_token_stops,
        total_bash_antipatterns=total_bash_antipatterns,
        redundant_reads_count=total_redundant_reads,
        edit_retries_count=total_edit_retries,
        total_thinking=total_thinking,
        total_tool_errors=total_tool_errors,
        global_cache_ratio=global_cache_ratio,
        total_sessions=total_sessions,
        response_time_dist=response_time_dist,
        bash_antipattern_projects=bash_antipatterns[:3],
        top_redundant_files=redundant_reads[:3],
        edit_retry_projects=edit_retries[:3],
        cost_by_project=cost_by_project,
        total_cost_usd=total_cost_usd,
        context_seg_summary=context_seg_summary,
        dominant_cache_tier=dominant_cache_tier,
        model_split=model_split,
    )

    return {
        "generated_at": Instant.now().format_iso(),
        "total_sessions": total_sessions,
        "date_range": date_range,
        "kpis": kpis,
        "sessions_by_day": sessions_by_day,
        "top_tools": top_tools,
        "model_split": model_split,
        "cost_by_day": cost_by_day,
        "cost_by_project": cost_by_project,
        "cache_trajectory": cache_trajectory,
        "context_segments": context_segments,
        "context_segments_recent": context_segments_recent,
        "tool_footprint": tool_footprint,
        "context_seg_summary": context_seg_summary,
        "ephem_split": ephem_split,
        "bash_antipatterns": bash_antipatterns,
        "tool_errors_by_tool": tool_errors_by_tool,
        "redundant_reads": redundant_reads,
        "edit_retries": edit_retries,
        "agent_cost": agent_cost,
        "turn_complexity": turn_complexity,
        "thinking_in_complexity": thinking_in_complexity,
        "response_time_dist": response_time_dist,
        "hook_overhead": hook_overhead,
        "project_spend": project_spend,
        "project_tool_profile": project_tool_profile,
        "skill_usage": skill_usage,
        "skill_usage_by_day": skill_usage_by_day,
        "agent_delegation": agent_delegation,
        "agent_model_dist": agent_model_dist,
        "hook_performance": hook_performance,
        **insight_data,
    }
