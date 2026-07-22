---
task_id: "T01"
title: "Add generic tool content extraction to extract_text_content"
status: "planned"
depends_on: []
implements: ["FR#1", "FR#2", "FR#9", "AC#1", "AC#7", "AC#8"]
---

## Summary
Extend `extract_text_content` in `content.py` to return a fifth value (`tool_content`) containing searchable text markers extracted from `tool_use` blocks. Use a generic field-join approach that iterates top-level string-valued input fields, automatically covering new tool types. Add cap constants and robustness guarantees (extraction must never raise on malformed input). Include comprehensive unit tests for all tool types, malformed inputs, and cap truncation.

## Target Files
- modify: `src/ccrecall/content.py`
- modify: `tests/test_content.py`
- read: `src/ccrecall/session_tail.py` (reference for tool_use input shapes in `_tool_event`)

## Prompt
Extend `extract_text_content` in `src/ccrecall/content.py` to return a 5-tuple: `(text, has_tool_use, has_thinking, tool_summary, tool_content)`. The existing 4 return values are unchanged.

For each `tool_use` block in the message content list, extract the `input` dict's string-valued top-level fields into a marker string formatted as `[ToolName: joined values]`. This is a generic approach — do NOT write a per-tool dispatch table. Instead:

1. Get `name = item.get("name", "")` and `inp = item.get("input", {})` (guard: ensure `inp` is a dict).
2. Iterate `inp`'s items. For each value that is a `str`, cap it at 200 chars and collect it. For values that are lists of dicts (like AskUserQuestion's `questions`), recursively extract string values from the nested structure. Skip non-string, non-list values (booleans, integers, None).
3. Join collected strings with spaces, cap the total at `TOOL_CONTENT_CAP = 300` chars per block.
4. Format as `[{name}: {joined}]` — one per tool_use block, newline-separated.
5. Join all markers into a single `tool_content` string.

Add constants at the top of `content.py`:
- `TOOL_FIELD_CAP = 200` — per-field character cap
- `TOOL_CONTENT_CAP = 300` — per-block total cap

Robustness: wrap the entire per-block extraction in a try/except that catches `Exception` and falls back to `[{name}]` (tool name only, no content). The extraction must never raise regardless of malformed input — missing keys, wrong types, `None` where a list is expected.

See `## Architecture → Extraction layer` in the design doc for the full marker format specification and examples.

Update `tests/test_content.py`:
- All existing `extract_text_content` tests unpack 4 values — add the 5th (`tool_content`) to each.
- Add new tests for tool content extraction covering: Bash (with/without description), AskUserQuestion (questions with options), Agent (subagent_type + prompt), Skill (skill + args), Edit (file_path + old/new strings), Read (file_path), Grep (pattern), Glob (pattern), Write (file_path), MultiEdit (file_path), unknown tool type.
- Add tests for malformed input: missing `input` key, `input` is a string instead of dict, `input` has `None` values where strings expected, nested structure with wrong types.
- Add test for cap truncation: Agent prompt of 1000 chars is capped to 200, total block capped to 300.

## Focus
- `content.py:extract_text_content` (line 11) currently returns a 4-tuple. The 5th value is appended.
- The existing `extract_files_modified` (line 103) and `extract_commits` (line 116) show the codebase's defensive extraction pattern — `.get()` with defaults, `isinstance` checks, no direct indexing.
- `session_tail.py:_tool_event()` (line 222) shows the 9 tool types and their `input` field names — use as a reference for what fields exist, but do NOT duplicate its dispatch table.
- `test_content.py` has ~377 lines of existing tests. The 4-tuple return is asserted throughout — every assertion needs the 5th value added.
- Claude Code's tool `input` payloads are untyped beyond Pydantic's `extra="allow"` — shapes change at patch cadence. Defensive extraction is critical.

## Verify
- [ ] FR#1: `extract_text_content` returns a 5-tuple where the 5th element is a string of `[ToolName: ...]` markers
- [ ] FR#2: Unit tests cover all listed tool types with representative inputs and verify correct marker output
- [ ] FR#9: Unit test verifies an Agent prompt of 1000 chars is capped to 200 chars per field, 300 chars per block
- [ ] AC#1: Unit tests pass for all tool types with well-formed inputs
- [ ] AC#7: Cap truncation test passes
- [ ] AC#8: Malformed input tests pass — extraction never raises, falls back to `[ToolName]`
