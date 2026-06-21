"""Shared builders for token-subsystem tests.

One place to construct token sessions/turns and the dummy JnlFile, so the
fixtures in conftest and the per-module tests don't drift apart.
"""

from pathlib import Path

from ccrecall.token_parser import JnlFile, ParsedSession, Turn

# Dummy source file for import_session — not read by the import path itself.
TOKEN_JNL = JnlFile(path=Path("/x/s.jsonl"), project_cwd="/home/u/proj", is_sidechain=False, parent_session_id=None)


def token_turn(
    index: int,
    *,
    timestamp: str | None = None,
    model: str = "claude-sonnet-4-5",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    tool_calls: list | None = None,
) -> Turn:
    return Turn(
        index=index,
        message_id=f"m{index}",
        # :02d so multi-turn fixtures (index >= 10) stay valid timestamps.
        timestamp=timestamp or f"2026-03-01T10:{index:02d}:00Z",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        tool_calls=tool_calls or [],
    )


def token_session(sid: str, turns: list[Turn], project_path: str = "/home/u/proj") -> ParsedSession:
    s = ParsedSession(session_id=sid, project_path=project_path)
    s.turns = turns
    return s
