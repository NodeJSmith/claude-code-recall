# Known Issues

Durable issues discovered during orchestration that were intentionally not fixed in this run.

## KI-001: `hooks/backfill_runner.py` was missed by this design's file audit

Status: resolved — confirmed FR#6-exempt, no code change
Run: 102
Source: T05 (deferred) → known-issues walkthrough (resolved)
Reason not fixed now: N/A — resolved by audit
Observed in: T05 (executor audit during AC#1 verification)
Affected files:
- src/ccrecall/hooks/backfill_runner.py

Issue:
This file has zero `getLogger` calls (shows up in `grep -rL getLogger src/ccrecall/*.py src/ccrecall/hooks/*.py`) but is not named anywhere in design.md or context.md's target-file lists for T01-T05. It was introduced by PR #153 ("refactor: extract shared backfill batch-loop and transcript tree-walk"), which landed *before* this design's own sketch commit (`8b41a2e`) — meaning the design's original file audit (issue #145) should have caught it but didn't.

Why deferred (original T05 judgment):
Out of every task's Target Files in this design; fixing it then would have expanded scope beyond the approved plan. The executor read the full file (88 lines) and found exactly one swallow-worthy construct — `with contextlib.suppress(AttributeError, OSError): os.nice(nice_level)` in `lower_scheduling_priority`, a best-effort `os.nice(2)` call explicitly documented in its own docstring as "either way the run proceeds." This matches the same established "intentional, non-fatal swallowing" idiom already confirmed for `hooks/subprocess_utils.py` in this same design — so it would likely land as a legitimate FR#6 no-op, not a real fix, if brought into scope. Low severity: no user-visible breakage, no data loss, no security exposure, and the core workflow is not blocked (clears the Severity Gate).

Resolution (known-issues walkthrough):
A dedicated fixer + independent reviewer dispatch read the full file against `rules/common/logging.md`'s decision tree. `run_batch_loop` and `limit_reached` have no `except`/`suppress` blocks and no I/O — no qualifying boundary. `lower_scheduling_priority`'s `contextlib.suppress(AttributeError, OSError)` around `os.nice()` is structurally identical to the already-approved FR#6-exempt precedent in this same design (`subprocess_utils.py`'s `try_load_libc`/`reclaim_memory`): a platform-conditional syscall, caught by specific exception types, whose failure the caller (`backfill_embeddings.py`, `backfill_tool_content.py`, both fire-and-forget) proceeds through identically either way. No operator-relevant signal is lost by staying silent — confirmed FR#6-exempt, file left unchanged. This conclusion was independently re-confirmed during a later code-review pass on the same branch, including checking whether an upcoming PR to drop Windows support would change the analysis — it doesn't: the exemption rests on `OSError` (permission denied), which is platform-independent. (The `AttributeError` half of the except tuple exists only to cover `os.nice` not existing on Windows; once Windows support is actually dropped, that half becomes dead code and can be simplified to `except OSError` in whatever PR does the drop — not a logging concern, and out of scope here.)

Acceptance criteria:
- `hooks/backfill_runner.py` is read in full against `rules/common/logging.md`'s decision tree. — done.
- Either a log call is added at a genuine dark-operation boundary, or the file is confirmed FR#6-exempt with the same rigor T01-T05 applied to their target files. — confirmed FR#6-exempt; see Resolution above.

## KI-002: `search_cli.py` and `status.py` add persisted logging on top of existing CLI print output, in tension with `logging.md` rule 2 ("CLI Tools: Print, Not Logging")

Status: open
Run: 102
Source: final-review (code-review.md pass 2, MEDIUM)
Reason not fixed now: needs-decision
Observed in: T01 (DB/status-facing logging task)
Affected files:
- src/ccrecall/search_cli.py (lines 114-121, 152-154)
- src/ccrecall/status.py (lines 144-149)

Issue:
`rules/common/logging.md` rule 2 ("CLI Tools: Print, Not Logging") states: for a process that runs once and whose invoker reads its result from stdout, "print calls *are* the logging... Don't introduce the `logging` module here." T01 added `log.exception(...)` / `log.warning(..., exc_info=True)` calls to three `except` blocks in `search_cli.py` and `status.py`, each of which already surfaces the same failure to the user via `emit_error(...)` or a `print(...)` fallback line. Verified directly: `search_cli.py:114-121` (`except Exception as e:` in the message-search path — calls `log.exception` then `emit_error(str(e), ...)`), `search_cli.py:152-154` (`except sqlite3.Error as e:` in `print_status` — calls `log.warning(..., exc_info=True)` then prints the same error inline), and `status.py:144-149` (`except (sqlite3.Error, OSError) as exc:` in the embedding-backfill status block — calls `log.warning(..., exc_info=True)` then returns `error: str(exc)` in the dict that the caller prints/serializes). Confirmed both modules are CLI-only: `grep -rn` across `src/ccrecall/` shows the only importers of `search_cli`/`status` are `src/ccrecall/cli/commands.py` (`from ccrecall import search_cli as search_mod` / `from ccrecall import status as status_mod`) — no other module reaches into either file. Both already declare `log = logging.getLogger(LOGGER_NAME)` at module level (no `basicConfig`/handler wiring of their own), so this isn't a rule-1 "library configures its own logging" violation — it's rule 2's narrower claim that the `logging` module has no place in a pure CLI-print entry point at all, which these three call sites now contradict on their face.

Why deferred:
This design (014-add-logging-to-core-modules) explicitly scoped `search_cli.py` and `status.py` into Task 1 as "DB/status-facing (highest risk — swallow `sqlite3.Error`/`OSError` today)" (design.md lines 57, 61, 75) and AC#1's grep check treats them as expected `getLogger` adopters, with no design-time carve-out for CLI-only rule 2 exemption. The tension is real and was not reconciled anywhere in the approved design, but resolving it is a product/architecture call, not a mechanical fix: removing the `log.*` calls to match rule 2 literally would lose durable, greppable failure records in the per-process log file (`~/.ccrecall/ccrecall-cli.log`) for search/status failures that today are visible only transiently in the terminal — a real regression for anyone debugging a one-off failed invocation after the fact (e.g. an agent-invoked `ccrecall search` failure whose stderr scrolled off). Conversely, keeping both print and log is a defensible deliberate exception (these are diagnostic CLI subcommands consumed by both humans and agents, not simple print-and-exit tools), but that argument should be made explicitly and either codified as a documented exception in `logging.md` or reverted to match the rule as written — not left as a silent, unexplained inconsistency for the next reader of `logging.md` to trip over. Severity Gate: does not trip — the underlying failures are still fully surfaced to the user via `emit_error`/print in the same code path, so there is no user-visible breakage, no silent data loss, no security exposure, and no core workflow blockage; only the log-vs-print convention is at stake.

Recommended follow-up:
Human decision: either (a) revert the three `log.*` calls in `search_cli.py`/`status.py` to match `logging.md` rule 2 exactly (CLI-only entry points don't touch the `logging` module), or (b) keep them and add an explicit, named exception to `logging.md` rule 2 (or a project-local note in this repo's CLAUDE.md) documenting that agent-invoked diagnostic CLI subcommands with a persisted per-process log file are allowed to log in addition to printing, so future contributors don't have to re-derive this tension from scratch.

Acceptance criteria:
- `logging.md` rule 2 (or a project-local addendum) explicitly states whether `search_cli.py`/`status.py`-style diagnostic CLI commands may log in addition to printing, or the `log.*` calls at the three cited sites are removed to match the rule as written.
- No future code-review finding re-raises this same tension for `search_cli.py`/`status.py` without a documented resolution to point to.
