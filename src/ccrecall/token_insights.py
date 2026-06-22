"""
token_insights — Trend analysis, insight generation, and findings/recommendations
for the token ingest pipeline.
"""

import sqlite3
from dataclasses import asdict, dataclass

from ccrecall.token_parser import (
    _BASH_ANTIPATTERN_PREDICATE,
    get_pricing,
    project_slug,
    row_cost,
)

# Per-insight waste-token estimates (tokens wasted per occurrence).
WASTE_TOKENS_PER_CACHE_CLIFF = 15000
WASTE_TOKENS_PER_MAX_TOKEN_STOP = 5000
WASTE_TOKENS_PER_BASH_ANTIPATTERN = 200
WASTE_TOKENS_PER_REDUNDANT_READ = 500
WASTE_TOKENS_PER_EDIT_RETRY = 300
WASTE_TOKENS_PER_IDLE_GAP = 2000

# Fallback per-million-token cost rates when no model split is available.
FALLBACK_INPUT_CPM = 5.0
FALLBACK_OUTPUT_CPM = 25.0

# Standalone Bash commands mapped to their dedicated-tool replacement (for the antipattern insight).
BASH_TOOL_MAP = {
    "cat": "Read",
    "head": "Read (with offset+limit)",
    "tail": "Read (with offset+limit)",
    "grep": "Grep",
    "find": "Glob",
    "ls": "Glob or Bash(ls)",
}

# Window clause strings for window_kpis (wrap column in datetime() for consistent comparison).
CURRENT_WINDOW_CLAUSE = "datetime(sm.first_turn_ts) >= datetime('now', '-7 days')"
PRIOR_WINDOW_CLAUSE = (
    "datetime(sm.first_turn_ts) >= datetime('now', '-14 days') "
    "AND datetime(sm.first_turn_ts) < datetime('now', '-7 days')"
)

# Bare-column variants for the skill/hook set-diff and hook-perf queries. These deliberately
# omit the datetime() wrapper above (preserving the original SQL text for those queries). Kept
# as constants alongside the wrapped pair so the 7/14-day window has one source of truth — change
# the interval here and both query families move together.
SET_DIFF_CURRENT_CLAUSE = "sm.first_turn_ts >= datetime('now', '-7 days')"
SET_DIFF_PRIOR_CLAUSE = (
    "sm.first_turn_ts >= datetime('now', '-14 days') AND sm.first_turn_ts < datetime('now', '-7 days')"
)


@dataclass
class Solution:
    action: str
    detail: str
    claudemd_rule: str | None
    estimated_savings_usd: float


@dataclass
class Insight:
    title: str
    severity: str
    finding: str
    root_cause: str
    waste_tokens: int
    waste_usd: float
    solution: Solution
    priority: str = ""


def compute_waste_usd(tokens: int, avg_input_rate: float, avg_output_rate: float, is_output: bool = False) -> float:
    rate = avg_output_rate if is_output else avg_input_rate
    return round(tokens * rate / 1_000_000, 2)


def compute_severity(count: int, sessions: int, high_rate: float, crit_rate: float) -> str:
    rate = count / sessions
    if rate >= crit_rate:
        return "CRITICAL"
    if rate >= high_rate:
        return "WARNING"
    return "INFO"


def build_cache_cliffs_insight(
    cache_cliffs: int,
    sessions: int,
    avg_input_rate: float,
    avg_output_rate: float,
    dominant_cache_tier: str,
    cache_cliff_projects: list[dict],
) -> Insight | None:
    if cache_cliffs <= 0:
        return None
    tier = dominant_cache_tier
    ttl_label = "5 minutes" if tier == "5m" else "1 hour"
    waste_tok = cache_cliffs * WASTE_TOKENS_PER_CACHE_CLIFF
    waste_dollars = compute_waste_usd(waste_tok, avg_input_rate, avg_output_rate)
    top_proj = cache_cliff_projects[0] if cache_cliff_projects else None
    root_cause = (
        f"Worst: {top_proj['project']} ({top_proj['cliffs']} cliffs across {top_proj['sessions']} sessions)"
        if top_proj
        else "Distributed across projects"
    )
    return Insight(
        title="Cache Cliffs",
        severity=compute_severity(cache_cliffs, sessions, 0.1, 0.4),
        finding=f"{cache_cliffs} cache cliffs detected — cache_read_ratio dropped >50% after {ttl_label}+ idle gaps.",
        root_cause=f"When you're idle >{ttl_label}, Anthropic's prompt cache expires ({tier} tier). "
        f"The next turn re-creates the entire cache from scratch. {root_cause}.",
        waste_tokens=waste_tok,
        waste_usd=waste_dollars,
        solution=Solution(
            action="Run /compact before stepping away from a session",
            detail="Compacting reduces context size so cache re-creation is cheaper when you return. "
            "For planned breaks, also consider ending the session and starting fresh.",
            claudemd_rule=None,
            estimated_savings_usd=round(waste_dollars * 0.6, 2),
        ),
    )


def build_max_token_stops_insight(
    max_token_stops: int,
    sessions: int,
    avg_input_rate: float,
    avg_output_rate: float,
) -> Insight | None:
    if max_token_stops <= 0:
        return None
    waste_tok = max_token_stops * WASTE_TOKENS_PER_MAX_TOKEN_STOP
    waste_dollars = compute_waste_usd(waste_tok, avg_input_rate, avg_output_rate, is_output=True)
    return Insight(
        title="Context Pressure",
        severity=compute_severity(max_token_stops, sessions, 0.02, 0.1),
        finding=f"{max_token_stops} turns hit max_tokens — model was cut off mid-response.",
        root_cause="The conversation context exceeded the model's output budget. This typically "
        "happens in long sessions with many tool calls, large file reads, or when "
        "CLAUDE.md/hooks inject significant context every turn.",
        waste_tokens=waste_tok,
        waste_usd=waste_dollars,
        solution=Solution(
            action="Run /compact proactively when sessions exceed ~40 turns, or split into smaller sessions",
            detail="Monitor turn count. If you're doing a large refactor, break it into focused sessions "
            "(one per file/module) rather than one marathon session.",
            claudemd_rule=None,
            estimated_savings_usd=round(waste_dollars * 0.8, 2),
        ),
    )


def suggest_bash_claudemd_rule(top_bash_antipattern_cmds: list[dict]) -> str:
    """Derive a CLAUDE.md rule suggestion from the top antipattern commands, or a generic fallback."""
    suggested_rules = []
    seen_prefixes = set()
    for cmd in top_bash_antipattern_cmds[:3]:
        prefix = cmd["command"].split()[0] if cmd.get("command") else ""
        replacement = BASH_TOOL_MAP.get(prefix)
        if replacement and prefix not in seen_prefixes:
            suggested_rules.append(f"Use {replacement} instead of `{prefix}` command")
            seen_prefixes.add(prefix)
    if suggested_rules:
        return "\n".join(suggested_rules)
    return (
        "Use Read instead of standalone cat/head/tail. Use Grep instead of standalone grep. "
        "Use Glob instead of standalone find/ls."
    )


def build_bash_antipatterns_insight(
    bash_antipatterns: int,
    sessions: int,
    avg_input_rate: float,
    avg_output_rate: float,
    bash_antipattern_projects: list[dict],
    top_bash_antipattern_cmds: list[dict],
) -> Insight | None:
    if bash_antipatterns <= 0:
        return None
    waste_tok = bash_antipatterns * WASTE_TOKENS_PER_BASH_ANTIPATTERN
    waste_dollars = compute_waste_usd(waste_tok, avg_input_rate, avg_output_rate, is_output=True)
    proj_detail = (
        "; ".join(f"{b['project']}: {b['antipatterns']}" for b in bash_antipattern_projects[:3])
        if bash_antipattern_projects
        else "unknown"
    )
    cmd_detail = (
        ", ".join(f"`{c['command'][:50]}` ({c['count']}x)" for c in top_bash_antipattern_cmds[:3])
        if top_bash_antipattern_cmds
        else "various cat/grep/find/ls calls"
    )
    claudemd_rule = suggest_bash_claudemd_rule(top_bash_antipattern_cmds)
    return Insight(
        title="Bash Antipatterns",
        severity=compute_severity(bash_antipatterns, sessions, 0.5, 2.0),
        finding=f"{bash_antipatterns} Bash calls use standalone cat/grep/find/ls where a "
        f"dedicated tool (Read, Grep, Glob) exists. Legitimate pipeline feeders, "
        f"existence checks, and time-sorted ls are excluded.",
        root_cause=f"Claude is choosing Bash for standalone file reads/searches. "
        f"Top projects: {proj_detail}. Top commands: {cmd_detail}.",
        waste_tokens=waste_tok,
        waste_usd=waste_dollars,
        solution=Solution(
            action="Reinforce CLAUDE.md rule for standalone tool use",
            detail="Dedicated tools return structured output with fewer tokens than raw shell. "
            "A blanket PreToolUse enforcement hook would have high false-positive rate — "
            "pipelines, existence checks, and stat operations are legitimate Bash uses. "
            "CLAUDE.md guidance is sufficient for standalone cases.",
            claudemd_rule=claudemd_rule,
            estimated_savings_usd=round(waste_dollars * 0.7, 2),
        ),
    )


def build_redundant_reads_insight(
    redundant_reads: int,
    sessions: int,
    avg_input_rate: float,
    avg_output_rate: float,
    top_redundant_files: list[dict],
) -> Insight | None:
    if redundant_reads <= 0:
        return None
    waste_tok = redundant_reads * WASTE_TOKENS_PER_REDUNDANT_READ
    waste_dollars = compute_waste_usd(waste_tok, avg_input_rate, avg_output_rate)
    file_detail = (
        "; ".join(f"`{f['file']}` read {f['count']}x in session {f['session_id']}" for f in top_redundant_files[:3])
        if top_redundant_files
        else "various files"
    )
    return Insight(
        title="Redundant Reads",
        severity=compute_severity(redundant_reads, sessions, 0.3, 1.0),
        finding=f"{redundant_reads} extra file reads (same file read 3+ times in a session).",
        root_cause=f"Claude is re-reading files it already has in context. This happens when earlier "
        f"context gets compressed away or when Claude doesn't trust its cached knowledge. "
        f"Worst offenders: {file_detail}.",
        waste_tokens=waste_tok,
        waste_usd=waste_dollars,
        solution=Solution(
            action="Add a CLAUDE.md rule: 'After reading a file, reference it from context — "
            "do not re-read unless the file was modified since last read'",
            detail="Each redundant read re-ingests the file as input tokens. For large files (1K+ lines) "
            "this adds significant cost. The Read tool output note already says 'content unchanged "
            "since last read' but Claude sometimes ignores it.",
            claudemd_rule="After reading a file, reference it from context. "
            "Only re-read if the file was modified since last read.",
            estimated_savings_usd=round(waste_dollars * 0.5, 2),
        ),
    )


def build_edit_retries_insight(
    edit_retries: int,
    sessions: int,
    avg_input_rate: float,
    avg_output_rate: float,
    edit_retry_projects: list[dict],
) -> Insight | None:
    if edit_retries <= 0:
        return None
    waste_tok = edit_retries * WASTE_TOKENS_PER_EDIT_RETRY
    waste_dollars = compute_waste_usd(waste_tok, avg_input_rate, avg_output_rate, is_output=True)
    proj_detail = (
        "; ".join(f"{r['project']}: {r['retries']}x" for r in edit_retry_projects[:3])
        if edit_retry_projects
        else "various projects"
    )
    return Insight(
        title="Edit Retry Chains",
        severity=compute_severity(edit_retries, sessions, 0.2, 0.5),
        finding=f"{edit_retries} failed-edit retry chains detected.",
        root_cause=f"An Edit call fails (usually unique-match failure), then Claude retries on the same "
        f"file next turn. The failure means Claude's mental model of the file diverged from "
        f"reality — typically after a prior edit changed the file. Projects: {proj_detail}.",
        waste_tokens=waste_tok,
        waste_usd=waste_dollars,
        solution=Solution(
            action="Add a CLAUDE.md rule: 'Always read a file before editing "
            "if more than 2 turns have passed since last read'",
            detail="The root cause is stale context. Claude edits based on what it remembers, not "
            "the current file state. A fresh read before each edit is cheap (~500 input tokens) "
            "compared to the cost of a failed edit + retry (~300 output tokens wasted).",
            claudemd_rule="Read file before editing if >2 turns since last read, to avoid stale-context edit failures.",
            estimated_savings_usd=round(waste_dollars * 0.7, 2),
        ),
    )


def build_thinking_insight(
    total_thinking: int,
    total_output: int,
    avg_input_rate: float,
    avg_output_rate: float,
) -> Insight | None:
    if total_thinking <= 0:
        return None
    pct = round(total_thinking / total_output * 100, 1) if total_output else 0
    thinking_dollars = compute_waste_usd(total_thinking, avg_input_rate, avg_output_rate, is_output=True)
    return Insight(
        title="Thinking Token Overhead",
        severity="INFO",
        finding=f"~{pct}% of output tokens went to extended thinking "
        f"({total_thinking // 1000}K tokens, ~${thinking_dollars}).",
        root_cause="Extended thinking is Opus's reasoning mode — it produces internal chain-of-thought "
        "tokens that are billed as output but not visible to you. This is expected for complex "
        "tasks but can be excessive for simple ones.",
        waste_tokens=0,
        waste_usd=0,
        solution=Solution(
            action="Use Sonnet for routine tasks (file reads, simple edits, git operations) "
            "and reserve Opus for complex reasoning",
            detail=f"Thinking tokens cost ${thinking_dollars} at output rates. If you're using Opus for "
            f"everything, switching routine tasks to Sonnet (which doesn't use extended thinking) "
            f"could save 50-70% of this cost.",
            claudemd_rule=None,
            estimated_savings_usd=round(thinking_dollars * 0.3, 2),
        ),
    )


def build_idle_gap_insight(
    response_time_dist: dict,
    dominant_cache_tier: str,
    sessions: int,
    avg_input_rate: float,
    avg_output_rate: float,
) -> Insight | None:
    tier = dominant_cache_tier
    ttl_label = "5 minutes" if tier == "5m" else "1 hour"
    if tier == "5m":
        idle_over_ttl = (
            response_time_dist.get("5m_15m", 0)
            + response_time_dist.get("15m_1h", 0)
            + response_time_dist.get("over_1h", 0)
        )
    else:
        idle_over_ttl = response_time_dist.get("over_1h", 0)
    if idle_over_ttl <= 0:
        return None
    total_gaps = sum(response_time_dist.values())
    pct = round(idle_over_ttl / total_gaps * 100, 1) if total_gaps else 0
    waste_tok = idle_over_ttl * WASTE_TOKENS_PER_IDLE_GAP
    waste_dollars = compute_waste_usd(waste_tok, avg_input_rate, avg_output_rate)
    return Insight(
        title="Idle Gap Impact",
        severity=compute_severity(idle_over_ttl, sessions, 0.3, 1.0),
        finding=f"{pct}% of turns follow {ttl_label}+ idle gaps ({idle_over_ttl} of {total_gaps}).",
        root_cause=f"Anthropic's prompt cache has a {ttl_label} TTL ({tier} tier). After "
        f"{ttl_label} of inactivity, the cached context expires and must be "
        f"re-created from scratch on the next turn.",
        waste_tokens=waste_tok,
        waste_usd=waste_dollars,
        solution=Solution(
            action="Run /compact before stepping away from a session",
            detail=f"Compacting reduces context size so cache re-creation is cheaper when you return "
            f"after {ttl_label}+ of inactivity. For planned breaks, also consider ending the "
            f"session and starting fresh.",
            claudemd_rule=None,
            estimated_savings_usd=round(waste_dollars * 0.4, 2),
        ),
    )


def build_cost_concentration_insight(
    cost_by_project: list[dict],
    total_cost_usd: float,
) -> Insight | None:
    if not cost_by_project or total_cost_usd <= 0:
        return None
    top = cost_by_project[0]
    top_pct = round(top["cost_usd"] / total_cost_usd * 100, 1)
    if top_pct <= 40:
        return None
    return Insight(
        title="Cost Concentration",
        severity="INFO",
        finding=f"{top['project']} accounts for {top_pct}% of total spend "
        f"(${top['cost_usd']:.2f} of ${total_cost_usd:.2f}).",
        root_cause="This project dominates your usage. Either it has the most sessions, uses "
        "the most expensive model, or both. Review whether all work in this project "
        "requires the current model tier.",
        waste_tokens=0,
        waste_usd=0,
        solution=Solution(
            action=f"Audit {top['project']} sessions — could routine tasks use Sonnet instead of Opus?",
            detail=f"Switching from Opus ($5/$25 per MTok) to Sonnet ($3/$15 per MTok) for 50% of "
            f"{top['project']} work would save ~${top['cost_usd'] * 0.2:.2f}.",
            claudemd_rule=None,
            estimated_savings_usd=round(top["cost_usd"] * 0.2, 2),
        ),
    )


def build_context_overhead_insight(
    context_seg_summary: dict,
    sessions: int,
    avg_input_rate: float,
    avg_output_rate: float,
) -> Insight | None:
    base_pct = context_seg_summary.get("base_overhead_pct", 0)
    avg_base = context_seg_summary.get("avg_base_ctx", 0)
    if base_pct <= 30 or avg_base <= 0:
        return None
    reducible_base = max(0, avg_base - 20000)
    waste_tok = reducible_base * sessions
    waste_dollars = compute_waste_usd(waste_tok, avg_input_rate, avg_output_rate)
    return Insight(
        title="Context Overhead",
        severity="WARNING" if base_pct > 40 else "INFO",
        finding=f"{base_pct}% of all context tokens are base overhead repeated every turn. "
        f"Average base context: {avg_base:,} tokens.",
        root_cause="Every turn rebuilds the full prompt: system instructions, tool schemas, "
        "CLAUDE.md, memory injections, skill descriptions, and MCP schemas. "
        "This base payload is cached when cache hits, but counts against rate limits "
        "and pays full price whenever cache expires.",
        waste_tokens=waste_tok,
        waste_usd=waste_dollars,
        solution=Solution(
            action="Enable ENABLE_TOOL_SEARCH to defer tool schemas, trim CLAUDE.md, and disable unused skills",
            detail=f"Your avg base is {avg_base:,} tokens. "
            "Tool schemas typically account for 14-20K of that. "
            f"With deferred loading + pruning unused skills, base can drop to ~20K. "
            f"Every 1K tokens trimmed from base saves that amount on every turn of every session.",
            claudemd_rule=None,
            estimated_savings_usd=round(waste_dollars * 0.4, 2),
        ),
    )


def build_insights(**kw) -> list[dict]:
    """Build unified insights: finding + root cause + solution + dollar cost.

    Each insight is a complete diagnosis-to-action unit. Severity is
    rate-normalized (per-session) not absolute count.
    """
    sessions = kw["total_sessions"] or 1
    avg_input_rate = kw.get("avg_input_cost_per_mtok", FALLBACK_INPUT_CPM)
    avg_output_rate = kw.get("avg_output_cost_per_mtok", FALLBACK_OUTPUT_CPM)

    builders = [
        build_cache_cliffs_insight(
            kw["cache_cliffs"],
            sessions,
            avg_input_rate,
            avg_output_rate,
            kw.get("dominant_cache_tier", "1h"),
            kw.get("cache_cliff_projects", []),
        ),
        build_max_token_stops_insight(kw["max_token_stops"], sessions, avg_input_rate, avg_output_rate),
        build_bash_antipatterns_insight(
            kw["bash_antipatterns"],
            sessions,
            avg_input_rate,
            avg_output_rate,
            kw.get("bash_antipattern_projects", []),
            kw.get("top_bash_antipattern_cmds", []),
        ),
        build_redundant_reads_insight(
            kw["redundant_reads"],
            sessions,
            avg_input_rate,
            avg_output_rate,
            kw.get("top_redundant_files", []),
        ),
        build_edit_retries_insight(
            kw["edit_retries"],
            sessions,
            avg_input_rate,
            avg_output_rate,
            kw.get("edit_retry_projects", []),
        ),
        build_thinking_insight(kw["total_thinking"], kw["total_output"], avg_input_rate, avg_output_rate),
        build_idle_gap_insight(
            kw["response_time_dist"],
            kw.get("dominant_cache_tier", "1h"),
            sessions,
            avg_input_rate,
            avg_output_rate,
        ),
        build_cost_concentration_insight(kw.get("cost_by_project", []), kw.get("total_cost_usd", 0)),
        build_context_overhead_insight(
            kw.get("context_seg_summary", {}),
            sessions,
            avg_input_rate,
            avg_output_rate,
        ),
    ]

    insights = [i for i in builders if i is not None]
    insights.sort(key=lambda i: (i.waste_usd > 0, i.waste_usd), reverse=True)

    for idx, ins in enumerate(insights):
        if idx < 2:
            ins.priority = "P0"
        elif idx < 5:
            ins.priority = "P1"
        else:
            ins.priority = "P2"

    return [asdict(i) for i in insights]


def insights_to_findings(insights: list[dict]) -> list[dict]:
    return [
        {
            "title": i["title"],
            "severity": i["severity"],
            "text": f"{i['finding']} {i['root_cause']}",
            "waste": i["waste_tokens"],
        }
        for i in insights
    ]


def insights_to_recommendations(insights: list[dict]) -> list[dict]:
    return [
        {
            "text": f"{i['solution']['action']}. {i['solution']['detail']}",
            "impact": i["waste_tokens"],
            "priority": i["priority"],
        }
        for i in insights
    ]


def window_tool_stats(cur: sqlite3.Cursor, where_clause: str) -> tuple[int, int]:
    """Return (total_tool_calls, bash_antipatterns) for a window on session_metrics.first_turn_ts."""
    total_tool_calls = (
        cur.execute(f"""
        SELECT COUNT(*) FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id
        WHERE sm.is_sidechain = 0 AND {where_clause}
    """).fetchone()[0]
        or 0
    )
    bash_antipatterns = (
        cur.execute(f"""
        SELECT SUM(CASE WHEN {_BASH_ANTIPATTERN_PREDICATE} THEN 1 ELSE 0 END)
        FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id
        WHERE sm.is_sidechain = 0 AND {where_clause}
    """).fetchone()[0]
        or 0
    )
    return total_tool_calls, bash_antipatterns


def window_cost(cur: sqlite3.Cursor, where_clause: str) -> float:
    """Sum per-model turn cost for a window on session_metrics.first_turn_ts."""
    return sum(
        row_cost(crow, model_idx=0, token_indices=[1, 2, 3, 4, 5, 6])
        for crow in cur.execute(f"""
            SELECT t.model,
                   SUM(t.input_tokens), SUM(t.output_tokens),
                   SUM(t.cache_read_tokens), SUM(t.cache_creation_tokens),
                   SUM(t.ephem_5m_tokens), SUM(t.ephem_1h_tokens)
            FROM turns t
            JOIN session_metrics sm ON t.session_id = sm.session_id
            WHERE sm.is_sidechain = 0 AND {where_clause}
            GROUP BY t.model
        """)
    )


def window_kpis(cur: sqlite3.Cursor, where_clause: str) -> dict | None:
    """Compute KPIs for a time window defined by where_clause on session_metrics.first_turn_ts."""
    row = cur.execute(f"""
        SELECT COUNT(*), SUM(turn_count),
               SUM(total_cache_read), SUM(total_cache_creation),
               SUM(cache_cliff_count), SUM(tool_error_count),
               SUM(total_hook_ms)
        FROM session_metrics sm
        WHERE is_sidechain = 0 AND {where_clause}
    """).fetchone()
    sessions = row[0] or 0
    if sessions == 0:
        return None
    turns = row[1] or 0
    cache_read = row[2] or 0
    cache_creation = row[3] or 0
    cliffs = row[4] or 0
    tool_errors = row[5] or 0
    hook_ms = row[6] or 0

    cache_denom = cache_read + cache_creation
    cache_ratio = round(cache_read / cache_denom, 4) if cache_denom > 0 else 0.0

    total_tool_calls, bash_antipatterns = window_tool_stats(cur, where_clause)
    cost = window_cost(cur, where_clause)

    return {
        "sessions": sessions,
        "turns": turns,
        "cost_usd": round(cost, 2),
        "cost_per_session": round(cost / sessions, 2),
        "cache_ratio": cache_ratio,
        "cliffs_per_session": round(cliffs / sessions, 3),
        "antipatterns_per_session": round(bash_antipatterns / sessions, 2),
        "tool_error_rate": round(tool_errors / total_tool_calls, 4) if total_tool_calls else 0,
        "hook_avg_ms": round(hook_ms / turns, 1) if turns else 0,
    }


def skill_set_diff(cur: sqlite3.Cursor, current_clause: str, prior_clause: str) -> tuple[list, list]:
    """Compute new/retired sets for skills over two time windows.

    Returns (new_skills, retired_skills) as sorted lists.
    """
    current_set = {
        row[0]
        for row in cur.execute(f"""
        SELECT tc.skill_name FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id
        WHERE sm.is_sidechain = 0 AND tc.skill_name IS NOT NULL
          AND {current_clause}
        GROUP BY tc.skill_name
    """)
    }
    prior_set = {
        row[0]
        for row in cur.execute(f"""
        SELECT tc.skill_name FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id
        WHERE sm.is_sidechain = 0 AND tc.skill_name IS NOT NULL
          AND {prior_clause}
        GROUP BY tc.skill_name
    """)
    }
    return sorted(current_set - prior_set), sorted(prior_set - current_set)


def hook_set_diff(cur: sqlite3.Cursor, current_clause: str, prior_clause: str) -> tuple[list, list]:
    """Compute new/retired sets for hooks over two time windows.

    Returns (new_hooks, retired_hooks) as sorted lists.
    """
    current_set = {
        row[0]
        for row in cur.execute(f"""
        SELECT he.hook_command FROM hook_executions he
        JOIN session_metrics sm ON he.session_id = sm.session_id
        WHERE sm.is_sidechain = 0
          AND {current_clause}
        GROUP BY he.hook_command
    """)
    }
    prior_set = {
        row[0]
        for row in cur.execute(f"""
        SELECT he.hook_command FROM hook_executions he
        JOIN session_metrics sm ON he.session_id = sm.session_id
        WHERE sm.is_sidechain = 0
          AND {prior_clause}
        GROUP BY he.hook_command
    """)
    }
    return sorted(current_set - prior_set), sorted(prior_set - current_set)


def hook_perf_trends(cur: sqlite3.Cursor, current_clause: str, prior_clause: str) -> list[dict]:
    """Compute per-hook avg ms for current vs prior window, returning change_pct."""
    current_perf = dict(
        cur.execute(f"""
        SELECT he.hook_command, CAST(AVG(he.duration_ms) AS INT)
        FROM hook_executions he
        JOIN session_metrics sm ON he.session_id = sm.session_id
        WHERE sm.is_sidechain = 0 AND {current_clause}
        GROUP BY he.hook_command
    """)
    )
    prior_perf = dict(
        cur.execute(f"""
        SELECT he.hook_command, CAST(AVG(he.duration_ms) AS INT)
        FROM hook_executions he
        JOIN session_metrics sm ON he.session_id = sm.session_id
        WHERE sm.is_sidechain = 0
          AND {prior_clause}
        GROUP BY he.hook_command
    """)
    )
    result = []
    for hook_cmd in sorted(set(current_perf) | set(prior_perf)):
        current_ms = current_perf.get(hook_cmd)
        prior_ms = prior_perf.get(hook_cmd)
        change_pct = None
        if current_ms is not None and prior_ms is not None and prior_ms > 0:
            change_pct = round((current_ms - prior_ms) / prior_ms * 100, 1)
        result.append({"hook": hook_cmd, "current_ms": current_ms, "prior_ms": prior_ms, "change_pct": change_pct})
    return result


def build_insights_and_trends(
    conn: sqlite3.Connection,
    *,
    total_output: int,
    total_input: int,
    cache_cliffs: int,
    max_token_stops: int,
    total_bash_antipatterns: int,
    redundant_reads_count: int,
    edit_retries_count: int,
    total_thinking: int,
    total_tool_errors: int,
    global_cache_ratio: float,
    total_sessions: int,
    response_time_dist: dict,
    bash_antipattern_projects: list[dict],
    top_redundant_files: list[dict],
    edit_retry_projects: list[dict],
    cost_by_project: list[dict],
    total_cost_usd: float,
    context_seg_summary: dict,
    dominant_cache_tier: str,
    model_split: list[dict],
) -> dict:
    """Run root-cause queries, build insights, and compute trends."""
    cur = conn.cursor()

    # Root-cause detail: cache cliffs by project
    cache_cliff_projects = [
        {"project": project_slug(row[0]), "cliffs": row[1], "sessions": row[2]}
        for row in cur.execute("""
        SELECT project_path, SUM(cache_cliff_count) as cliffs, COUNT(*) as sessions
        FROM session_metrics WHERE is_sidechain = 0 AND cache_cliff_count > 0
        GROUP BY project_path ORDER BY cliffs DESC LIMIT 5
    """)
    ]

    # Root-cause detail: top antipattern commands
    top_bash_cmds = [
        {"command": row[0], "count": row[1]}
        for row in cur.execute(f"""
        SELECT SUBSTR(tc.command, 1, 60) as cmd, COUNT(*) as cnt
        FROM turn_tool_calls tc
        JOIN session_metrics sm ON tc.session_id = sm.session_id AND sm.is_sidechain = 0
        WHERE {_BASH_ANTIPATTERN_PREDICATE}
        GROUP BY cmd ORDER BY cnt DESC LIMIT 5
    """)
    ]

    # Weighted average cost rates for waste-to-dollar conversion
    avg_input_cpm = FALLBACK_INPUT_CPM
    avg_output_cpm = FALLBACK_OUTPUT_CPM
    if model_split:
        weighted_in = sum(m["input_tokens"] * get_pricing(m["model"])["input"] for m in model_split)
        weighted_out = sum(m["output_tokens"] * get_pricing(m["model"])["output"] for m in model_split)
        total_in = sum(m["input_tokens"] for m in model_split) or 1
        total_out = sum(m["output_tokens"] for m in model_split) or 1
        avg_input_cpm = weighted_in / total_in
        avg_output_cpm = weighted_out / total_out

    insights = build_insights(
        total_output=total_output,
        total_input=total_input,
        cache_cliffs=cache_cliffs,
        max_token_stops=max_token_stops,
        bash_antipatterns=total_bash_antipatterns,
        redundant_reads=redundant_reads_count,
        edit_retries=edit_retries_count,
        total_thinking=total_thinking,
        total_tool_errors=total_tool_errors,
        global_cache_ratio=global_cache_ratio,
        total_sessions=total_sessions,
        response_time_dist=response_time_dist,
        bash_antipattern_projects=bash_antipattern_projects,
        top_bash_antipattern_cmds=top_bash_cmds,
        cache_cliff_projects=cache_cliff_projects,
        top_redundant_files=top_redundant_files,
        edit_retry_projects=edit_retry_projects,
        cost_by_project=cost_by_project,
        total_cost_usd=total_cost_usd,
        avg_input_cost_per_mtok=avg_input_cpm,
        avg_output_cost_per_mtok=avg_output_cpm,
        context_seg_summary=context_seg_summary,
        dominant_cache_tier=dominant_cache_tier,
    )

    findings = insights_to_findings(insights)
    recommendations = insights_to_recommendations(insights)
    trends = build_trends(conn)

    return {
        "insights": insights,
        "findings": findings,
        "recommendations": recommendations,
        "trends": trends,
    }


def build_trends(conn: sqlite3.Connection) -> dict:
    """Compute week-on-week deltas for key metrics.

    Splits data into two 7-day windows: 'current' (last 7 days) and
    'prior' (7-14 days ago). Returns per-metric current/prior/change_pct
    plus classified improved/regressed/new/retired lists.
    """
    cur = conn.cursor()

    current = window_kpis(cur, CURRENT_WINDOW_CLAUSE)
    prior = window_kpis(cur, PRIOR_WINDOW_CLAUSE)

    if not current:
        return {}

    metrics = {}
    compare_keys = [
        ("cost_per_session", "Cost/Session", True),
        ("cache_ratio", "Cache Ratio", False),
        ("cliffs_per_session", "Cache Cliffs/Session", True),
        ("antipatterns_per_session", "Bash Antipatterns/Session", True),
        ("tool_error_rate", "Tool Error Rate", True),
        ("hook_avg_ms", "Hook Avg Latency", True),
    ]

    improved = []
    regressed = []

    for key, label, lower_is_better in compare_keys:
        cur_val = current.get(key, 0)
        pri_val = prior.get(key, 0) if prior else None
        change_pct = None
        if pri_val is not None and pri_val != 0:
            change_pct = round((cur_val - pri_val) / abs(pri_val) * 100, 1)
        elif pri_val == 0 and cur_val != 0:
            change_pct = 100.0

        metrics[key] = {
            "label": label,
            "current": cur_val,
            "prior": pri_val,
            "change_pct": change_pct,
        }

        if change_pct is not None and abs(change_pct) > 5:
            got_better = (change_pct < 0) if lower_is_better else (change_pct > 0)
            if got_better:
                improved.append(f"{label}: {change_pct:+.1f}%")
            else:
                regressed.append(f"{label}: {change_pct:+.1f}%")

    new_skills, retired_skills = skill_set_diff(cur, SET_DIFF_CURRENT_CLAUSE, SET_DIFF_PRIOR_CLAUSE)
    new_hooks, retired_hooks = hook_set_diff(cur, SET_DIFF_CURRENT_CLAUSE, SET_DIFF_PRIOR_CLAUSE)
    hook_trends = hook_perf_trends(cur, SET_DIFF_CURRENT_CLAUSE, SET_DIFF_PRIOR_CLAUSE)

    return {
        "window_days": 7,
        "current_window": current,
        "prior_window": prior,
        "metrics": metrics,
        "improved": improved,
        "regressed": regressed,
        "new_skills": new_skills,
        "retired_skills": retired_skills,
        "new_hooks": new_hooks,
        "retired_hooks": retired_hooks,
        "hook_trends": hook_trends,
    }
