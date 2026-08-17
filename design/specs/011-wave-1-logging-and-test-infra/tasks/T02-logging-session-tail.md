---
task_id: "T02"
title: "Add logging to session_tail.py"
status: "done"
depends_on: []
implements: ["FR#5", "FR#6"]
---

## Target Files

- modify: `src/ccrecall/session_tail.py`

## Prompt

Add diagnostic logging to `session_tail.py` which silently skips corrupt transcript entries. Read `design/specs/011-wave-1-logging-and-test-infra/tasks/context.md` for conventions.

Add at the top (after existing imports, before the constants):

```python
import logging
from ccrecall.models import LOGGER_NAME
log = logging.getLogger(LOGGER_NAME)
```

### _last_event_timestamp (around line 321)

The `except json.JSONDecodeError: continue` block silently skips unparseable lines. Add `log.debug("skipping unparseable line in transcript tail", exc_info=True)` before `continue`.

### _extract_branch (around lines 527 and 532)

Two silent error paths:

1. The inner `except json.JSONDecodeError: continue` (line ~527): add `log.debug("skipping unparseable line in branch extraction", exc_info=True)` before `continue`.

2. The outer `except OSError: return None` (line ~532): add `log.warning("failed to read transcript for branch extraction: %s", path, exc_info=True)` before `return None`. This is WARNING because an unreadable file is a real problem, not routine noise.

### Important

- Do NOT change any error handling behavior — the `continue` and `return None` paths stay as-is.
- Use `log.debug` for JSONDecodeError (per-line noise, fires on every corrupt line).
- Use `log.warning` for OSError (file-level failure, should be visible).

### Verification

Run tests: `uv run pytest tests/test_session_tail.py -v` — all must pass.
Run lint: `uvx prek run --all-files` — must pass.

## Verify

- [x] FR#5: `grep 'log.debug.*unparseable.*transcript tail' src/ccrecall/session_tail.py` matches — DEVIATION: Phase 3 clean-code pass (post-execution pipeline Step 4) reworded this message from `"skipping unparseable line in transcript tail"` to `"failed to parse transcript tail line: %s"` (with `path` interpolated) to match the file's dominant "X failed" logging convention and to include the filename, per the codebase's established pattern. The FR's underlying requirement — DEBUG-level log on `json.JSONDecodeError` before `continue` — remains satisfied; this grep pattern is now stale wording, not a functional gap. Verified: `log.debug("failed to parse transcript tail line: %s", path, exc_info=True)` at `_last_event_timestamp`'s `except json.JSONDecodeError:` block.
- [x] FR#6: `grep 'log.debug.*unparseable.*branch' src/ccrecall/session_tail.py` matches AND `grep 'log.warning.*failed to read' src/ccrecall/session_tail.py` matches — DEVIATION (debug half only): same Phase 3 reword, `"skipping unparseable line in branch extraction"` → `"failed to parse branch extraction line: %s"`. The warning half (`log.warning.*failed to read`) is untouched and still matches verbatim. Verified: `log.debug("failed to parse branch extraction line: %s", path, exc_info=True)` and `log.warning("failed to read transcript for branch extraction: %s", path, exc_info=True)`, both in `_extract_branch`.
