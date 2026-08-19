---
task_id: "T04"
title: "Add logging to remaining silent core modules"
status: "done"
depends_on: []
implements: ["FR#1", "FR#3", "FR#4", "FR#6"]
---

## Target Files

- modify: `src/ccrecall/content.py`
- modify: `src/ccrecall/errors.py`
- modify: `src/ccrecall/file_hashing.py`
- modify: `src/ccrecall/fusion.py`
- modify: `src/ccrecall/summarizer.py`
- modify: `src/ccrecall/search_hydrate.py`
- modify: `src/ccrecall/search_query.py`

## Prompt

Read `design.md` (Approach section) and `tasks/context.md` (Key Decisions 1-6) before starting.

These 7 modules currently have zero logging. For each file:

1. Read the whole file first.
2. Add `import logging` and `from ccrecall.models import LOGGER_NAME` to the import block, then `log = logging.getLogger(LOGGER_NAME)` at module level (match the pattern in `src/ccrecall/search_vector.py` lines 1-12) — but only if step 3 finds a qualifying boundary.
3. For each `except` block found via `grep -n except <file>`: apply the `rules/common/logging.md` decision tree. `errors.py` (53 lines) defines no exception classes and has no `except` blocks — it's two error-formatting helper functions (`emit_error`, `emit_error_return`). Confirm this on read; if so, it has no qualifying boundary and FR#6 applies.
4. `content.py` is CLAUDE.md's documented parse boundary for tool-content extraction (`extract_text_content`) — per that same doc, it's designed to "never raise on malformed input," which means it likely has defensive branches (falsy checks, `.get()` with defaults) rather than `except` blocks. If so, there may be no qualifying boundary here either — verify by reading before adding anything.
5. `fusion.py` and `search_hydrate.py`/`search_query.py` are search-result-shaping modules per CLAUDE.md's "Search decomposition" section — check whether they have any DB calls or external I/O of their own, or whether they're pure Python transforms over already-fetched rows (in which case FR#6 applies: no logging needed).
6. Do not add a log call inside any `except` block that already re-raises.
7. If a module has no qualifying boundary after reading it, leave it unchanged and note that explicitly when reporting your work (FR#6) — do not add a decorative `getLogger()` with no log call.

## Verify

- [ ] FR#1: Every non-reraising `except` block found in these 7 files (if any) logs its outcome.
- [ ] FR#3: Any external I/O boundary found in these files logs on failure.
- [ ] FR#4: For any file where a log call was added, `grep -n "getLogger\|basicConfig\|setLevel\|addHandler"` shows only `getLogger(LOGGER_NAME)`.
- [ ] FR#6: For each of the 7 files, report explicitly whether it received a log call or was found to have no qualifying boundary (and why) — this task's completion report must account for all 7 files, not just the ones that changed.
- [ ] `uv run pytest` passes with no new failures introduced by this task's changes.
