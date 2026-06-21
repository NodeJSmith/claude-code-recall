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
