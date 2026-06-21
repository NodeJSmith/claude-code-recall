---
task_id: "T05"
title: "Decompose parse_session into line-type handlers"
status: "done"
depends_on: ["T02"]
implements: ["FR#4", "FR#7", "FR#8", "AC#4", "AC#6", "AC#7", "AC#8"]
---

## Summary
Decompose `token_parser.parse_session` (~214 lines) into per-line-type handlers operating on an explicit mutable parse-state, with content-block extraction pulled into its own helper. `parse_session` keeps the file read, the dispatch loop, and the post-loop finalization. Existing `parse_session` characterization tests (`TestParseSessionCharacterization`) must stay green unchanged.

## Target Files
- modify: `src/ccrecall/token_parser.py`
- read: `tests/test_token_parser.py`
- read: `tests/conftest.py`
- read: `tests/token_helpers.py`
- read: `design/specs/001-token-subsystem-refactor/design.md`

## Prompt
Refactor `parse_session` in `src/ccrecall/token_parser.py` per the design doc's `## Architecture → token_parser.py` section.

1. Introduce a small mutable parse-state dataclass (e.g. `SessionParseState`) holding the fields threaded through the loop today: `session: ParsedSession`, `current_turn: Turn | None`, `turn_index: int`, `last_assistant_ts: str | None`, `metadata_captured: bool`.
2. Extract the per-line-type bodies into module-level functions that take the state (and the parsed `line` dict / relevant fields):
   - `handle_assistant_line(...)` — turn finalize/start, user-gap computation, usage update via `_extract_usage`, content-block iteration.
   - `handle_user_line(...)` — `user_msg_count` increment, `tool_result` matching against `current_turn._pending_tools`.
   - `handle_system_line(...)` — `turn_duration` (sets `turn_duration_ms` + `last_assistant_ts`), `stop_hook_summary`/`hook_summary` (hook accumulation), `api_error`.
3. Extract content-block handling (the `for block in content:` body inside the assistant branch — thinking-token estimate, `tool_use` extraction including the `Skill` prefix normalization and `Agent` metadata capture) into a focused helper (e.g. `apply_content_block(state, block)` or `extract_tool_call(block) -> ToolCall` plus a thin caller). Preserve the exact `Skill`/`Agent` normalization logic (the `claude-` prefix strip, `uses_agent` flag, description-as-command fallback).
4. `parse_session` keeps: the `read_text` + line-split with its `(OSError, ValueError)` guard, the per-line `json.loads` + `is_valid(TokenLine, ...)` guards, the metadata-capture block (sessionId/version/slug/entrypoint/gitBranch), the dispatch by `line_type`, and the **post-loop finalization** — append the last `current_turn`, the `session_id` filename-stem fallback, the sidechain stem fixup, and the `return session if session.turns else None`.

Keep all behavior byte-identical. Run `uv run pytest -q tests/test_token_parser.py` (the `TestParseSessionCharacterization` tests must pass unchanged), then the full suite `uv run pytest -q`.

## Focus
- **Mutation is intentional here.** `parse_session` accumulates into `Turn`/`ParsedSession` in place. Keep that model — thread `SessionParseState` and mutate it in the handlers. Do NOT convert to a returns-only/immutable style; that changes semantics and risks behavior drift (explicit exemption in `context.md`).
- **Do not relocate the dataclasses.** `Turn`, `ParsedSession`, `ToolCall`, `JnlFile` must stay importable from `ccrecall.token_parser` — `conftest.py` imports `ToolCall`; `token_helpers.py` imports `JnlFile`/`ParsedSession`/`Turn`; `tests/test_ingest_token_data.py` imports `JnlFile`/`ParsedSession`/`Turn`. Adding `SessionParseState` is fine; moving the existing ones is not.
- **Subtle ordering in the assistant branch:** the "same logical turn?" merge check (`current_turn and current_turn.message_id == mid`) decides whether to finalize+start a new turn or keep accumulating. The usage update then runs on **every** assistant event for that message id (always overwrites with latest). Preserve this — the merge path is a `pass` that falls through to the always-run usage update. Read lines 330-377 carefully.
- `_extract_usage`, `_detect_cache_ttl_ms`, `compute_session_analytics`, `_normalize_worktree_path`, `project_slug`, `discover_jsonl_files`, `should_skip_file`, `record_import` are all outside `parse_session` and **out of scope** — do not touch them. In particular `tests/test_ingest_token_data.py` imports `_normalize_worktree_path`, `project_slug`, `record_import`, and `should_skip_file` by name from `ccrecall.token_parser` — do not rename or inline any of them as incidental cleanup.
- The T02 cost helper also lives in this file (added before this task via `depends_on: T02`). Keep your edits to the `parse_session` region so they don't collide with the helper.
- `current_turn._pending_tools` uses the existing `_`-prefixed field on `Turn` (it's a dataclass field, framework-style pending map) — leave it as-is; preserving behavior outranks the no-underscore preference for an existing field.
- Personal style: module-level functions over methods, no new `_`-prefixed helpers without reason.

## Verify
- [ ] FR#4: `parse_session(path, jnl)` returns a `ParsedSession`/`None` equal to pre-refactor output for identical JSONL — `TestParseSessionCharacterization` passes unchanged.
- [ ] FR#7: Every function in `token_parser.py` is ≤50 lines except thin orchestrators; `parse_session` contains the read, dispatch loop, and finalization only.
- [ ] FR#8: `parse_session` and the dataclasses remain importable from `ccrecall.token_parser` with unchanged signatures.
- [ ] AC#4: The existing `TestParseSessionCharacterization` tests pass without modification.
- [ ] AC#6: A manual/ruff scan confirms no oversized function remains in `token_parser.py`.
- [ ] AC#7: The full test suite passes with zero failures.
- [ ] AC#8: Importing `build_output`, `build_insights_and_trends`, `build_trends`, `parse_session`, `get_pricing`, `turn_cost`, `project_slug`, `Turn`, `ParsedSession`, `ToolCall`, `JnlFile`, `_BASH_ANTIPATTERN_PREDICATE` from their modules succeeds with documented signatures.
