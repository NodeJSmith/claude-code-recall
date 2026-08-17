---
task_id: "T02"
title: "Split session_tail.py into three modules by concern"
status: "done"
depends_on: ["T01"]
implements: ["FR#4", "FR#6", "FR#7"]
---

## Target Files

- create: `src/ccrecall/tail_resolve.py`
- create: `src/ccrecall/tail_pending.py`
- modify: `src/ccrecall/session_tail.py`
- modify: `src/ccrecall/hooks/context_rendering.py`

## Prompt

Split `src/ccrecall/session_tail.py` (653 lines, 4 mixed concerns) into three cohesive modules. This is issue #140.

### Circular import constraint (CRITICAL)

`tail_pending.py` must NOT import from `session_tail.py`, and `tail_resolve.py` must NOT import from `session_tail.py`. Both new modules import only from `ccrecall.content`, `ccrecall.parsing`, `ccrecall.formatting`, `ccrecall.db`, `ccrecall.models`, and stdlib. `session_tail.py` imports from both new modules. This is a one-directional dependency graph — no cycles.

To achieve this, `typed_instruction`, `_is_main_chain`, `clip`, and `_NOISE_PREFIXES` move to `tail_pending.py` (they're dependencies of `find_pending_question`). `session_tail.py` re-imports them: `from ccrecall.tail_pending import clip, typed_instruction, find_pending_question, format_pending_block`.

### Create `src/ccrecall/tail_resolve.py` — path resolution and session selection

Move these functions and constants from `session_tail.py`:

**Functions:**
- `transcript_dir(cwd, projects_dir)` — cwd-to-slug path builder
- `transcript_for_uuid(uuid, cwd, projects_dir)` — locate transcript by session id
- `list_transcripts(pdir)` — list .jsonl files sorted by last event timestamp
- `resolve_target(pdir, selector)` — pick transcript from one dir
- `resolve_target_global(selector, projects_dir)` — cross-project search fallback
- `_last_event_timestamp(path)` — timestamp extraction for sorting
- `_extract_branch(path)` — head-read for branch filter
- `_pick_branch_match(sessions, branch_hint)` — branch-aware selection
- `_build_search_dirs(provided_cwd, *, real_cwd)` — worktree-aware dir list builder
- `_resolve_across_dirs(dirs, selector, *, branch_hint)` — multi-dir search orchestrator

**Constants:**
- `_TIMESTAMP_TAIL_LINES = 20`
- `_BRANCH_HEAD_LINES = 20`

**Imports needed by `tail_resolve.py`:**
- `json`, `logging`, `sys`, `deque` from `collections`, `Path` from `pathlib`
- `Instant` from `whenever`
- `DEFAULT_PROJECTS_DIR` from `ccrecall.db`
- `split_worktree_path` from `ccrecall.formatting`
- `extract_session_uuid` from `ccrecall.parsing`
- `LOGGER_NAME` from `ccrecall.models`

Note: `resolve_target` calls `_last_event_timestamp` (which is also moving), so no cross-module dependency. `_build_search_dirs` calls `transcript_dir` (also moving). All internal refs stay within `tail_resolve.py`.

### Create `src/ccrecall/tail_pending.py` — pending-question detection, formatting, and shared helpers

Move these functions and constants from `session_tail.py`:

**Functions:**
- `clip(text, limit)` — whitespace-collapse + truncate helper
- `_is_main_chain(entry)` — sidechain filter (one-liner)
- `typed_instruction(entry)` — extract user's typed text, filtering noise
- `find_pending_question(entries)` — scan for unanswered AskUserQuestion
- `format_pending_block(payload, *, for_injection)` — render pending question for display

**Constants:**
- `_TEXT_CLIP = 600`
- `_NOISE_PREFIXES` tuple
- `_INJECTION_OPTION_CLIP = 160`
- `_CLI_OPTION_CLIP = 140`

**Imports needed by `tail_pending.py`:**
- `ccrecall.content`: `extract_text_content`, `is_task_notification`, `is_teammate_message`, `is_tool_result`
- `ccrecall.models`: `LOGGER_NAME` (only if logging is needed — check)

Note: `find_pending_question` calls `typed_instruction` and `_is_main_chain`, both of which are also moving to this same module. `format_pending_block` calls `clip`, also moving here. No cross-module dependencies remain.

### Modify `src/ccrecall/session_tail.py` — slim to rendering + CLI

Remove all functions and constants listed above. Add imports from the new modules:

```python
from ccrecall.tail_pending import (
    _is_main_chain,
    clip,
    find_pending_question,
    format_pending_block,
    typed_instruction,
)
from ccrecall.tail_resolve import (
    _build_search_dirs,
    _extract_branch,
    _last_event_timestamp,
    _pick_branch_match,
    _resolve_across_dirs,
    list_transcripts,
    resolve_target,
    resolve_target_global,
    transcript_dir,
)
```

**Re-export coverage (CRITICAL):** `tests/test_session_tail.py` imports 20 names from `ccrecall.session_tail`. All moved names must remain importable from `session_tail` — include every moved function in the re-import blocks above. The test file imports: `_brief_path`, `_build_search_dirs`, `_emit_full`, `_extract_branch`, `_last_event_timestamp`, `_pick_branch_match`, `_resolve_across_dirs`, `_tool_event`, `build_tail`, `emit`, `find_pending_question`, `format_pending_block`, `last_typed_instruction`, `list_transcripts`, `load_tail_entries`, `resolve_target`, `resolve_target_global`, `transcript_dir`, `typed_instruction`. All of these must resolve after the split (either stayed or re-imported).

Also remove the now-unused `from ccrecall.db import DEFAULT_PROJECTS_DIR` from `session_tail.py` (it moved to `tail_resolve.py`) — ruff F401 will flag it.

What stays in `session_tail.py`:
- `load_entries`, `load_tail_entries` — transcript loading
- `last_typed_instruction`, `last_assistant_text` — call `typed_instruction` (re-imported)
- `_brief_path`, `_tool_event`, `build_tail` — tail event rendering; `build_tail` calls `typed_instruction`, `clip`, `_is_main_chain` (all re-imported)
- `first_typed_preview` — calls `clip` and `typed_instruction` (re-imported)
- `_emit_header`, `emit`, `_emit_full` — CLI output
- `run` — CLI orchestrator; calls functions from both new modules
- Constants: `DEFAULT_TAIL_EVENTS`, `_HOOK_TAIL_LINES`, `_PREVIEW_CLIP`, `_TOOL_CLIP`

### Update `src/ccrecall/hooks/context_rendering.py`

Change:
```python
from ccrecall.session_tail import (
    find_pending_question,
    format_pending_block,
    load_tail_entries,
    transcript_for_uuid,
)
```
To:
```python
from ccrecall.session_tail import load_tail_entries
from ccrecall.tail_pending import find_pending_question, format_pending_block
from ccrecall.tail_resolve import transcript_for_uuid
```

### Verification

After the split:
- `wc -l src/ccrecall/session_tail.py` — must be under 300
- `wc -l src/ccrecall/tail_resolve.py` — must be under 250
- `wc -l src/ccrecall/tail_pending.py` — must be under 150
- `python -c "from ccrecall.tail_pending import find_pending_question; from ccrecall.session_tail import run"` — exits 0, no circular import
- `uv run pytest tests/test_session_tail.py -v` — all existing tests pass
- `uv run pytest -q` — full suite passes

## Verify

- [ ] FR#4: `session_tail.py` under 300 lines; `tail_resolve.py` under 250 lines; `tail_pending.py` under 150 lines
- [ ] FR#6: `python -c "from ccrecall.tail_pending import find_pending_question; from ccrecall.session_tail import run"` exits 0
- [ ] FR#7: `uv run pytest -q` — all tests pass, count unchanged (1178)
- [ ] AC#7: `uvx prek run --all-files` passes clean
