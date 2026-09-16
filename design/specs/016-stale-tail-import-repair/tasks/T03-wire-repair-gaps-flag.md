---
task_id: "T03"
title: "Wire --repair-gaps flag through ccrecall import"
status: "done"
depends_on: ["T01", "T02"]
implements: ["FR#2", "FR#5", "FR#6", "FR#7", "FR#8", "AC#1", "AC#2", "AC#5", "AC#6", "AC#7", "AC#8"]
---

## Target Files

- modify: `src/ccrecall/hooks/import_conversations.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `tests/test_import_pipeline.py`

## Prompt

Read `design/specs/016-stale-tail-import-repair/design.md` (Approach + Smoke Test sections) and `tasks/context.md`. T01 added `ingestion_status.find_repairable_sessions()`; T02 added `hooks/import_repair.repair_sessions()`. This task wires them behind a CLI flag.

In `src/ccrecall/hooks/import_conversations.py`:
- Add `repair_gaps: bool = False` to `run()`'s signature (alongside `db`, `projects_dir`, `project`, `verbose`), threaded through to `_run(..., repair_gaps=repair_gaps)`.
- **Restructure the `if project:` branch's two early returns (FR#6/AC#6)**: currently, `if not project_dir.exists(): print(...); return` and `if not is_safe_project_dir(...): print(...); return` (`import_conversations.py:377-382`) exit `_run()` entirely before the per-project loop even runs, which would also skip the repair-gaps step below since it's unreachable code after a `return`. Change these two checks from early `return`s to an `elif` chain so the function falls through to the code after the `if project: / else:` block regardless of which path was taken:

  ```python
  if project:
      project_dir = projects_dir / project
      if not project_dir.exists():
          print(f"Project not found: {project_dir}")
      elif not is_safe_project_dir(project_dir, projects_dir):
          print(f"Unsafe project path: {project_dir}")
      else:
          sessions, messages, skipped = import_project(conn, project_dir, exclude_projects, _reclaim)
          conn.commit()
          total_sessions += sessions
          total_messages += messages
          total_skipped += skipped
          print(f"Imported {project}: {sessions} branches, {messages} messages")
  else:
      ...  # unchanged all-projects loop
  ```

  This is a deliberate, minor behavior change beyond just enabling repair-gaps: the "Total: N branches, M messages imported" and "Database size" lines printed after the `with get_connection(...)` block (further down in `_run()`, unchanged) will now also print after a bad/unsafe `--project` value (with zero counts), where before they were skipped entirely by the `return`. This is acceptable — those lines being informative even when nothing was imported for that project is more useful than the current silent early exit, and it's what actually lets FR#6/AC#6's repair summary reach the user in the same invocation.

- After the (now always-reached) `if project: / else:` block completes, still inside the same `with get_connection(settings, load_vec=True) as conn:` block so it shares the connection and its final state, add:

  ```python
  repair_failures = 0
  repair_lock_denied = False
  if repair_gaps:
      if not try_acquire_pid_file(PID_KEY):
          print("ccrecall import --repair-gaps: another import is already running — skipping repair")
          repair_lock_denied = True
      else:
          candidates = ingestion_status.find_repairable_sessions(conn)
          repaired_sessions, repaired_messages, repair_failures, repair_unrepairable = import_repair.repair_sessions(
              conn, candidates, on_reclaim=_reclaim
          )
          summary = f"Repaired {repaired_sessions} session(s), recovered {repaired_messages} message(s)"
          if repair_unrepairable:
              summary += f", {repair_unrepairable} could not be repaired (source transcript is missing the required message(s))"
          if repair_failures:
              summary += f", {repair_failures} failed — see ccrecall-import.log"
          print(summary)
  ```

  `repair_unrepairable` (FR#8/AC#8) is reported but does **not** contribute to `repair_failures`/the nonzero exit code — it's an accurate report about the data (the transcript genuinely lacks the expected content), not an operational failure of this invocation. Only `repair_failures` (a poison file raising during processing) triggers the nonzero exit in the `run()` change below.

  `try_acquire_pid_file` is the same helper `sync_current.py` and the backfill CLI path already use for this purpose. It is not currently imported in `import_conversations.py` — add it to the existing `from ccrecall.config import DEFAULT_DB_PATH, get_db_path, load_settings, remove_pid_file, setup_logging` line (`import_conversations.py:16`) rather than a new import statement. Guard only the repair-gaps step this way, not the whole of `run()` — a broader PID guard on every `ccrecall import` invocation (including the common no-flags case run by the SessionStart background auto-import) is a larger, separate change outside this design's scope; FR#7 exists specifically because `--repair-gaps` holds longer transactions than normal import and therefore raises the stakes of colliding with a concurrent instance, not because normal import needs the same guard today.

  No `conn.commit()` call is needed after `repair_sessions()` — per T02, each candidate's files already commit independently (per-file `SAVEPOINT`/`RELEASE`, not wrapped in an outer `BEGIN`), so by the time `repair_sessions()` returns, every completed repair is already durable. Do not add a `BEGIN` guard or a trailing commit here to make the batch "more atomic" — that would silently change the intended per-file durability model back to the all-or-nothing one this design explicitly rejected (see design.md's Approach section).

  **`run()`'s existing `finally: remove_pid_file(PID_KEY)` must not fire when this invocation was denied the lock (AC#7's critical assertion)** — `run()` currently has:

  ```python
  def run(...) -> None:
      try:
          _run(...)
      except Exception:
          log.exception("Import process failed with an uncaught exception")
          raise
      finally:
          remove_pid_file(PID_KEY)
  ```

  This `finally` runs unconditionally today because, before this change, `run()` never itself attempted to acquire `PID_KEY` — the marker (when present) was always written by `_spawn_background`'s parent process for a *different* invocation of this same code, whose own eventual `finally` correctly cleans up after itself. That assumption breaks the moment `--repair-gaps` adds its own `try_acquire_pid_file` call that can *fail* (a real concurrent holder exists): if this invocation's `finally` still unconditionally removes the marker in that case, it deletes the other, still-running process's guard — silently un-guarding it and defeating the whole point of FR#7. Fix: `_run()` must return whether repair-gaps was denied the lock, alongside `repair_failures`, and `run()`'s `finally` must skip the removal in exactly that one case. Every other case (`repair_gaps=False`, or `repair_gaps=True` with a successful acquisition) keeps the exact existing unconditional-removal behavior unchanged — this is not a general fix to `ccrecall import`'s PID-file lifecycle, only to the one new failure mode this feature introduces.

  Change `_run()`'s return type from `-> None` to `-> tuple[int, bool]` (returning `(repair_failures, repair_lock_denied)`, both `0`/`False` whenever `repair_gaps` is `False`), and restructure `run()`:

  ```python
  def run(*, db=DEFAULT_DB_PATH, projects_dir=DEFAULT_PROJECTS_DIR, project=None, verbose=False, repair_gaps=False) -> None:
      repair_failures = 0
      repair_lock_denied = False
      try:
          repair_failures, repair_lock_denied = _run(
              db=db, projects_dir=projects_dir, project=project, verbose=verbose, repair_gaps=repair_gaps
          )
      except Exception:
          log.exception("Import process failed with an uncaught exception")
          raise
      finally:
          if not repair_lock_denied:
              remove_pid_file(PID_KEY)
      if repair_failures:
          raise SystemExit(1)
  ```

  `_run()`'s final `return` statement becomes `return repair_failures, repair_lock_denied` (both locals are already established by the `if repair_gaps:` block above, defaulting to `0, False` when `repair_gaps` is `False` since that block never executes).

  Import `ingestion_status` and the new `import_repair` module at the top of the file (no lazy imports — see `rules/common/python.md`). Per the design's Non-Goals, this step ignores `project` entirely and always scans the whole DB — do not filter `candidates` by the `project` argument.

In `src/ccrecall/cli/commands.py`, add a `--repair-gaps` flag to `cmd_import` following the existing `_FLAG` pattern used elsewhere in this file (e.g. `check_ingestion` on `cmd_status`):

```python
repair_gaps: Annotated[
    bool,
    _FLAG,
    Parameter(help="Force-reimport sessions with a stale-tail or ingestion-gap (see `ccrecall status --check-ingestion`)."),
] = False,
```

Thread it into the `import_mod.run(...)` call.

**Fix an existing test broken by the `if project:` restructuring above.** `tests/test_import_pipeline.py`'s `TestImportRunPathSafety::test_run_rejects_symlink_project_dir` (around line 856) calls `_run(db=db_path, projects_dir=projects_dir, project="linked-project", verbose=False)` directly and asserts the captured stdout is exactly `f"Unsafe project path: {projects_dir / 'linked-project'}\n"`. Two things break:
1. `_run()` now requires a `repair_gaps` argument (no default on `_run()` itself, matching its existing no-default convention for `db`/`projects_dir`/`project`/`verbose`) — add `repair_gaps=False` to this call site.
2. `_run()`'s return type changed to a 2-tuple; this test ignores the return value today and can keep doing so.
3. With the `elif` restructuring, this call no longer hits an early `return` — it falls through to the unchanged "Total: ..."/"Database size" prints at the end of `_run()`. `db_path` in this test is pre-created as an empty file (`db_path.write_text("", encoding="utf-8")`, a few lines above), so `db_path.exists()` is `True` and the "Database size: 0.00 MB" line will also print. Update the assertion to:
   ```python
   assert capsys.readouterr().out == (
       f"Unsafe project path: {projects_dir / 'linked-project'}\n"
       "\nTotal: 0 branches, 0 messages imported (0 unchanged)\n"
       "Database size: 0.00 MB\n"
   )
   ```
   (The leading `\n` before `Total:` matches `_run()`'s existing `print(f"\nTotal: ...")` call, unchanged by this task.)

In `tests/test_import_pipeline.py`, add an end-to-end test:
- Set up a DB + project directory such that a session's `import_log` stat matches its on-disk JSONL, but its `messages` table is missing a trailing row that the transcript has (simulate the same way T02's fixture does, or reuse a helper if one already exists in this file for building session fixtures).
- Run `import_mod.run(db=..., projects_dir=..., project=None, verbose=False, repair_gaps=True)` (or via the CLI command if this test file already drives commands that way — check existing patterns in the file first).
- Assert the missing message row now exists, and that a plain `ccrecall import` run (i.e. the same call with `repair_gaps=False`, or the pre-existing default) does *not* recover it — this is the regression check for the bug this whole change fixes (AC#1's contrast case).
- AC#1 also requires confirming the session's *classification* flips, not just the row insert: after the `repair_gaps=True` run, call `ingestion_status.summarize_ingestion(conn, sources=import_log_source_index(conn.cursor()))` (or `status_mod.collect_status(..., check_ingestion=True)` if that's a closer fit to this file's existing patterns — check what's already imported here) against the same DB and assert the repaired session is no longer counted under `stale_tail_sessions` (nor `ingestion_gap_sessions`).
- Assert running `repair_gaps=True` again when there's nothing to repair prints/reports 0 sessions repaired and doesn't raise (AC#2).
- AC#5: set up a batch with one repairable session plus one poison-transcript session (a file that raises during `import_session` — e.g. malformed JSON), run with `repair_gaps=True`, and assert: (a) `import_mod.run(...)` raises `SystemExit` with a nonzero code, (b) the printed/reported summary includes a nonzero failed count, and (c) the non-poison session's message row was still recovered despite the other candidate's failure.
- AC#6: with a DB that has a repairable session unrelated to any specific project, run `import_mod.run(db=..., projects_dir=..., project="does-not-exist", verbose=False, repair_gaps=True)` — assert it does not raise, and the repairable session's missing message row is recovered anyway (the bad `--project` value did not suppress the DB-wide repair step).
- AC#7: pre-acquire `PID_KEY_IMPORT`'s PID marker (e.g. via `config.try_acquire_pid_file(config.PID_KEY_IMPORT)` in the test itself, simulating a concurrently-running import) before calling `import_mod.run(..., repair_gaps=True)` — assert it does not raise, does not attempt the repair, the would-be-repairable session's message row is still missing afterward (repair was skipped, not silently no-op'd for an unrelated reason), **and the PID marker file (`config.pid_file_path(config.PID_KEY_IMPORT)`) still exists after `run()` returns** — this is the regression check for the `finally`-deletes-someone-else's-marker bug the PID-guard fix above exists to prevent. Release the marker in the test's own cleanup (it was never released by `run()`, since `run()` correctly saw it wasn't the owner).
- AC#8: set up a candidate whose transcript genuinely lacks the expected message content (same fixture shape as T02's unrepairable-case test), run `import_mod.run(..., repair_gaps=True)`, and assert: (a) it does not raise (unrepairable is not a failure exit condition), (b) the printed/reported summary includes a nonzero "could not be repaired" count, distinct from the failed count.

## Note for T02 (informational, no edit needed)

FR#7's PID guard sits entirely in this task's `_run()` change, before `find_repairable_sessions`/`repair_sessions` are ever called — `import_repair.repair_sessions()` itself (T02) needs no awareness of the PID guard.

## Verify

- [ ] FR#2 (CLI half): `ccrecall import --repair-gaps` runs the repair step; a bare `ccrecall import` does not.
- [ ] AC#1: end-to-end test confirms a stale-tail session's missing message is recovered by `--repair-gaps` and not by plain `import`, AND that a post-repair ingestion check no longer counts that session under `stale_tail_sessions`/`ingestion_gap_sessions`.
- [ ] AC#2: end-to-end test confirms a no-op run with `--repair-gaps` on a clean DB reports 0 repaired and exits without error.
- [ ] FR#5 / AC#5: end-to-end test confirms a batch with one poison candidate still repairs the other candidates, reports a nonzero failed count, and `run()` raises `SystemExit` with a nonzero code.
- [ ] FR#6 / AC#6: end-to-end test confirms `--repair-gaps` still runs and repairs a DB-wide session even when `--project` names a nonexistent directory.
- [ ] FR#7 / AC#7: end-to-end test confirms `--repair-gaps` skips (not fails) when the `PID_KEY_IMPORT` marker is already held, without raising, and confirms the marker still exists after `run()` returns (the `finally`-deletion bug is fixed).
- [ ] The existing `test_run_rejects_symlink_project_dir` test is updated (new `repair_gaps=False` argument, new expected stdout including the "Total:"/"Database size" lines) and passes.
- [ ] FR#8 / AC#8: end-to-end test confirms a genuinely unrepairable candidate is reported under a distinct "could not be repaired" count and does not raise or trigger a nonzero exit.
- [ ] `uv run pytest tests/test_import_pipeline.py -q` passes.
- [ ] `uv run pytest` (full suite) passes; `uvx prek run --all-files` clean.
