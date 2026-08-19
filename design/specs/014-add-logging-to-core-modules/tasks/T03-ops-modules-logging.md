---
task_id: "T03"
title: "Add logging to ops-layer core modules"
status: "planned"
depends_on: []
implements: ["FR#1", "FR#3", "FR#4", "FR#6"]
---

## Target Files

- modify: `src/ccrecall/branch_ops.py`
- modify: `src/ccrecall/message_ops.py`
- modify: `src/ccrecall/project_ops.py`
- modify: `src/ccrecall/session_tail.py`
- modify: `src/ccrecall/tool_content_status.py`
- modify: `src/ccrecall/recent_chats.py`

## Prompt

Read `design.md` (Approach section) and `tasks/context.md` (Key Decisions 1-5) before starting.

These 6 modules currently have zero logging. For each file:

1. Read the whole file first.
2. Add `import logging` and `from ccrecall.models import LOGGER_NAME` to the import block, then `log = logging.getLogger(LOGGER_NAME)` at module level (match the pattern in `src/ccrecall/hooks/session_selection.py` lines 15-28) — but only if step 3/4 finds a qualifying boundary. As a heads-up: `branch_ops.py`, `message_ops.py`, `project_ops.py`, `session_tail.py`, and `tool_content_status.py` currently have zero `except` blocks (confirm this yourself via `grep -n except` — don't assume it's still true by the time you run this task), so FR#6 (no qualifying boundary, leave unchanged) is a live, expected outcome for several of these files, not just a fallback case. Only `recent_chats.py` has an existing `except` block as of this writing.
3. For each `except` block found via `grep -n except <file>`: apply the `rules/common/logging.md` decision tree. Use `log.exception()` for caught-exception failures, `log.error()` for non-exception failures. `recent_chats.py`'s one existing `except Exception as e:` already calls `emit_error(...)` (the project's CLI-print error-output convention from `errors.py` — see `rules/common/logging.md` rule 2, "CLI Tools: Print, not Logging") — if that except block is only reachable from a CLI invocation where `emit_error` already surfaces the failure to the invoker, it may not be a dark operation at all and FR#6 could apply instead of FR#1. Check whether `recent_chats.py`'s functions are also called as a library import (not just via the CLI) before deciding; if so, the library call path still needs a log even though the CLI path is already visible.
4. Look also for state-transition boundaries worth an INFO log per CLAUDE.md's "Mandatory coverage" (a branch/session/project row changing state, a long-running operation completing). CLAUDE.md's `session_ops.py` decomposition section names `branch_ops.py` and `message_ops.py` (2 of these 6 files) as owning branch/message row CRUD and diffing; `project_ops.py`, `session_tail.py`, `tool_content_status.py`, and `recent_chats.py` aren't part of that named decomposition, so read each on its own terms rather than assuming the same shape. (`hooks/session_selection.py`, referenced above for the `getLogger` pattern, belongs to CLAUDE.md's `memory_context.py` decomposition section, not `session_ops.py` — it's cited purely for its logging convention, not as an architectural sibling of these 6 files.)
5. Do not add a log call inside any `except` block that already re-raises.
6. Use `extra={...}` for structured context (branch IDs, session IDs, counts) rather than interpolating into the message string.
7. If a module has no qualifying boundary after reading it (no I/O, no swallowed exception, no meaningful state transition), leave it unchanged and note that explicitly when reporting your work — do not add a decorative `getLogger()` with no log call (Key Decision 5 in context.md).

## Verify

- [ ] FR#1: Every non-reraising `except` in these 6 files logs its outcome (verify via `grep -n except` on each file plus manual read).
- [ ] FR#3: External I/O boundaries (DB reads/writes) in these files log on failure.
- [ ] FR#4: `grep -n "getLogger\|basicConfig\|setLevel\|addHandler"` on each of the 6 files shows only `getLogger(LOGGER_NAME)` where a log call was added, and no import at all for any file where no qualifying boundary was found.
- [ ] FR#6: For each of the 6 files, report explicitly whether it received a log call or was found to have no qualifying boundary (and why) — this task's completion report must account for all 6 files, not just the ones that changed.
- [ ] `uv run pytest` passes with no new failures introduced by this task's changes.
