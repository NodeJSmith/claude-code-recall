---
task_id: "T05"
title: "Add logging to clear_handoff.py, memory_context.py, memory_setup.py; audit the 3 no-op hook modules; run full-repo verification"
status: "done"
depends_on: ["T01", "T02", "T03", "T04"]
implements: ["FR#4", "FR#5", "FR#6", "AC#1", "AC#2", "AC#3"]
---

## Target Files

- modify: `src/ccrecall/hooks/clear_handoff.py`
- modify: `src/ccrecall/hooks/memory_context.py`
- modify: `src/ccrecall/hooks/memory_setup.py`
- audit only, no change expected: `src/ccrecall/hooks/backfill_query.py`
- audit only, no change expected: `src/ccrecall/hooks/subprocess_utils.py`
- audit only, no change expected: `src/ccrecall/hooks/tool_content_eligibility.py`

## Prompt

Read `design.md` (Approach section, including "Per-module judgment, not blanket application" and the two "Scope correction" subsections) and `tasks/context.md` (Key Decisions 1-9, especially 8 and 9) before starting. This is the last task in the sequence — it runs after T01-T04 so it can do a full-repo verification pass.

**Important context**: the issue that spawned this work (#145) originally listed only 4 hook modules as needing a fix. Its audit used a literal `getLogger` grep, which missed 2 modules that log through `ccrecall.config`'s `setup_logging()`/`log_hook_exception()` helpers instead of calling `getLogger` directly — and which each *also* contain a genuinely silent except block unrelated to that existing logging. Don't repeat that mistake: presence of *some* logging in a file doesn't mean the specific boundary you're fixing is covered. Read each target file's full contents, not just its grep hits.

**Part 1 — `hooks/clear_handoff.py` (real fix):**

1. Read the file (it's short, ~60 lines). Its outer `except Exception:` block already calls `log_hook_exception("clear-handoff")` (imported from `ccrecall.config`) — do not touch that or add a second top-level handler.
2. The inner `except ValidationError: return` (currently around line 24) is the actual silent path: malformed hook input on stdin is dropped with no trace. Add a log call there. `hooks/import_conversations.py` is already-fixed and has the same shape as `clear_handoff.py` (a genuine `main()` hook entry point, not just a library helper another module imports) — follow its `log = logging.getLogger(LOGGER_NAME)` convention rather than inventing a new one. (`hooks/context_alerts.py` also already calls `logging.getLogger(LOGGER_NAME)` — inline inside one `except` block, not as a module-level `log` variable — and is a library-style helper imported by `memory_context.py` — CLAUDE.md's "memory_context.py decomposition" section — not a hook entry point itself. Use `import_conversations.py`'s module-level `log = logging.getLogger(LOGGER_NAME)` shape as the actual pattern to follow.)
3. Use WARNING or ERROR (not `exception()`, since `ValidationError` here is an expected "malformed/irrelevant input" case, not an unexpected crash) — judge based on how the already-fixed hook modules log their own validation failures.

**Part 1b — `hooks/memory_context.py` (real fix):**

1. Read the whole file (~220 lines). `main()` already assigns `logger = setup_logging(settings, process_name="context")` near the top (line 77) and uses it (`logger.warning`, `logger.info`) plus a separate `log_hook_exception("context")` call in its own top-level `except Exception:` guard (~line 211-212) — do not touch either of those.
2. The silent path is `except ValidationError: hook_input = HookInput()` (~lines 80-83): malformed/empty stdin falls back to a default `HookInput()` with no log call. `logger` is assigned at line 77, before this try/except at lines 80-83, so it's already in scope — add a log call there using that existing `logger` variable (confirm the actual line order yourself when you read the file; don't add a second `getLogger()` call). Use `logger.warning(...)` (malformed hook input is unexpected but the file already recovers gracefully by defaulting the input) with `extra={}` noting the raw input length or a truncated snippet if useful for diagnosis, not the raw content itself (avoid logging potentially large/arbitrary stdin verbatim).

**Part 1c — `hooks/memory_setup.py` (real fix):**

1. Read the whole file (~160 lines) end to end before changing anything — this file has more `except`/`contextlib.suppress` constructs than the two named below, and a partial read is how the prior draft of this task missed some of them. `main()`'s only logging today is `log_hook_exception("setup")` in its top-level `except Exception:` guard (~line 153) — this only fires if an exception propagates that far, which it never does today because the helper functions below all swallow their own errors first.
2. `_needs_reimport` (~line 73) and `_needs_backfill` (~line 88) each have `except (sqlite3.Error, OSError): return False` with no log call. Fix both with `log.exception(...)` — `sqlite3.Error`/`OSError` here indicate a real DB/filesystem problem worth a traceback, not an expected condition.
3. `_spawn_background` (~lines 31-70), the PID-file-guarded process spawner, has three separate `except`/`suppress` constructs — judge each independently, don't apply one verdict to all three:
   - `except FileExistsError:` (~line 48) is the retry loop's own control-flow mechanism (there's a `# noqa: PERF203` comment on this exact line saying so) — hitting it just means another instance's PID file already exists, which is the expected, common case. No log needed; this isn't a dark operation, it's normal branching.
   - `except (ValueError, OSError):` (~line 55), reached when the existing PID is unreadable or belongs to a dead process: the code reaps the stale PID file and retries the spawn. This **is** worth a log — per `rules/common/logging.md`'s decision tree, "something unexpected happened, but the code handled it (retry succeeded)" → WARNING. A dead process holding a stale PID file is exactly the kind of orphaned-process signal worth surfacing (this machine's CLAUDE.md context flags orphan-process thrash as a known operational risk). Add `logger.warning(...)` here, before the `continue`, noting the `pid_key`/stale PID via `extra={}`.
   - The nested `with contextlib.suppress(OSError): pid_path.unlink()` (~line 57) inside that except block is fine to leave as-is — the unlink can legitimately fail if another process already cleaned up the same stale file, and the outer `continue` already handles moving on regardless. No log needed for this specific line.
4. This file also has a fourth suppressed path: `_reap_stale_temp_files`'s `with contextlib.suppress(OSError): if path.stat()...: path.unlink()` (~line 117). Judge it the same way `subprocess_utils.py`'s two `contextlib.suppress()` blocks were judged (see design.md's "Per-module judgment" section): this is a best-effort cleanup of stale temp files where the failure (file vanished or got permission-denied between glob and stat/unlink, likely from a concurrent process cleaning up the same file) is expected and harmless — the loop just moves on. Leave this one as `contextlib.suppress`, no log call — but confirm this judgment yourself by reading the function's docstring and caller context rather than taking this description on faith, and note in your report that you reviewed it (FR#6-style: reviewed and intentionally left alone, not silently skipped).
5. None of `_needs_reimport`, `_needs_backfill`, or `_spawn_background` currently receives a logger. Add a `logger: logging.Logger` parameter to all three (matching the existing convention in `src/ccrecall/hooks/backfill_status.py`, which already takes a `logger: logging.Logger` parameter from its caller rather than creating its own — read that file's signature for the exact shape). In `main()`, call `logger = setup_logging(settings, process_name="setup")` once, near the top (mirroring `memory_context.py`'s pattern — settings is already loaded via `load_settings()` at that point), and pass `logger` into all five call sites (`_needs_reimport(settings)` → `_needs_reimport(settings, logger)`; `_needs_backfill` likewise; all three `_spawn_background(argv, pid_key)` calls, currently at lines ~134/137/149, → `_spawn_background(argv, pid_key, logger)`). This also means `main()`'s existing `log_hook_exception("setup")` guard and the new `setup_logging()` call will both configure the same underlying logger (`getLogger(LOGGER_NAME)`) — `setup_logging` clears and rebuilds handlers idempotently (see its docstring in `config.py`), so calling it once explicitly and having `log_hook_exception` call it again later if an unrelated exception fires is safe, not a double-configuration bug.
6. Do not add anything to `_reap_stale_temp_files` beyond what step 4 concluded, and do not add a log to the `except FileExistsError:` or the nested `pid_path.unlink()` suppress per step 3 — the point of this task is targeted coverage of real gaps, not maximal instrumentation of every except block regardless of whether it's a dark operation.

**Part 2 — audit the other 3 hook modules (expected: no change):**

`design.md` already investigated these and concluded:
- `hooks/backfill_query.py` and `hooks/tool_content_eligibility.py` are pure SQL-fragment builders (string concatenation only) with no `except` blocks and no I/O.
- `hooks/subprocess_utils.py`'s two `contextlib.suppress()` blocks are the established idiom for intentional, non-fatal swallowing (loading `libc.so.6`, calling `malloc_trim`) — both failures are expected and gracefully handled by the caller.

Re-verify this by reading all three files yourself (don't just trust the design doc) — confirm no `except` blocks, no file/network/DB I/O, and that `contextlib.suppress` really is used rather than a bare `except: pass`. If your reading disagrees with the design doc (e.g. you find a real qualifying boundary), add logging there following the same convention as the other tasks and note the discrepancy in your report. Otherwise, leave all three files unchanged and report that explicitly (FR#6).

**Part 3 — full-repo verification (run after Parts 1 and 2, and after confirming T01-T04's changes are present on disk):**

Run these and record the output:

```bash
grep -rL getLogger src/ccrecall/*.py src/ccrecall/hooks/*.py
uv run pytest
uvx prek run --all-files
```

The `grep -rL` output (files with no literal `getLogger`) should now only be:
- `__init__.py`, `db.py`, `tail_pending.py` (out of scope per design.md)
- `memory_sync.py`, `warm_model.py`, `backfill_embeddings.py`, `backfill_summaries.py`, `backfill_tool_content.py`, `backfill_status.py` (already log via `setup_logging()`/`log_hook_exception()`/an injected `logger` parameter instead of a literal `getLogger` call — false positives for this grep, confirmed out of scope in design.md)
- `memory_setup.py` and `memory_context.py` **may still appear here even after your fixes**, since both fixes reuse or inject a `setup_logging()`-derived logger rather than adding a module-level `getLogger(LOGGER_NAME)` — that's expected and correct for both files, not a miss. Verify each fix landed by reading the file, not by the grep. (Do not "fix" this by adding a redundant `getLogger(LOGGER_NAME)` to either file just to make the grep pass — that violates `context.md` Key Decision 8.)
- `src/ccrecall/hooks/__init__.py` (bare package marker, out of scope — same as `src/ccrecall/__init__.py`)
- any file from T01-T04 or this task's Part 2 that was legitimately found to have no qualifying boundary (FR#6)

If any *other* file appears in that list, flag it — don't silently let it slide. This is exactly the check the issue's original audit skipped, which is how `memory_context.py` and `memory_setup.py` got missed the first time.

## Verify

- [ ] FR#4: Each new log call in `clear_handoff.py`, `memory_context.py`, and `memory_setup.py` uses the file's existing logging convention (its own `getLogger`/`setup_logging`/`log_hook_exception` pattern) — no `basicConfig`, no handler, no `setLevel` added anywhere.
- [ ] FR#5: `hooks/clear_handoff.py`'s inner `except ValidationError: return` now logs (outer `except Exception:` untouched); `hooks/memory_context.py`'s `except ValidationError: hook_input = HookInput()` now logs via the existing `logger` variable; `hooks/memory_setup.py`'s `_needs_reimport` and `_needs_backfill` both now log their `(sqlite3.Error, OSError)` catches, and `_spawn_background`'s dead/corrupted-PID reap branch (`except (ValueError, OSError):`) now logs a WARNING before retrying — all three via an injected `logger` parameter. `_spawn_background`'s `except FileExistsError:` and its nested PID-unlink `contextlib.suppress` remain untouched (confirmed not dark operations — see Prompt Part 1c step 3).
- [ ] FR#6: `backfill_query.py`, `subprocess_utils.py`, `tool_content_eligibility.py` are confirmed (by direct read, not assumption) to have no qualifying boundary, and are left unchanged — or, if a real boundary was found, it's fixed and the discrepancy from the design doc is reported.
- [ ] AC#1: `grep -rL getLogger src/ccrecall/*.py src/ccrecall/hooks/*.py` output matches only the expected out-of-scope/no-boundary/hook-convention files listed above — report the full output and account for every entry.
- [ ] AC#2: `uv run pytest` passes with no new failures (full suite, not just this task's files).
- [ ] AC#3: `uvx prek run --all-files` passes.
