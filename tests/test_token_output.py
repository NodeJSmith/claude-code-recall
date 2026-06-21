"""Tests for token_output.build_output.

build_output runs the full read-side aggregation (and spreads in
build_insights_and_trends), so these exercise token_output and token_insights
together against the shared populated_token_db fixture (see conftest).
"""

from ccrecall.token_output import build_output

# Golden snapshot of build_output(populated_token_db) minus generated_at.
# populated_token_db: 2 sessions (s1, s2), 3 turns total.
#   s1: turn1 (in=100, out=50, one Read tool call), turn2 (in=200, out=80)
#   s2: turn1 (in=300, out=120)
# Cost derived from get_pricing("claude-sonnet-4-5"): input=$3/MTok, output=$15/MTok.
# total cost = turn_cost(600, 250, 0, 0, 0, 0, pricing) = (600*3 + 250*15) / 1_000_000
#            = (1800 + 3750) / 1_000_000 = 0.00555 → round(4) = 0.0056, round(2) = 0.01
_GOLDEN_BUILD_OUTPUT = {
    "total_sessions": 2,
    "date_range": {"earliest": "2026-03-01", "latest": "2026-03-01"},
    "kpis": {
        "total_sessions": 2,
        "total_turns": 3,
        "total_output_tokens": 250,
        "global_cache_ratio": 0.0,
        "cache_cliffs": 0,
        "max_token_stops": 0,
        "bash_antipatterns": 0,
        "tool_error_rate": 0.0,
        "total_cost_usd": 0.01,
    },
    "sessions_by_day": [
        {
            "date": "2026-03-01",
            "session_count": 2,
            "input_tokens": 600,
            "output_tokens": 250,
            "cache_read": 0,
            "cache_creation": 0,
        }
    ],
    "top_tools": [{"tool": "Read", "count": 1}],
    "model_split": [
        {
            "model": "claude-sonnet-4-5",
            "input_tokens": 600,
            "output_tokens": 250,
            "thinking_tokens": 0,
            "cost_usd": 0.0056,
        }
    ],
    "cost_by_day": [{"date": "2026-03-01", "cost_usd": 0.0056}],
    "cost_by_project": [{"project": "proj", "cost_usd": 0.0056}],
    "cache_trajectory": [],
    "context_segments": [],
    "context_segments_recent": [],
    "tool_footprint": [],
    "context_seg_summary": {
        "sessions_analyzed": 0,
        "avg_base_ctx": 0,
        "base_overhead_pct": 0.0,
        "recent_sessions_count": 0,
    },
    "ephem_split": [],
    "bash_antipatterns": [],
    "tool_errors_by_tool": [],
    "redundant_reads": [],
    "edit_retries": [],
    "agent_cost": [],
    "turn_complexity": {"minimal": 2, "light": 1, "medium": 0, "heavy": 0, "runaway": 0},
    "thinking_in_complexity": {"minimal": 0, "light": 0, "medium": 0, "heavy": 0, "runaway": 0},
    "response_time_dist": {
        "under_30s": 0,
        "30s_2m": 0,
        "2m_5m": 0,
        "5m_15m": 0,
        "15m_1h": 0,
        "over_1h": 0,
    },
    "hook_overhead": [],
    "project_spend": [
        {
            "project": "proj",
            "input_tokens": 600,
            "output_tokens": 250,
            "cache_creation": 0,
            "cache_read": 0,
            "sessions": 2,
        }
    ],
    "project_tool_profile": [{"project": "proj", "tools": {"Read": 1}}],
    "skill_usage": [],
    "skill_usage_by_day": [],
    "agent_delegation": [],
    "agent_model_dist": [],
    "hook_performance": [],
    # spread from build_insights_and_trends
    "insights": [
        {
            "title": "Cost Concentration",
            "severity": "INFO",
            "finding": "proj accounts for 100.0% of total spend ($0.01 of $0.01).",
            "root_cause": (
                "This project dominates your usage. Either it has the most sessions, uses "
                "the most expensive model, or both. Review whether all work in this project "
                "requires the current model tier."
            ),
            "waste_tokens": 0,
            "waste_usd": 0,
            "solution": {
                "action": "Audit proj sessions — could routine tasks use Sonnet instead of Opus?",
                "detail": (
                    "Switching from Opus ($5/$25 per MTok) to Sonnet ($3/$15 per MTok) for 50% of "
                    "proj work would save ~$0.00."
                ),
                "claudemd_rule": None,
                "estimated_savings_usd": 0.0,
            },
            "priority": "P0",
        }
    ],
    "findings": [
        {
            "title": "Cost Concentration",
            "severity": "INFO",
            "text": (
                "proj accounts for 100.0% of total spend ($0.01 of $0.01). "
                "This project dominates your usage. Either it has the most sessions, uses "
                "the most expensive model, or both. Review whether all work in this project "
                "requires the current model tier."
            ),
            "waste": 0,
        }
    ],
    "recommendations": [
        {
            "text": (
                "Audit proj sessions — could routine tasks use Sonnet instead of Opus?. "
                "Switching from Opus ($5/$25 per MTok) to Sonnet ($3/$15 per MTok) for 50% of "
                "proj work would save ~$0.00."
            ),
            "impact": 0,
            "priority": "P0",
        }
    ],
    "trends": {},
}


class TestBuildOutputKpis:
    def test_kpi_aggregates(self, populated_token_db):
        kpis = build_output(populated_token_db)["kpis"]
        assert kpis["total_sessions"] == 2
        assert kpis["total_turns"] == 3
        assert kpis["total_output_tokens"] == 50 + 80 + 120
        assert kpis["total_cost_usd"] >= 0

    def test_top_level_total_sessions(self, populated_token_db):
        assert build_output(populated_token_db)["total_sessions"] == 2

    def test_date_range_present(self, populated_token_db):
        dr = build_output(populated_token_db)["date_range"]
        assert dr["earliest"] == "2026-03-01"
        assert dr["latest"] == "2026-03-01"

    def test_top_tools_counts_read(self, populated_token_db):
        tools = {t["tool"]: t["count"] for t in build_output(populated_token_db)["top_tools"]}
        assert tools.get("Read") == 1


class TestBuildOutputStructure:
    def test_has_expected_sections(self, populated_token_db):
        out = build_output(populated_token_db)
        for key in (
            "kpis",
            "date_range",
            "sessions_by_day",
            "top_tools",
            "model_split",
            "cost_by_day",
            # spread in from build_insights_and_trends:
            "insights",
            "findings",
            "recommendations",
            "trends",
        ):
            assert key in out, f"missing section: {key}"

    def test_insights_and_trends_types(self, populated_token_db):
        out = build_output(populated_token_db)
        assert isinstance(out["insights"], list)
        assert isinstance(out["findings"], list)
        assert isinstance(out["recommendations"], list)
        assert isinstance(out["trends"], dict)


class TestBuildOutputGolden:
    def test_full_dict_equality(self, populated_token_db):
        """Golden pin: full build_output dict (minus generated_at) matches captured snapshot."""
        out = build_output(populated_token_db)
        out.pop("generated_at")
        assert out == _GOLDEN_BUILD_OUTPUT


class TestBuildOutputEmpty:
    def test_empty_db_zeroed_not_crashed(self, token_db):
        out = build_output(token_db)
        assert out["kpis"]["total_sessions"] == 0
        assert out["kpis"]["total_turns"] == 0
        assert out["kpis"]["total_cost_usd"] == 0
        assert out["top_tools"] == []
        assert out["date_range"]["earliest"] is None
