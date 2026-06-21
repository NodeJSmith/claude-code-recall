"""Tests for token_parser.parse_session.

The characterization tests here pin parse_session's behavior on realistic JSONL,
so that boundary-validation changes stay provably behavior-preserving on valid
input.
"""

import json
from pathlib import Path

from ccrecall.token_parser import JnlFile, parse_session


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
