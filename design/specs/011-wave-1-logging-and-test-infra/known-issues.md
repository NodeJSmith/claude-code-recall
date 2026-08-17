# Known Issues

Durable issues discovered during orchestration that were intentionally not fixed in this run.

## KI-001: db.py's chunk_vec_queryable() has the same silent-except pattern as vec_available()

Status: resolved — fixed during known issues walkthrough
Run: 96
Source: impl-review
Reason not fixed now: out-of-scope (superseded — see Resolution below)
Observed in: T01 (commit 133e796)
Affected files:
- src/ccrecall/db.py

Issue:
`chunk_vec_queryable()` in `db.py` has a silent `except` block that swallows errors without logging, the same dark-operation pattern that T01 fixed in `vec_available()`. It was not in this wave's target list.

Why deferred:
Design.md's Non-Goals explicitly exclude "Rolling out logging to all 25 zero-logging modules (that's wave 4 / #145 remainder)." Fixing this now would expand beyond the approved T01-T03 scope.

Recommended follow-up:
Include `chunk_vec_queryable()` in the wave-4 logging rollout (#145 remainder).

Acceptance criteria:
- `chunk_vec_queryable()`'s except block logs at an appropriate level before its silent-failure return.

Resolution:
User chose "Fix now" during the Step 5.6 known-issues walkthrough (this run). Added `log.debug("chunk_vec table not queryable", exc_info=True)` to `chunk_vec_queryable()`'s `except sqlite3.Error:` block in `src/ccrecall/db.py`. Initially matched `vec_available()`'s `log.warning` level, but a follow-up code-review finding identified that every caller reaching `chunk_vec_queryable()` with `load_vec=True` already runs `vec_available()` on the same connection first — so a genuine extension-load failure would double-log (WARNING at connection-open, then WARNING again here for the same root cause on every subsequent call). Refined to `log.debug()` so the diagnostic signal is preserved without repeat-alerting at WARNING severity. Verified via `uv run pytest tests/test_db.py` (98 passed) and `uvx prek run --all-files` (14/14 hooks passed) after each change.
