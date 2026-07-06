---
task_id: "T04"
title: "Split memory_context.py into focused modules"
status: "done"
depends_on: []
implements: ["FR#2", "AC#1", "AC#2", "AC#3"]
---

## Summary
Decompose the 699-line `hooks/memory_context.py` hook into four focused modules. The proactive alert builder, session selection algorithm, and context renderer each become their own module; `memory_context.py` stays as the slim hook entry point. No production code imports from this module — only 2 test files need updating.

## Target Files
- create: `src/ccrecall/hooks/context_alerts.py`
- create: `src/ccrecall/hooks/session_selection.py`
- create: `src/ccrecall/hooks/context_rendering.py`
- modify: `src/ccrecall/hooks/memory_context.py`
- modify: `tests/test_context_injection.py`
- modify: `tests/test_clear_handoff_contract.py`
- read: `src/ccrecall/health.py` (imported by context_alerts)
- read: `src/ccrecall/config.py` (imported by session_selection, context_alerts)
- read: `src/ccrecall/summarizer.py` (imported by context_rendering)
- read: `src/ccrecall/formatting.py` (imported by context_rendering)
- read: `src/ccrecall/serialization.py` (imported by session_selection)

## Prompt
### Module extraction

Read `src/ccrecall/hooks/memory_context.py` fully. Extract functions and constants into new modules per this mapping:

**`src/ccrecall/hooks/context_alerts.py`** (~90 lines):
- `_proactive_alert_block` (lines 102-185)
- Move only the imports this function needs (from `ccrecall.health`, `ccrecall.config`, etc.)

**`src/ccrecall/hooks/session_selection.py`** (~225 lines):
- `_row_to_entry` (lines 214-238)
- `_CANDIDATE_QUERY` SQL constant (lines 241-255)
- `_SESSION_BY_UUID_QUERY` SQL constant (lines 258-265)
- `_find_first_substantive` (lines 268-275)
- `_load_messages_for` (lines 278-303)
- `_finalize` (lines 306-310)
- `_find_cleared_from_session_uuid` (lines 313-359)
- `_select_cleared_sessions` (lines 362-383)
- `select_sessions` (lines 386-455)
- Constants: `HANDOFF_STALE_SECONDS` (line 64), `_CANDIDATE_LIMIT` (line 69)

**`src/ccrecall/hooks/context_rendering.py`** (~115 lines):
- `_build_fallback_context` (lines 458-478)
- `_extract_topic` (lines 481-497)
- `build_origin_block` (lines 500-532)
- `build_context` (lines 535-552)
- `_pending_question_block` (lines 188-211)
- Constant: `TOPIC_PREVIEW_MAX_CHARS` (line 67)

**`src/ccrecall/hooks/memory_context.py`** (slimmed ~170 lines):
- `_emit_empty` (lines 76-78)
- `_emit_with_proactive` (lines 81-99)
- `main` (lines 555-699)
- Constant: `_CHARS_PER_TOKEN_ESTIMATE` (line 73)
- Import from the three new modules

### Hook stdout contract

`memory_context.py`'s `main()` emits `{}` (via `_emit_empty()`) or `{"hookSpecificOutput": {...}}` (via `_emit_with_proactive()` and the success path) — it does NOT emit `{"continue": true}`. Preserve this pattern exactly. The `_emit_empty` and `_emit_with_proactive` helper functions stay in `memory_context.py` since they are stdout helpers specific to the hook entry point.

### Test updates

In `tests/test_context_injection.py` (37 tests):
- Update the import block (lines 18-24) to import from the new modules:
  - `TOPIC_PREVIEW_MAX_CHARS` → from `ccrecall.hooks.context_rendering`
  - `_proactive_alert_block` → from `ccrecall.hooks.context_alerts`
  - `build_context` → from `ccrecall.hooks.context_rendering`
  - `_build_fallback_context` → from `ccrecall.hooks.context_rendering`
  - `select_sessions` → from `ccrecall.hooks.session_selection`
- Update any `monkeypatch` targets for `probe_filesystem` — check which module it's called from and patch in the correct new module path

In `tests/test_clear_handoff_contract.py` (line 26):
- Update `import ccrecall.hooks.memory_context as _memory_context` and the subsequent `_find_cleared_from_session_uuid` extraction to import from `ccrecall.hooks.session_selection`

## Focus
- `_proactive_alert_block` imports from `ccrecall.health` which is structurally guarded to not import vec/fastembed — `context_alerts.py` inherits this safety
- `select_sessions` has two code paths: a "clear" path (handoff file lookup → `_select_cleared_sessions`) and a "startup" path (candidate iteration). Both stay together in `session_selection.py`
- `_pending_question_block` is conceptually a rendering function (it builds a display block) even though it sits at line 188 in the current file — move it to `context_rendering.py`
- The `_emit_empty()` and `_emit_with_proactive()` functions must stay in `memory_context.py` — they are the stdout envelope functions called from multiple paths in `main()`
- `probe_filesystem` is imported from `ccrecall.health` and used inside `_proactive_alert_block` — patches in tests should target `ccrecall.hooks.context_alerts.probe_filesystem` after the move
- After this task, `memory_context.py` should be ~170 lines

## Verify
- [ ] FR#2: `memory_context.py` is decomposed into 4 focused modules (context_alerts, session_selection, context_rendering, slimmed memory_context)
- [ ] AC#1: No created or modified source file exceeds 400 lines
- [ ] AC#2: `uv run pytest` passes with zero failures
- [ ] AC#3: `uvx prek run --all-files` passes
