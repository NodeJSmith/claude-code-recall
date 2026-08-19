# Known Issues

Durable issues discovered during orchestration that were intentionally not fixed in this run.

## KI-001: `hooks/backfill_runner.py` was missed by this design's file audit

Status: open
Run: 102
Source: T05
Reason not fixed now: out-of-scope
Observed in: T05 (executor audit during AC#1 verification)
Affected files:
- src/ccrecall/hooks/backfill_runner.py

Issue:
This file has zero `getLogger` calls (shows up in `grep -rL getLogger src/ccrecall/*.py src/ccrecall/hooks/*.py`) but is not named anywhere in design.md or context.md's target-file lists for T01-T05. It was introduced by PR #153 ("refactor: extract shared backfill batch-loop and transcript tree-walk"), which landed *before* this design's own sketch commit (`8b41a2e`) — meaning the design's original file audit (issue #145) should have caught it but didn't.

Why deferred:
Out of every task's Target Files in this design; fixing it now would expand scope beyond the approved plan. The executor read the full file (88 lines) and found exactly one swallow-worthy construct — `with contextlib.suppress(AttributeError, OSError): os.nice(nice_level)` in `lower_scheduling_priority`, a best-effort `os.nice(2)` call explicitly documented in its own docstring as "either way the run proceeds." This matches the same established "intentional, non-fatal swallowing" idiom already confirmed for `hooks/subprocess_utils.py` in this same design — so it would likely land as a legitimate FR#6 no-op, not a real fix, if brought into scope. Low severity: no user-visible breakage, no data loss, no security exposure, and the core workflow is not blocked (clears the Severity Gate).

Recommended follow-up:
File a small follow-up issue (or fold into the next logging-audit pass) to formally audit `hooks/backfill_runner.py` against the same FR#1/FR#3/FR#6 criteria this design applied elsewhere, and add a `log = logging.getLogger(LOGGER_NAME)` + no-op confirmation (or an actual fix, if the read turns out to disagree with this run's judgment).

Acceptance criteria:
- `hooks/backfill_runner.py` is read in full against `rules/common/logging.md`'s decision tree.
- Either a log call is added at a genuine dark-operation boundary, or the file is confirmed FR#6-exempt with the same rigor T01-T05 applied to their target files.
