"""Tests for ccrecall.tail_skills — invoked-skill detection for ``ccrecall tail``."""

import json

from ccrecall.session_tail import emit
from ccrecall.tail_skills import InvokedSkill, find_invoked_skills, format_skills_block

# entry builders (mirror the real transcript shapes)

_counter = [0]


def _uuid() -> str:
    _counter[0] += 1
    return f"sk-uuid-{_counter[0]:04d}"


def _user(content, *, parent: str | None = None, **extra) -> dict:
    entry = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "uuid": _uuid(),
        "parentUuid": parent,
        "timestamp": f"2026-09-20T10:{_counter[0] % 60:02d}:00.000Z",
    }
    entry.update(extra)
    return entry


def slash_command(name: str, args: str | None = None) -> dict:
    content = f"<command-message>{name.lstrip('/')}</command-message>\n<command-name>{name}</command-name>"
    if args is not None:
        content += f"\n<command-args>{args}</command-args>"
    return _user(content)


def skill_load(skill_dir: str, *, parent: str | None = None, source_tool_use_id: str | None = None, **extra) -> dict:
    text = f"Base directory for this skill: {skill_dir}\n\n# Skill body\n\nInstructions..."
    if source_tool_use_id:
        extra["sourceToolUseID"] = source_tool_use_id
    return _user([{"type": "text", "text": text}], parent=parent, isMeta=True, **extra)


def skill_tool_call(tool_id: str, skill_input) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": "Skill", "input": skill_input}],
        },
        "uuid": _uuid(),
    }


def typed_skill(name: str, skill_dir: str, args: str | None = None) -> list[dict]:
    cmd = slash_command(name, args)
    return [cmd, skill_load(skill_dir, parent=cmd["uuid"])]


def tool_skill(tool_id: str, skill_input, skill_dir: str) -> list[dict]:
    call = skill_tool_call(tool_id, skill_input)
    result = _user(
        [{"type": "tool_result", "tool_use_id": tool_id, "content": "Launching skill"}],
        parent=call["uuid"],
    )
    return [call, result, skill_load(skill_dir, parent=result["uuid"], source_tool_use_id=tool_id)]


class TestFindInvokedSkills:
    def test_typed_slash_command_uses_command_name_and_args(self):
        entries = typed_skill("/mine-orchestrate", "/home/u/.claude/skills/mine-orchestrate", "design/specs/002")
        [skill] = find_invoked_skills(entries)
        assert skill.name == "mine-orchestrate"
        assert skill.last_args == "design/specs/002"
        assert skill.count == 1
        assert skill.last_at == entries[1]["timestamp"]

    def test_plugin_namespaced_command_keeps_the_typed_name(self):
        entries = typed_skill("/ccrecall:ccr-recall", "/home/u/.claude/plugins/cache/ccrecall/skills/ccr-recall")
        assert [s.name for s in find_invoked_skills(entries)] == ["ccrecall:ccr-recall"]

    def test_skill_tool_call_uses_tool_input(self):
        entries = tool_skill(
            "toolu_1",
            {"skill": "mine-implementation-review", "args": "design/specs/002"},
            "/home/u/.claude/skills/mine-implementation-review",
        )
        [skill] = find_invoked_skills(entries)
        assert skill.name == "mine-implementation-review"
        assert skill.last_args == "design/specs/002"

    def test_builtin_command_without_skill_body_is_not_a_skill(self):
        assert find_invoked_skills([slash_command("/model", "opus")]) == []

    def test_most_recent_first_with_repeat_counts(self):
        entries = [
            *typed_skill("/mine-orchestrate", "/s/mine-orchestrate", "first"),
            *tool_skill("toolu_2", {"skill": "mine-comb"}, "/s/mine-comb"),
            *typed_skill("/mine-orchestrate", "/s/mine-orchestrate", "second"),
        ]
        skills = find_invoked_skills(entries)
        assert [(s.name, s.count, s.last_args) for s in skills] == [
            ("mine-orchestrate", 2, "second"),
            ("mine-comb", 1, ""),
        ]

    def test_falls_back_to_directory_name_when_invocation_is_missing(self):
        entries = [skill_load("/home/u/.claude/skills/mine-debug", parent="not-in-transcript")]
        [skill] = find_invoked_skills(entries)
        assert skill.name == "mine-debug"
        assert skill.last_args == ""

    def test_sidechain_skill_loads_are_ignored(self):
        assert find_invoked_skills([skill_load("/s/mine-comb", isSidechain=True)]) == []

    def test_malformed_skill_input_does_not_crash(self):
        entries = [
            *tool_skill("toolu_3", {"skill": 42, "args": ["x"]}, "/s/mine-audit"),
            *tool_skill("toolu_4", "not-a-dict", "/s/mine-how"),
        ]
        assert [(s.name, s.last_args) for s in find_invoked_skills(entries)] == [
            ("mine-how", ""),
            ("mine-audit", ""),
        ]

    def test_unhashable_source_tool_use_id_does_not_crash(self):
        call = skill_tool_call("toolu_5", {"skill": "mine-audit"})
        result = _user(
            [{"type": "tool_result", "tool_use_id": "toolu_5", "content": "Launching skill"}],
            parent=call["uuid"],
        )
        load = skill_load("/s/mine-audit", parent=result["uuid"])
        load["sourceToolUseID"] = ["toolu_5"]  # undocumented field, wrong type
        entries = [call, result, load]
        [skill] = find_invoked_skills(entries)
        assert skill.name == "mine-audit"  # falls back to directory name, not the tool input
        assert skill.last_args == ""

    def test_prefix_match_ignores_case_and_leading_whitespace(self):
        # Same normalization typed_instruction uses to filter skill bodies as noise.
        entry = _user([{"type": "text", "text": "\n  base DIRECTORY for this skill: /s/mine-why\n\nbody"}])
        assert [s.name for s in find_invoked_skills([entry])] == ["mine-why"]

    def test_skill_body_quoted_in_typed_text_is_not_a_load(self):
        # Only a message that *starts* with the prefix is a skill body.
        entries = [_user("why does it say 'Base directory for this skill: /s/x'?")]
        assert find_invoked_skills(entries) == []


class TestFormatSkillsBlock:
    def test_renders_count_timestamp_and_clipped_args(self):
        out = format_skills_block(
            [
                InvokedSkill(name="mine-orchestrate", count=2, last_at="2026-09-20T10:00:00Z", last_args="a" * 200),
                InvokedSkill(name="mine-comb", count=1, last_at=None, last_args=""),
            ]
        )
        lines = out.splitlines()
        assert lines[0] == "SKILLS INVOKED (most recent first):"
        assert lines[1].startswith("  mine-orchestrate  x2  last 2026-09-20T10:00:00Z  args: aaa")
        assert lines[1].endswith("[…]")
        assert lines[2] == "  mine-comb  last unknown"


class TestEmitShowsSkills:
    def _write(self, tmp_path, entries):
        path = tmp_path / "s.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        return path

    def test_emit_prints_skills_block(self, tmp_path, capsys):
        path = self._write(tmp_path, typed_skill("/mine-orchestrate", "/s/mine-orchestrate"))
        assert emit(path, k=8) == 0
        assert "SKILLS INVOKED (most recent first):\n  mine-orchestrate" in capsys.readouterr().out

    def test_emit_full_also_prints_skills_block(self, tmp_path, capsys):
        path = self._write(tmp_path, typed_skill("/mine-orchestrate", "/s/mine-orchestrate"))
        assert emit(path, k=8, full=True) == 0
        assert "SKILLS INVOKED" in capsys.readouterr().out

    def test_emit_omits_block_when_no_skills(self, tmp_path, capsys):
        path = self._write(tmp_path, [slash_command("/model", "opus"), _user("hello")])
        assert emit(path, k=8) == 0
        assert "SKILLS INVOKED" not in capsys.readouterr().out
