"""Tests for token_parser: parse_session, pricing, and cost computation.

The parse_session characterization tests pin its behavior on realistic JSONL, so
boundary-validation changes stay provably behavior-preserving on valid input.
"""

import json
from pathlib import Path

from ccrecall.token_parser import (
    DEFAULT_PRICING,
    JnlFile,
    get_pricing,
    parse_session,
    turn_cost,
)


def _jnl(tmp_path: Path, lines: list[dict], name: str = "sess.jsonl") -> JnlFile:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return JnlFile(path=path, project_cwd="/home/u/proj", is_sidechain=False, parent_session_id=None)


def _assistant(msg_id: str, *, model="claude-opus-4-6", content=None, usage=None, stop_reason=None) -> dict:
    return {
        "type": "assistant",
        "sessionId": "sess-1",
        "version": "1.2.3",
        "gitBranch": "main",
        "timestamp": "2026-03-01T10:00:00Z",
        "message": {
            "id": msg_id,
            "model": model,
            "stop_reason": stop_reason,
            "usage": usage or {},
            "content": content or [],
        },
    }


# ── Characterization: realistic happy-path parse ──────────────────────────


class TestParseSessionCharacterization:
    def test_full_session_shape(self, tmp_path):
        lines = [
            _assistant(
                "msg-a",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 10,
                    "cache_creation_input_tokens": 20,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 12,
                        "ephemeral_1h_input_tokens": 8,
                    },
                },
                content=[
                    {"type": "thinking", "thinking": "x" * 40},
                    {"type": "tool_use", "id": "tu-1", "name": "Read", "input": {"file_path": "/a/b.py"}},
                    {"type": "tool_use", "id": "tu-2", "name": "Bash", "input": {"command": "echo hi"}},
                ],
                stop_reason="tool_use",
            ),
            {
                "type": "user",
                "sessionId": "sess-1",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu-1", "is_error": False, "content": "ok"},
                    ]
                },
            },
            {"type": "system", "subtype": "turn_duration", "timestamp": "2026-03-01T10:00:05Z", "durationMs": 5000},
        ]
        jnl = _jnl(tmp_path, lines)
        session = parse_session(jnl.path, jnl)

        assert session is not None
        assert session.session_id == "sess-1"
        assert session.cc_version == "1.2.3"
        assert session.git_branch == "main"
        assert session.user_msg_count == 1
        assert len(session.turns) == 1

        turn = session.turns[0]
        assert turn.message_id == "msg-a"
        assert turn.model == "claude-opus-4-6"
        assert turn.input_tokens == 100
        assert turn.output_tokens == 50
        assert turn.cache_read_tokens == 10
        assert turn.cache_creation_tokens == 20
        assert turn.ephem_5m_tokens == 12
        assert turn.ephem_1h_tokens == 8
        assert turn.thinking_tokens == 10  # 40 chars // 4
        assert turn.stop_reason == "tool_use"
        assert turn.turn_duration_ms == 5000

        tools = {tc.tool_name: tc for tc in turn.tool_calls}
        assert tools["Read"].file_path == "/a/b.py"
        assert tools["Bash"].command == "echo hi"
        assert tools["Read"].is_error == 0  # matched tool_result, no error

    def test_no_turns_returns_none(self, tmp_path):
        jnl = _jnl(tmp_path, [{"type": "system", "subtype": "api_error"}])
        assert parse_session(jnl.path, jnl) is None

    def test_session_id_falls_back_to_filename(self, tmp_path):
        # No sessionId on any line, but a turn exists -> id derived from stem.
        jnl = _jnl(
            tmp_path,
            [
                {"type": "assistant", "message": {"id": "m1", "content": []}},
            ],
            name="abc123.jsonl",
        )
        session = parse_session(jnl.path, jnl)
        assert session is not None
        assert session.session_id == "abc123"

    def test_blank_and_malformed_lines_skipped(self, tmp_path):
        path = tmp_path / "mixed.jsonl"
        path.write_text(
            "\n".join(
                [
                    "",
                    "{not json",
                    json.dumps(_assistant("m1", usage={"input_tokens": 5, "output_tokens": 5})),
                    "   ",
                ]
            ),
            encoding="utf-8",
        )
        jnl = JnlFile(path=path, project_cwd="/p", is_sidechain=False, parent_session_id=None)
        session = parse_session(path, jnl)
        assert session is not None
        assert len(session.turns) == 1
        assert session.turns[0].input_tokens == 5


# ── Pricing ────────────────────────────────────────────────────────────────


class TestGetPricing:
    def test_opus_46(self):
        assert get_pricing("claude-opus-4-6-20260101")["input"] == 5.0

    def test_opus_41_distinct_from_46(self):
        # opus-4-1 is the older, pricier tier — must not collide with opus-4-6.
        assert get_pricing("claude-opus-4-1-20250805")["input"] == 15.0

    def test_sonnet(self):
        assert get_pricing("claude-sonnet-4-5")["input"] == 3.0

    def test_haiku_uppercase_matches(self):
        # get_pricing lowercases the model id before matching.
        assert get_pricing("CLAUDE-HAIKU-4-5")["input"] == 1.0

    def test_unknown_model_falls_back_to_sonnet(self):
        # The fallback-by-name fix: an unrecognized model gets Sonnet rates.
        assert get_pricing("gpt-4-turbo") == DEFAULT_PRICING
        assert get_pricing("gpt-4-turbo")["input"] == 3.0

    def test_none_falls_back_to_sonnet(self):
        assert get_pricing(None) == DEFAULT_PRICING

    def test_default_pricing_is_sonnet(self):
        assert get_pricing("claude-sonnet-4-5") == DEFAULT_PRICING


# ── Cost computation ─────────────────────────────────────────────────────────


class TestTurnCost:
    # Exactly 1M tokens of each kind, so cost == the per-million rate from the
    # pricing dict — derived from the dict so it can't drift if rates change.
    def test_input_and_output(self):
        p = get_pricing("claude-sonnet-4-5")
        assert turn_cost(1_000_000, 0, 0, 0, 0, 0, p) == p["input"]
        assert turn_cost(0, 1_000_000, 0, 0, 0, 0, p) == p["output"]

    def test_cache_tiers(self):
        p = get_pricing("claude-sonnet-4-5")
        assert turn_cost(0, 0, 1_000_000, 0, 0, 0, p) == p["cache_read"]
        assert turn_cost(0, 0, 0, 0, 1_000_000, 0, p) == p["cache_write_5m"]
        assert turn_cost(0, 0, 0, 0, 0, 1_000_000, p) == p["cache_write_1h"]

    def test_unclassified_creation_billed_at_5m(self):
        # cache_creation beyond the classified 5m/1h tiers is attributed to 5m.
        p = get_pricing("claude-sonnet-4-5")
        assert turn_cost(0, 0, 0, 1_000_000, 0, 0, p) == p["cache_write_5m"]

    def test_zero_everything(self):
        assert turn_cost(0, 0, 0, 0, 0, 0, get_pricing(None)) == 0.0
