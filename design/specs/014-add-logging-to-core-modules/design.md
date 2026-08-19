# Design: Add Logging to Silent Core Modules

**Date:** 2026-08-19
**Status:** draft
**Mode:** sketch

## Problem

A codebase audit (issue #145) found 25 of 34 core library modules and 4 hook-side modules in `ccrecall` have zero logging — no `import logging`, no `getLogger` call. Several of these modules swallow exceptions (`sqlite3.Error`, `json.JSONDecodeError`, `FileNotFoundError`, `OSError`) without a trace, which is exactly the "dark operation" failure mode `rules/common/logging.md` warns about: a failure that's invisible until a downstream symptom forces someone to reverse-engineer the cause. PRs #149/#150 already fixed 4 core modules and 1 hook module (`search_vector.py`, `db_vec.py`, `db_base.py`, `embeddings.py`, `hooks/session_selection.py`) using the project's established convention (`log = logging.getLogger(LOGGER_NAME)`, imported from `ccrecall.models`). This work finishes the audit for the remaining 22 core modules and the remaining hook modules (the issue's own count of "21 core modules" undercounts by one — its listed file names total 22; see Approach for the corrected hook-module count too).

**Correction to the issue's hook count**: the issue's audit grepped for a literal `getLogger` call and found 4 hook modules missing one. Direct investigation during this sketch (via a fine-toothed-comb review — see Approach) found the issue's grep undercounted: `hooks/memory_context.py` and `hooks/memory_setup.py` also have genuinely silent paths — they weren't flagged because they import `setup_logging`/`log_hook_exception` from `ccrecall.config` (so *some* code in the file logs), but each also has a distinct `except` block that swallows silently with no log call at all, which is the same failure mode the issue is about. The real hook-module scope is 6 modules, not 4: 3 need an actual fix (`clear_handoff.py`, `memory_context.py`, `memory_setup.py`), 3 are audit-confirmed to need no change (`backfill_query.py`, `subprocess_utils.py`, `tool_content_eligibility.py`).

## Goals

- Every module with a genuine external I/O boundary or non-reraising `except` block gets a `log = logging.getLogger(LOGGER_NAME)` and the log calls that boundary needs, per CLAUDE.md's Mandatory Coverage rules.
- Follow the existing convention exactly (`from ccrecall.models import LOGGER_NAME`; never `logging.basicConfig`, never a handler, never `setLevel` — that's the importing application's job, not a library module's).
- Don't force logging into modules that have no qualifying boundary (pure string/query builders, dataclass-shaped helpers) — that's decoration, not coverage.

## Functional Requirements

- **FR#1** Every non-reraising `except` block in the 22 target core modules and the target hook modules that currently swallows an error silently logs the outcome (`log.exception()` inside the `except`, or `log.error()` for a failure not from a caught exception), per the decision tree in `rules/common/logging.md`.
- **FR#2** Modules that parse many items in a loop (`parsing.py`'s `parse_jsonl_file`, `parse_lines_with_uuids`, `parse_lines_with_uuids_and_numbers`) log a per-call summary (e.g. count of skipped malformed lines) rather than one log line per skipped line, matching the "log the summary after, not each step during" rule.
- **FR#3** External I/O boundaries in the target modules (file stats, DB reads not already covered by `db.py`/`db_vec.py`) log on failure at the appropriate level.
- **FR#4** Every target module that gains a log call uses `log = logging.getLogger(LOGGER_NAME)` (imported from `ccrecall.models`), matching the pattern in `search_vector.py`, `db_vec.py`, `db_base.py`, `embeddings.py`, `hooks/session_selection.py`. No module configures a handler, calls `basicConfig`, or calls `setLevel`.
- **FR#5** Every hook module with a silent validation/DB-check/retry swallow logs it without disturbing the module's existing top-level exception guard: `hooks/clear_handoff.py`'s inner `except ValidationError: return`, `hooks/memory_context.py`'s inner `except ValidationError: hook_input = HookInput()`, `hooks/memory_setup.py`'s `_needs_reimport`/`_needs_backfill` (`except (sqlite3.Error, OSError): return False`), and `hooks/memory_setup.py`'s `_spawn_background` dead/corrupted-PID reap branch (`except (ValueError, OSError): ... continue`) all currently swallow with no log call, distinct from each file's own outer `except Exception:` guard (which already calls `log_hook_exception`) or absence thereof. Not every `except`/`contextlib.suppress` in these files qualifies — `_spawn_background`'s `except FileExistsError:` (the retry loop's own control-flow mechanism) and its nested PID-file `contextlib.suppress(OSError)` unlink stay as-is; see Approach and T05 for the per-branch reasoning.
- **FR#6** Modules audited and found to have no qualifying boundary (no I/O, no swallowed exception, no meaningful state transition) are left unchanged; the task's Verify step records that decision instead of forcing a no-op `getLogger` import.

## Acceptance Criteria

- **AC#1** `grep -rL getLogger src/ccrecall/*.py src/ccrecall/hooks/*.py` returns only: (a) `src/ccrecall/__init__.py`, `src/ccrecall/hooks/__init__.py`, `src/ccrecall/db.py`, `src/ccrecall/tail_pending.py` (out of scope, see Approach); (b) the hook modules that already log via `setup_logging()`/`log_hook_exception()`/an injected `logger` parameter without a literal `getLogger` call in their own file — `memory_sync.py`, `warm_model.py`, `backfill_embeddings.py`, `backfill_summaries.py`, `backfill_tool_content.py`, `backfill_status.py`, **and, even after its FR#5 fix lands, `memory_context.py`, `memory_setup.py`, and `clear_handoff.py`** (all three fixes reuse/inject an existing `setup_logging()`-derived logger rather than adding a fresh module-level `getLogger(LOGGER_NAME)` — see Approach and T05; `clear_handoff.py`'s fix originally followed `import_conversations.py`'s module-level pattern per T05's prompt, but was changed during T05's review-fix loop to call `setup_logging()` inside `main()`'s guarded try block instead, since the module-level `getLogger()` alone never reached a configured handler without it — see T05's fix-ledger and code-review history); (c) any module found during T01-T05 to have no qualifying boundary and left unchanged per FR#6 (this design names a few examples — e.g. `errors.py`, the audit-only hook modules — but does not enumerate every case in advance; T03 and T04 in particular expect several of their target files to land here, since some currently have zero `except` blocks). A grep-based check alone cannot distinguish (b) from a real gap — cross-check its output against this list rather than trusting the grep verbatim (this is exactly the mistake the original issue's audit made).
- **AC#2** `uv run pytest` passes with no new failures.
- **AC#3** `uvx prek run --all-files` passes (no lazy-import violations, no lint/type errors from the new `logging` imports).
- **AC#4** Manually triggering a failure path covered by FR#1 (e.g. malformed JSON in a transcript file processed by `parsing.py`, or a missing file in `ingestion_status.py`'s `_source_fingerprint`) produces a log line in the relevant per-process log file (`~/.ccrecall/ccrecall-<process>.log`), confirmed by reading that file after the run — not by a log-capture test (CLAUDE.md/testing.md forbids asserting on log output in tests).

## Approach

**Convention** (unchanged from #149/#150): `import logging`, `from ccrecall.models import LOGGER_NAME`, `log = logging.getLogger(LOGGER_NAME)` at module level, nothing else. Library modules never touch handlers/levels — `config.py`'s `setup_logging()` is the only place that does, per CLAUDE.md invariant and `rules/common/logging.md` rule 1.

**Scope correction from the issue's audit (core modules)**: the issue listed core modules by name; the actual list totals 22 files (its own "21" label undercounts by one), all confirmed via `grep -rl getLogger src/ccrecall/*.py` to have no `getLogger` call. Two files not mentioned in the issue also have none: `db.py` and `tail_pending.py`. `db.py`'s only `except` block (`get_connection`'s rollback-then-`raise`) already re-raises, so it's exempt from FR#1's mandatory-log rule; adding a log there is optional polish, not coverage, and is left out of this sketch's scope to keep the diff matched to the issue. `tail_pending.py` has zero `except` blocks and no I/O — no qualifying boundary, left unchanged. `__init__.py` is a bare package marker, out of scope.

**Scope correction from the issue's audit (hook modules) — a second, more consequential gap**: the issue's audit used a literal `getLogger` grep, which produces false negatives for hook entry points that log through `ccrecall.config`'s helpers instead of calling `getLogger` themselves. Two conventions coexist in `hooks/`:
- **Library-style** (`getLogger(LOGGER_NAME)` at module level) — used by already-fixed modules like `hooks/session_selection.py`, `hooks/context_alerts.py`, `hooks/import_conversations.py`.
- **Hook-main-style** (`config.setup_logging(settings, process_name=...)` called inside `main()`, returning a configured logger; or the fire-and-forget `config.log_hook_exception(context)` for a top-level guard) — used by `hooks/memory_sync.py`, `hooks/warm_model.py`, `hooks/backfill_embeddings.py`, `hooks/backfill_summaries.py`, `hooks/backfill_tool_content.py`, and `hooks/backfill_status.py` (the last one doesn't call either directly — it takes `logger: logging.Logger` as a parameter from its caller, per the project's "Dependencies as Parameters" convention).

Neither convention is wrong, but a `grep -rL getLogger` sweep can't see the second one, and a file using the second convention can still contain an *unrelated* silently-swallowed except block the grep would never catch either way. Direct investigation (via a fine-toothed-comb pass over this design) found exactly that in two files the issue's audit missed entirely:
- **`hooks/memory_context.py`** already calls `setup_logging()` and logs several state transitions (`logger.info`, `logger.warning`, `log_hook_exception("context")`) — but its `except ValidationError: hook_input = HookInput()` (~line 82) is untouched by any of that and silently swallows malformed hook input, the exact pattern FR#5 fixes in `clear_handoff.py`.
- **`hooks/memory_setup.py`** only reaches `log_hook_exception("setup")` from its top-level `except Exception` in `main()` — but `_needs_reimport` and `_needs_backfill` (lines 73-105) each catch `(sqlite3.Error, OSError)` internally and `return False` before an exception ever reaches that top-level guard. Both are silent today.

Both are added to scope (FR#5, Task 5). Note that fixing `memory_context.py` and `memory_setup.py` does **not** remove them from a literal `getLogger` grep — both fixes reuse or inject an existing `setup_logging()`-derived logger rather than adding a fresh module-level `getLogger(LOGGER_NAME)` (see T05). The 6 hook modules confirmed to need no change at all — `memory_sync.py`, `warm_model.py`, `backfill_embeddings.py`, `backfill_summaries.py`, `backfill_tool_content.py`, `backfill_status.py` — plus `memory_context.py` and `memory_setup.py` post-fix, are all excluded from AC#1's grep-based check explicitly (see AC#1), since the grep alone would otherwise flag all of them as false positives.

**Per-module judgment, not blanket application**: of the 6 hook modules this design actually targets, 3 turn out to have no qualifying boundary once read closely:
- `hooks/backfill_query.py` and `hooks/tool_content_eligibility.py` are pure SQL-fragment builders (string concatenation, no `except`, no I/O). No log call is warranted — FR#6 applies.
- `hooks/subprocess_utils.py`'s two `contextlib.suppress()` blocks (loading `libc.so.6`, calling `malloc_trim`) are the project's established idiom for *intentional* non-fatal swallowing (`rules/common/invariants.md` — "use `contextlib.suppress` with a specific exception type when intentional"). Both failures are expected/handled gracefully by the caller (`reclaim_memory` no-ops when `libc` is `None`). Logging here would be noise on every non-Linux run, not signal. FR#6 applies — document as reviewed, not silently skipped.

The other 3 — `hooks/clear_handoff.py`, `hooks/memory_context.py`, `hooks/memory_setup.py` — need an actual fix (FR#5).

This means the real work is concentrated in the 22 core modules (schema.py, search_cli.py, ingestion_status.py, parsing.py, status.py, and 17 others) plus 3 hook modules, not all 25+ files uniformly. Executors must read each file before editing — this is an audit-and-fix task, not a mechanical find-and-insert.

**Batching** (mirrors the issue's suggested approach): group by concern so each task is independently reviewable and the highest dark-operation risk lands first.

1. **DB/status-facing** (highest risk — swallow `sqlite3.Error`/`OSError` today): `schema.py`, `search_cli.py`, `status.py`, `ingestion_status.py`.
2. **Parsing/serialization**: `parsing.py`, `serialization.py`, `dates.py`, `formatting.py`, `transcript_sources.py`.
3. **Ops modules**: `branch_ops.py`, `message_ops.py`, `project_ops.py`, `session_tail.py`, `tool_content_status.py`, `recent_chats.py`.
4. **Remaining core**: `content.py`, `errors.py`, `file_hashing.py`, `fusion.py`, `summarizer.py`, `search_hydrate.py`, `search_query.py`.
5. **Hooks**: `hooks/clear_handoff.py`, `hooks/memory_context.py`, `hooks/memory_setup.py` (real fixes), `hooks/backfill_query.py`, `hooks/subprocess_utils.py`, `hooks/tool_content_eligibility.py` (audit-and-document-no-op for the latter three).

Each task's executor applies the CLAUDE.md `logging.md` decision tree per file: is this an I/O boundary? does this except block re-raise? would a production operator want to see this in normal operation? — rather than following a prescriptive line-by-line script, since the "right" level (DEBUG/INFO/WARNING/ERROR) and message vary per call site.

## Dependencies and Assumptions

None beyond what's already in the repo — no new dependencies, no schema change. `LOGGER_NAME` and the per-process log file routing already exist in `config.py`/`models.py`.

## Changed Files

- modify: `src/ccrecall/schema.py`, `src/ccrecall/search_cli.py`, `src/ccrecall/status.py`, `src/ccrecall/ingestion_status.py` — DB/status-facing logging (Task 1)
- modify: `src/ccrecall/parsing.py`, `src/ccrecall/serialization.py`, `src/ccrecall/dates.py`, `src/ccrecall/formatting.py`, `src/ccrecall/transcript_sources.py` — parsing/serialization logging (Task 2)
- modify: `src/ccrecall/branch_ops.py`, `src/ccrecall/message_ops.py`, `src/ccrecall/project_ops.py`, `src/ccrecall/session_tail.py`, `src/ccrecall/tool_content_status.py`, `src/ccrecall/recent_chats.py` — ops-module logging (Task 3)
- modify: `src/ccrecall/content.py`, `src/ccrecall/errors.py`, `src/ccrecall/file_hashing.py`, `src/ccrecall/fusion.py`, `src/ccrecall/summarizer.py`, `src/ccrecall/search_hydrate.py`, `src/ccrecall/search_query.py` — remaining core module logging (Task 4)
- modify: `src/ccrecall/hooks/clear_handoff.py` — log the silent `ValidationError` path (Task 5)
- modify: `src/ccrecall/hooks/memory_context.py` — log the silent `ValidationError` path using the existing `logger` already in scope (Task 5)
- modify: `src/ccrecall/hooks/memory_setup.py` — log the silent `(sqlite3.Error, OSError)` swallows in `_needs_reimport`/`_needs_backfill` (Task 5)
- audit (no functional change expected unless investigation finds otherwise): `src/ccrecall/hooks/backfill_query.py`, `src/ccrecall/hooks/subprocess_utils.py`, `src/ccrecall/hooks/tool_content_eligibility.py` (Task 5)
