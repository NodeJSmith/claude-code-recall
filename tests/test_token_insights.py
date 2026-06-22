"""Tests for token_insights: trend windows and insight generation."""

from token_helpers import TOKEN_JNL, token_session, token_turn
from whenever import Instant

from ccrecall.token_analytics import import_session
from ccrecall.token_insights import build_insights_and_trends, build_trends


def _session_at(sid: str, days_ago: int):
    """A one-turn session whose timestamp lands `days_ago` before now."""
    ts = Instant.now().subtract(hours=24 * days_ago).format_iso()
    return token_session(sid, [token_turn(1, timestamp=ts, input_tokens=100, output_tokens=50)])


def _insight_kwargs(**overrides):
    """A complete kwargs set for build_insights_and_trends, all signals off.

    The count/rate signals below are what fire insights when nonzero; the
    structural args after the blank line just shape output and never trigger one.
    """
    base = dict(
        total_output=0,
        total_input=0,
        cache_cliffs=0,
        max_token_stops=0,
        total_bash_antipatterns=0,
        redundant_reads_count=0,
        edit_retries_count=0,
        total_thinking=0,
        total_tool_errors=0,
        global_cache_ratio=0.0,
        total_cost_usd=0.0,
        total_sessions=10,
        response_time_dist={},
        bash_antipattern_projects=[],
        top_redundant_files=[],
        edit_retry_projects=[],
        cost_by_project=[],
        context_seg_summary={},
        dominant_cache_tier="5m",
        model_split=[],
    )
    base.update(overrides)
    return base


# ── Golden snapshots ──────────────────────────────────────────────────────────

# All-off case — every signal at zero, no insights fire.
_GOLDEN_INSIGHTS_ALL_OFF = {
    "insights": [],
    "findings": [],
    "recommendations": [],
    "trends": {},
}

# Multi-insight case.
# Inputs chosen so that cache_cliffs, max_token_stops, bash_antipatterns,
# redundant_reads, edit_retries, thinking, and cost_concentration all fire,
# pinning the proj_detail / file_detail / cmd_detail formatting branches.
# Dollar amounts are deterministic: model_split=[] so fallback rates apply:
#   FALLBACK_INPUT_CPM=5.0, FALLBACK_OUTPUT_CPM=25.0  (per-million tokens)
# cache_cliffs waste: 5 * 15000 = 75000 tok → 75000*5/1e6 = $0.375 → round=0.38; savings 0.38*0.6=0.228→0.23
# max_token_stops waste: 2 * 5000 = 10000 tok (output) → 10000*25/1e6 = $0.25; savings 0.25*0.8=0.2
# bash_antipatterns waste: 9 * 200 = 1800 tok (output) → 1800*25/1e6 = $0.045 → round=0.04 (wait $0.045→0.04); savings 0.04*0.7=0.028→0.03
# redundant_reads waste: 4 * 500 = 2000 tok (input) → 2000*5/1e6 = $0.01; savings 0.01*0.5=0.005→0.01
# edit_retries waste: 3 * 300 = 900 tok (output) → 900*25/1e6 = $0.0225 → round=0.02; savings 0.02*0.7=0.014→0.01
# thinking: 50000 tok (output) → 50000*25/1e6 = $1.25; savings 1.25*0.3=0.375→0.38
# cost_concentration: myproj 0.80/1.0 = 80% > 40%; savings 0.80*0.2=0.16
_GOLDEN_INSIGHTS_MULTI = {
    "insights": [
        {
            "title": "Cache Cliffs",
            "severity": "CRITICAL",
            "finding": "5 cache cliffs detected — cache_read_ratio dropped >50% after 5 minutes+ idle gaps.",
            "root_cause": (
                "When you're idle >5 minutes, Anthropic's prompt cache expires (5m tier). "
                "The next turn re-creates the entire cache from scratch. Distributed across projects."
            ),
            "waste_tokens": 75000,
            "waste_usd": 0.38,
            "solution": {
                "action": "Run /compact before stepping away from a session",
                "detail": (
                    "Compacting reduces context size so cache re-creation is cheaper when you return. "
                    "For planned breaks, also consider ending the session and starting fresh."
                ),
                "claudemd_rule": None,
                "estimated_savings_usd": 0.23,
            },
            "priority": "P0",
        },
        {
            "title": "Context Pressure",
            "severity": "CRITICAL",
            "finding": "2 turns hit max_tokens — model was cut off mid-response.",
            "root_cause": (
                "The conversation context exceeded the model's output budget. This typically "
                "happens in long sessions with many tool calls, large file reads, or when "
                "CLAUDE.md/hooks inject significant context every turn."
            ),
            "waste_tokens": 10000,
            "waste_usd": 0.25,
            "solution": {
                "action": "Run /compact proactively when sessions exceed ~40 turns, or split into smaller sessions",
                "detail": (
                    "Monitor turn count. If you're doing a large refactor, break it into focused sessions "
                    "(one per file/module) rather than one marathon session."
                ),
                "claudemd_rule": None,
                "estimated_savings_usd": 0.2,
            },
            "priority": "P0",
        },
        {
            "title": "Bash Antipatterns",
            "severity": "WARNING",
            "finding": (
                "9 Bash calls use standalone cat/grep/find/ls where a "
                "dedicated tool (Read, Grep, Glob) exists. Legitimate "
                "pipeline feeders, existence checks, and time-sorted ls are excluded."
            ),
            "root_cause": (
                "Claude is choosing Bash for standalone file reads/searches. "
                "Top projects: myproj: 7; otherproj: 2. Top commands: various cat/grep/find/ls calls."
            ),
            "waste_tokens": 1800,
            "waste_usd": 0.04,
            "solution": {
                "action": "Reinforce CLAUDE.md rule for standalone tool use",
                "detail": (
                    "Dedicated tools return structured output with fewer tokens than raw shell. "
                    "A blanket PreToolUse enforcement hook would have high false-positive rate — "
                    "pipelines, existence checks, and stat operations are legitimate Bash uses. "
                    "CLAUDE.md guidance is sufficient for standalone cases."
                ),
                "claudemd_rule": (
                    "Use Read instead of standalone cat/head/tail. "
                    "Use Grep instead of standalone grep. "
                    "Use Glob instead of standalone find/ls."
                ),
                "estimated_savings_usd": 0.03,
            },
            "priority": "P1",
        },
        {
            "title": "Edit Retry Chains",
            "severity": "WARNING",
            "finding": "3 failed-edit retry chains detected.",
            "root_cause": (
                "An Edit call fails (usually unique-match failure), then Claude retries on the same "
                "file next turn. The failure means Claude's mental model of the file diverged from "
                "reality — typically after a prior edit changed the file. Projects: myproj: 3x."
            ),
            "waste_tokens": 900,
            "waste_usd": 0.02,
            "solution": {
                "action": "Add a CLAUDE.md rule: 'Always read a file before editing if more than 2 turns have passed since last read'",
                "detail": (
                    "The root cause is stale context. Claude edits based on what it remembers, not "
                    "the current file state. A fresh read before each edit is cheap (~500 input tokens) "
                    "compared to the cost of a failed edit + retry (~300 output tokens wasted)."
                ),
                "claudemd_rule": "Read file before editing if >2 turns since last read, to avoid stale-context edit failures.",
                "estimated_savings_usd": 0.01,
            },
            "priority": "P1",
        },
        {
            "title": "Redundant Reads",
            "severity": "WARNING",
            "finding": "4 extra file reads (same file read 3+ times in a session).",
            "root_cause": (
                "Claude is re-reading files it already has in context. This happens when earlier "
                "context gets compressed away or when Claude doesn't trust its cached knowledge. "
                "Worst offenders: `token_output.py` read 5x in session abc12345; `conftest.py` read 4x in session def67890."
            ),
            "waste_tokens": 2000,
            "waste_usd": 0.01,
            "solution": {
                "action": "Add a CLAUDE.md rule: 'After reading a file, reference it from context — do not re-read unless the file was modified since last read'",
                "detail": (
                    "Each redundant read re-ingests the file as input tokens. For large files (1K+ lines) "
                    "this adds significant cost. The Read tool output note already says 'content unchanged "
                    "since last read' but Claude sometimes ignores it."
                ),
                "claudemd_rule": "After reading a file, reference it from context. Only re-read if the file was modified since last read.",
                "estimated_savings_usd": 0.01,
            },
            "priority": "P1",
        },
        {
            "title": "Thinking Token Overhead",
            "severity": "INFO",
            "finding": "~50.0% of output tokens went to extended thinking (50K tokens, ~$1.25).",
            "root_cause": (
                "Extended thinking is Opus's reasoning mode — it produces internal chain-of-thought "
                "tokens that are billed as output but not visible to you. This is expected for complex "
                "tasks but can be excessive for simple ones."
            ),
            "waste_tokens": 0,
            "waste_usd": 0,
            "solution": {
                "action": "Use Sonnet for routine tasks (file reads, simple edits, git operations) and reserve Opus for complex reasoning",
                "detail": (
                    "Thinking tokens cost $1.25 at output rates. If you're using Opus for "
                    "everything, switching routine tasks to Sonnet (which doesn't use extended thinking) "
                    "could save 50-70% of this cost."
                ),
                "claudemd_rule": None,
                "estimated_savings_usd": 0.38,
            },
            "priority": "P2",
        },
        {
            "title": "Cost Concentration",
            "severity": "INFO",
            "finding": "myproj accounts for 80.0% of total spend ($0.80 of $1.00).",
            "root_cause": (
                "This project dominates your usage. Either it has the most sessions, uses "
                "the most expensive model, or both. Review whether all work in this project "
                "requires the current model tier."
            ),
            "waste_tokens": 0,
            "waste_usd": 0,
            "solution": {
                "action": "Audit myproj sessions — could routine tasks use Sonnet instead of Opus?",
                "detail": (
                    "Switching from Opus ($5/$25 per MTok) to Sonnet ($3/$15 per MTok) for 50% of "
                    "myproj work would save ~$0.16."
                ),
                "claudemd_rule": None,
                "estimated_savings_usd": 0.16,
            },
            "priority": "P2",
        },
    ],
    "findings": [
        {
            "title": "Cache Cliffs",
            "severity": "CRITICAL",
            "text": (
                "5 cache cliffs detected — cache_read_ratio dropped >50% after 5 minutes+ idle gaps. "
                "When you're idle >5 minutes, Anthropic's prompt cache expires (5m tier). "
                "The next turn re-creates the entire cache from scratch. Distributed across projects."
            ),
            "waste": 75000,
        },
        {
            "title": "Context Pressure",
            "severity": "CRITICAL",
            "text": (
                "2 turns hit max_tokens — model was cut off mid-response. "
                "The conversation context exceeded the model's output budget. This typically "
                "happens in long sessions with many tool calls, large file reads, or when "
                "CLAUDE.md/hooks inject significant context every turn."
            ),
            "waste": 10000,
        },
        {
            "title": "Bash Antipatterns",
            "severity": "WARNING",
            "text": (
                "9 Bash calls use standalone cat/grep/find/ls where a "
                "dedicated tool (Read, Grep, Glob) exists. Legitimate "
                "pipeline feeders, existence checks, and time-sorted ls are excluded. "
                "Claude is choosing Bash for standalone file reads/searches. "
                "Top projects: myproj: 7; otherproj: 2. Top commands: various cat/grep/find/ls calls."
            ),
            "waste": 1800,
        },
        {
            "title": "Edit Retry Chains",
            "severity": "WARNING",
            "text": (
                "3 failed-edit retry chains detected. "
                "An Edit call fails (usually unique-match failure), then Claude retries on the same "
                "file next turn. The failure means Claude's mental model of the file diverged from "
                "reality — typically after a prior edit changed the file. Projects: myproj: 3x."
            ),
            "waste": 900,
        },
        {
            "title": "Redundant Reads",
            "severity": "WARNING",
            "text": (
                "4 extra file reads (same file read 3+ times in a session). "
                "Claude is re-reading files it already has in context. This happens when earlier "
                "context gets compressed away or when Claude doesn't trust its cached knowledge. "
                "Worst offenders: `token_output.py` read 5x in session abc12345; `conftest.py` read 4x in session def67890."
            ),
            "waste": 2000,
        },
        {
            "title": "Thinking Token Overhead",
            "severity": "INFO",
            "text": (
                "~50.0% of output tokens went to extended thinking (50K tokens, ~$1.25). "
                "Extended thinking is Opus's reasoning mode — it produces internal chain-of-thought "
                "tokens that are billed as output but not visible to you. This is expected for complex "
                "tasks but can be excessive for simple ones."
            ),
            "waste": 0,
        },
        {
            "title": "Cost Concentration",
            "severity": "INFO",
            "text": (
                "myproj accounts for 80.0% of total spend ($0.80 of $1.00). "
                "This project dominates your usage. Either it has the most sessions, uses "
                "the most expensive model, or both. Review whether all work in this project "
                "requires the current model tier."
            ),
            "waste": 0,
        },
    ],
    "recommendations": [
        {
            "text": (
                "Run /compact before stepping away from a session. "
                "Compacting reduces context size so cache re-creation is cheaper when you return. "
                "For planned breaks, also consider ending the session and starting fresh."
            ),
            "impact": 75000,
            "priority": "P0",
        },
        {
            "text": (
                "Run /compact proactively when sessions exceed ~40 turns, or split into smaller sessions. "
                "Monitor turn count. If you're doing a large refactor, break it into focused sessions "
                "(one per file/module) rather than one marathon session."
            ),
            "impact": 10000,
            "priority": "P0",
        },
        {
            "text": (
                "Reinforce CLAUDE.md rule for standalone tool use. "
                "Dedicated tools return structured output with fewer tokens than raw shell. "
                "A blanket PreToolUse enforcement hook would have high false-positive rate — "
                "pipelines, existence checks, and stat operations are legitimate Bash uses. "
                "CLAUDE.md guidance is sufficient for standalone cases."
            ),
            "impact": 1800,
            "priority": "P1",
        },
        {
            "text": (
                "Add a CLAUDE.md rule: 'Always read a file before editing if more than 2 turns have passed since last read'. "
                "The root cause is stale context. Claude edits based on what it remembers, not "
                "the current file state. A fresh read before each edit is cheap (~500 input tokens) "
                "compared to the cost of a failed edit + retry (~300 output tokens wasted)."
            ),
            "impact": 900,
            "priority": "P1",
        },
        {
            "text": (
                "Add a CLAUDE.md rule: 'After reading a file, reference it from context — do not re-read unless the file was modified since last read'. "
                "Each redundant read re-ingests the file as input tokens. For large files (1K+ lines) "
                "this adds significant cost. The Read tool output note already says 'content unchanged "
                "since last read' but Claude sometimes ignores it."
            ),
            "impact": 2000,
            "priority": "P1",
        },
        {
            "text": (
                "Use Sonnet for routine tasks (file reads, simple edits, git operations) and reserve Opus for complex reasoning. "
                "Thinking tokens cost $1.25 at output rates. If you're using Opus for "
                "everything, switching routine tasks to Sonnet (which doesn't use extended thinking) "
                "could save 50-70% of this cost."
            ),
            "impact": 0,
            "priority": "P2",
        },
        {
            "text": (
                "Audit myproj sessions — could routine tasks use Sonnet instead of Opus?. "
                "Switching from Opus ($5/$25 per MTok) to Sonnet ($3/$15 per MTok) for 50% of "
                "myproj work would save ~$0.16."
            ),
            "impact": 0,
            "priority": "P2",
        },
    ],
    "trends": {},
}

# build_trends golden for a DB with one current-window (1d ago) and one
# prior-window (9d ago) session.  Each has 1 turn, in=100 out=50, model=claude-sonnet-4-5.
# Cost = turn_cost(100, 50, 0, 0, 0, 0, pricing) = (100*3 + 50*15)/1e6 = 0.00105 → round(2) = 0.0
# cost_per_session = round(0.00105 / 1, 2) = 0.0
# All antipattern/cliff/error counts are 0 for these bare sessions.
_GOLDEN_BUILD_TRENDS = {
    "window_days": 7,
    "current_window": {
        "sessions": 1,
        "turns": 1,
        "cost_usd": 0.0,
        "cost_per_session": 0.0,
        "cache_ratio": 0.0,
        "cliffs_per_session": 0.0,
        "antipatterns_per_session": 0.0,
        "tool_error_rate": 0,
        "hook_avg_ms": 0.0,
    },
    "prior_window": {
        "sessions": 1,
        "turns": 1,
        "cost_usd": 0.0,
        "cost_per_session": 0.0,
        "cache_ratio": 0.0,
        "cliffs_per_session": 0.0,
        "antipatterns_per_session": 0.0,
        "tool_error_rate": 0,
        "hook_avg_ms": 0.0,
    },
    "metrics": {
        "cost_per_session": {"label": "Cost/Session", "current": 0.0, "prior": 0.0, "change_pct": None},
        "cache_ratio": {"label": "Cache Ratio", "current": 0.0, "prior": 0.0, "change_pct": None},
        "cliffs_per_session": {"label": "Cache Cliffs/Session", "current": 0.0, "prior": 0.0, "change_pct": None},
        "antipatterns_per_session": {
            "label": "Bash Antipatterns/Session",
            "current": 0.0,
            "prior": 0.0,
            "change_pct": None,
        },
        "tool_error_rate": {"label": "Tool Error Rate", "current": 0, "prior": 0, "change_pct": None},
        "hook_avg_ms": {"label": "Hook Avg Latency", "current": 0.0, "prior": 0.0, "change_pct": None},
    },
    "improved": [],
    "regressed": [],
    "new_skills": [],
    "retired_skills": [],
    "new_hooks": [],
    "retired_hooks": [],
    "hook_trends": [],
}


class TestBuildTrends:
    def test_no_recent_data_returns_empty(self, populated_token_db):
        # The shared fixture is dated 2026-03-01, well outside the 7-day rolling
        # window, so build_trends short-circuits to an empty dict.
        assert build_trends(populated_token_db) == {}

    def test_empty_db_returns_empty(self, token_db):
        assert build_trends(token_db) == {}

    def test_structure_with_recent_data(self, token_db):
        # One session in the current window (1d ago) and one in the prior (9d ago).
        import_session(token_db, _session_at("cur", days_ago=1), TOKEN_JNL)
        import_session(token_db, _session_at("pri", days_ago=9), TOKEN_JNL)
        token_db.commit()
        t = build_trends(token_db)
        assert t["window_days"] == 7
        for key in ("current_window", "prior_window", "metrics", "improved", "regressed"):
            assert key in t

    def test_golden_full_dict(self, token_db):
        """Golden pin: full build_trends dict for current+prior window sessions."""
        import_session(token_db, _session_at("cur", days_ago=1), TOKEN_JNL)
        import_session(token_db, _session_at("pri", days_ago=9), TOKEN_JNL)
        token_db.commit()
        assert build_trends(token_db) == _GOLDEN_BUILD_TRENDS


class TestBuildInsights:
    def test_returns_all_sections(self, token_db):
        result = build_insights_and_trends(token_db, **_insight_kwargs())
        for key in ("insights", "findings", "recommendations", "trends"):
            assert key in result
        assert isinstance(result["insights"], list)

    def test_cache_cliffs_insight_fires(self, token_db):
        result = build_insights_and_trends(token_db, **_insight_kwargs(cache_cliffs=5))
        titles = [i["title"] for i in result["insights"]]
        assert "Cache Cliffs" in titles

    def test_cache_cliffs_absent_when_zero(self, token_db):
        result = build_insights_and_trends(token_db, **_insight_kwargs(cache_cliffs=0))
        titles = [i["title"] for i in result["insights"]]
        assert "Cache Cliffs" not in titles

    def test_cache_cliffs_insight_has_waste_estimate(self, token_db):
        result = build_insights_and_trends(token_db, **_insight_kwargs(cache_cliffs=5))
        cliff = next(i for i in result["insights"] if i["title"] == "Cache Cliffs")
        assert cliff["waste_tokens"] == 5 * 15000
        assert cliff["severity"] in ("INFO", "WARNING", "CRITICAL")


class TestBuildInsightsGolden:
    def test_all_off_full_dict(self, token_db):
        """Golden pin: all signals zero → empty insights/findings/recommendations."""
        assert build_insights_and_trends(token_db, **_insight_kwargs()) == _GOLDEN_INSIGHTS_ALL_OFF

    def test_multi_insight_full_dict(self, token_db):
        """Golden pin: multi-signal case captures proj_detail/file_detail/cmd_detail formatting paths."""
        result = build_insights_and_trends(
            token_db,
            **_insight_kwargs(
                cache_cliffs=5,
                max_token_stops=2,
                total_bash_antipatterns=9,
                redundant_reads_count=4,
                edit_retries_count=3,
                total_thinking=50000,
                total_output=100000,
                total_sessions=10,
                bash_antipattern_projects=[
                    {"project": "myproj", "antipatterns": 7, "total_bash": 20},
                    {"project": "otherproj", "antipatterns": 2, "total_bash": 5},
                ],
                top_redundant_files=[
                    {"session_id": "abc12345", "file": "token_output.py", "count": 5},
                    {"session_id": "def67890", "file": "conftest.py", "count": 4},
                ],
                edit_retry_projects=[
                    {"project": "myproj", "retries": 3},
                ],
                cost_by_project=[
                    {"project": "myproj", "cost_usd": 0.80},
                    {"project": "otherproj", "cost_usd": 0.20},
                ],
                total_cost_usd=1.0,
            ),
        )
        assert result == _GOLDEN_INSIGHTS_MULTI
