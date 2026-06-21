"""Boundary validation behavior.

Malformed JSON at the transcript / token / hook boundaries must be skipped
(and logged), not crash downstream. Valid input must flow through unchanged.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ccrecall.models import HookInput
from ccrecall.parsing import compute_branch_metadata, is_valid_entry, parse_jsonl_file
from ccrecall.token_parser import JnlFile, parse_session

FIXTURES = Path(__file__).parent / "fixtures"


# ── Transcript boundary (parsing.py) ──────────────────────────────────────


class TestTranscriptValidation:
    def test_accepts_normal_entry(self):
        assert is_valid_entry({"uuid": "u1", "type": "user", "message": {"content": "hi"}})

    def test_accepts_list_content_blocks(self):
        assert is_valid_entry(
            {"uuid": "u1", "type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}
        )

    def test_accepts_entry_without_message(self):
        # summary / file-history lines carry a uuid but no message
        assert is_valid_entry({"uuid": "u1", "type": "summary"})

    def test_rejects_non_dict(self):
        assert not is_valid_entry(["not", "an", "object"])
        assert not is_valid_entry("a bare string")

    def test_rejects_scalar_message(self):
        # message-as-string is exactly what crashed message.get("content")
        assert not is_valid_entry({"uuid": "u1", "type": "user", "message": "oops"})

    def test_rejects_scalar_content(self):
        assert not is_valid_entry({"uuid": "u1", "type": "user", "message": {"content": 42}})

    def test_parse_jsonl_skips_malformed_keeps_valid(self, tmp_path):
        path = tmp_path / "mixed.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"uuid": "u1", "type": "user", "message": {"content": "good"}}),
                    json.dumps({"uuid": "u2", "type": "assistant", "message": "bad-scalar-message"}),
                    json.dumps({"uuid": "u3", "type": "user", "message": {"content": 99}}),
                    json.dumps({"uuid": "u4", "type": "assistant", "message": {"content": "also good"}}),
                ]
            ),
            encoding="utf-8",
        )
        entries = list(parse_jsonl_file(path))
        uuids = [e["uuid"] for e in entries]
        assert uuids == ["u1", "u4"]

    def test_compute_branch_metadata_unaffected_for_valid(self):
        # Valid entries that reach compute_branch_metadata behave as before.
        entries = [
            {"type": "user", "message": {"content": "q"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "id": "t"}]}},
        ]
        count, _, _, tools = compute_branch_metadata(entries)
        assert count == 1
        assert tools == {"Read": 1}


# ── Token boundary (token_parser.py) ──────────────────────────────────────


def _jnl(tmp_path: Path, lines: list[dict]) -> JnlFile:
    path = tmp_path / "tok.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return JnlFile(path=path, project_cwd="/p", is_sidechain=False, parent_session_id=None)


class TestTokenValidation:
    def test_tool_input_as_list_does_not_crash(self, tmp_path):
        # block["input"] as a list previously crashed inp.get(...)
        lines = [
            {
                "type": "assistant",
                "sessionId": "s1",
                "message": {
                    "id": "m1",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [{"type": "tool_use", "name": "Weird", "id": "t1", "input": [1, 2, 3]}],
                },
            }
        ]
        jnl = _jnl(tmp_path, lines)
        session = parse_session(jnl.path, jnl)
        assert session is not None
        tc = session.turns[0].tool_calls[0]
        assert tc.tool_name == "Weird"
        assert tc.file_path is None  # no usable input dict

    def test_malformed_token_line_skipped(self, tmp_path):
        lines = [
            {"type": "assistant", "message": "scalar-not-a-dict"},  # invalid envelope
            {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 7, "output_tokens": 0}}},
        ]
        jnl = _jnl(tmp_path, lines)
        session = parse_session(jnl.path, jnl)
        assert session is not None
        assert len(session.turns) == 1
        assert session.turns[0].input_tokens == 7

    @pytest.mark.parametrize("fixture", ["linear_3_exchange.jsonl", "tool_heavy.jsonl"])
    def test_real_fixture_parses_without_error(self, fixture):
        jnl = JnlFile(path=FIXTURES / fixture, project_cwd="/p", is_sidechain=False, parent_session_id=None)
        # Must not raise; real transcripts are valid input.
        parse_session(jnl.path, jnl)


# ── Hook boundary (models.HookInput) ──────────────────────────────────────


class TestHookInputValidation:
    def test_valid_payload(self):
        hi = HookInput.model_validate_json('{"session_id": "abc", "cwd": "/x", "source": "clear"}')
        assert hi.session_id == "abc"
        assert hi.cwd == "/x"
        assert hi.source == "clear"

    def test_extra_fields_allowed(self):
        hi = HookInput.model_validate_json('{"session_id": "abc", "transcript_path": "/y", "unknown": 1}')
        assert hi.session_id == "abc"

    def test_rejects_non_string_session_id(self):
        with pytest.raises(ValidationError):
            HookInput.model_validate_json('{"session_id": 123}')

    def test_rejects_invalid_json(self):
        with pytest.raises(ValidationError):
            HookInput.model_validate_json("not json at all")

    def test_missing_fields_default_none(self):
        hi = HookInput.model_validate_json("{}")
        assert hi.session_id is None
        assert hi.cwd is None
        assert hi.source is None
