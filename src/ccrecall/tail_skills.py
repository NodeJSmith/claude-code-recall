"""Invoked-skill detection for ``ccrecall tail``.

A skill's body enters context as a main-chain user entry whose text starts with
``Base directory for this skill: <path>``. That entry appears for a user-typed
slash command and for a model ``Skill`` tool call alike, and never for built-in
commands (``/model``, ``/clear``), so it is the one reliable "this skill was
loaded" signal — the same set Claude Code re-injects after a compaction.

The invocation that triggered the load supplies the name the user actually
typed (``ccrecall:ccr-recall``, not just the directory name) and its args: the
parent entry's ``<command-args>`` for a slash command, or the ``Skill`` tool
input referenced by ``sourceToolUseID`` for a tool call. All of these are
undocumented transcript shapes; if one changes, detection degrades to the
directory name with no args rather than failing.
"""

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from ccrecall.content import extract_text_content
from ccrecall.tail_pending import SKILL_BODY_PREFIX, _is_main_chain, clip, tool_use_blocks

# Slash-command wrapper tags on the user entry that precedes a typed skill's body.
_COMMAND_NAME_RE = re.compile(r"<command-name>/?(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_ARGS_CLIP = 80


@dataclass(frozen=True)
class InvokedSkill:
    name: str
    count: int
    last_at: str | None
    last_args: str


@dataclass(frozen=True)
class _SkillLoad:
    name: str
    timestamp: str | None
    args: str


def find_invoked_skills(entries: list[dict]) -> list[InvokedSkill]:
    """Skills loaded into the main chain, one per name, most recently loaded first."""
    by_uuid = {e["uuid"]: e for e in entries if e.get("uuid")}
    skill_inputs = _skill_tool_inputs(entries)

    loads: list[_SkillLoad] = []
    for entry in entries:
        if not _is_main_chain(entry) or entry.get("type") != "user":
            continue
        text, _, _, _, _ = extract_text_content((entry.get("message") or {}).get("content"))
        text = text.lstrip()
        if not text.lower().startswith(SKILL_BODY_PREFIX):
            continue
        skill_dir = text[len(SKILL_BODY_PREFIX) :].split("\n", 1)[0].strip()
        name, args = _invocation(entry, skill_dir, by_uuid, skill_inputs)
        loads.append(_SkillLoad(name, entry.get("timestamp"), args))

    counts = Counter(load.name for load in loads)
    skills: dict[str, InvokedSkill] = {}
    for load in reversed(loads):  # newest first, so a name's first sighting is its latest load
        if load.name not in skills:
            skills[load.name] = InvokedSkill(load.name, counts[load.name], load.timestamp, load.args)
    return list(skills.values())


def format_skills_block(skills: list[InvokedSkill]) -> str:
    lines = ["SKILLS INVOKED (most recent first):"]
    for skill in skills:
        parts = [skill.name]
        if skill.count > 1:
            parts.append(f"x{skill.count}")
        parts.append(f"last {skill.last_at or 'unknown'}")
        if skill.last_args:
            parts.append(f"args: {clip(skill.last_args, _ARGS_CLIP)}")
        lines.append("  " + "  ".join(parts))
    return "\n".join(lines)


def _skill_tool_inputs(entries: list[dict]) -> dict[str, dict]:
    """Map each ``Skill`` tool_use id to its input payload."""
    inputs: dict[str, dict] = {}
    for entry in entries:
        for block in tool_use_blocks(entry, "Skill"):
            tool_id = block.get("id")
            inp = block.get("input")
            if isinstance(tool_id, str) and isinstance(inp, dict):
                inputs[tool_id] = inp
    return inputs


def _invocation(
    entry: dict, skill_dir: str, by_uuid: dict[str, dict], skill_inputs: dict[str, dict]
) -> tuple[str, str]:
    """(name, args) for a skill load, from the first source that has them:
    the ``Skill`` tool call, then the typed slash command, then the skill
    directory's name with no args."""
    fallback_name = PurePosixPath(skill_dir).name

    source_tool_use_id = entry.get("sourceToolUseID")
    inp = skill_inputs.get(source_tool_use_id) if isinstance(source_tool_use_id, str) else None
    if inp is not None:
        return _stripped_or_empty(inp.get("skill")) or fallback_name, _stripped_or_empty(inp.get("args"))

    parent = by_uuid.get(entry.get("parentUuid") or "")
    parent_content = ((parent or {}).get("message") or {}).get("content")
    if not isinstance(parent_content, str):
        return fallback_name, ""
    name = _COMMAND_NAME_RE.search(parent_content)
    args = _COMMAND_ARGS_RE.search(parent_content)
    return (name.group(1).strip() if name else "") or fallback_name, (args.group(1).strip() if args else "")


def _stripped_or_empty(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
