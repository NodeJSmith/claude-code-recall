"""Tests for token_output.build_output.

build_output runs the full read-side aggregation (and spreads in
build_insights_and_trends), so these exercise token_output and token_insights
together against the shared populated_token_db fixture (see conftest).
"""

from ccrecall.token_output import build_output


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


class TestBuildOutputEmpty:
    def test_empty_db_zeroed_not_crashed(self, token_db):
        out = build_output(token_db)
        assert out["kpis"]["total_sessions"] == 0
        assert out["kpis"]["total_turns"] == 0
        assert out["kpis"]["total_cost_usd"] == 0
        assert out["top_tools"] == []
        assert out["date_range"]["earliest"] is None
