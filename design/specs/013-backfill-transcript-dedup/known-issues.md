# Known Issues

Durable issues discovered during orchestration. Entries record the reasoning at the time they
were deferred (`Reason not fixed now`); a later run may resolve one without removing the entry —
check `Status` and `Resolution` for the current state.

## KI-001: process_batch closures push per-item loop nesting to 6-7 levels

Status: resolved
Run: 99
Source: clean-code
Reason not fixed now: out-of-scope
Observed in: T01, commit 283b937
Affected files:
- src/ccrecall/hooks/backfill_embeddings.py
- src/ccrecall/hooks/backfill_tool_content.py

Issue:
Wrapping each `run()` function's per-item batch loop in a `process_batch(rows)` closure (required by `run_batch_loop`'s callback interface, FR#1) added one nesting level on top of the pre-existing `try -> for -> try -> except` structure. The per-item loop body now sits at 6-7 levels of indentation in both files, past the repo's 4-level "Should" guideline in `coding-style.md`.

Why deferred:
T01's task file explicitly scoped the per-item loop as "relocated, not rewritten" — its content (SAVEPOINT handling, exception taxonomy, counters, progress-message text) was to move into the callback unchanged, not restructured. Flattening the nesting would require extracting the per-item body into a named top-level function taking explicit parameters (cursor, branch/session context, counters) instead of closure-captured locals — a real structural change beyond this refactor's approved boundary, and a legitimate follow-up decision rather than an unambiguous clean-code fix.

Recommended follow-up:
In a future task, extract each `process_batch`'s per-item body into its own top-level helper function with explicit parameters (replacing closure capture of `cursor`/counters), reducing nesting back toward the 4-level guideline. Do this per-file since the two functions' per-item logic is intentionally not shared (FR#2/FR#3).

Acceptance criteria:
- `process_batch`'s per-item loop body in both files is at or below 4 levels of nesting.
- No change to per-item exception taxonomy, SAVEPOINT handling, or progress-message content (behavior-preserving, same as this task's own constraint).
- Existing test suites (`tests/test_backfill_embeddings.py`, `tests/test_backfill_tool_content.py`) pass unchanged.

Resolution:
Extracted each `process_batch`'s per-item body into a top-level helper function taking explicit parameters instead of closure/nonlocal capture: `_embed_one_branch` (returns an `_EmbedResult` NamedTuple) in `backfill_embeddings.py`, `_backfill_one_session` (returns a `_SessionOutcome` enum) in `backfill_tool_content.py`. Progress-line rendering was also extracted into `_format_embed_progress`/`_format_tool_content_progress`. `backfill_tool_content.py`'s `process_batch` was restructured from an if/elif/else dispatch back to the original's guard-clause/early-`continue` shape so the happy path isn't nested one level deeper than the skip paths, with an explicit `raise AssertionError` on an unhandled outcome so an unmatched enum member fails loudly instead of silently miscounting. Verified behavior-preserving: full test suite (1183 tests) and pyright pass unchanged; SAVEPOINT sequences, exception taxonomy, and all log/print message text are unchanged from the pre-extraction code.
