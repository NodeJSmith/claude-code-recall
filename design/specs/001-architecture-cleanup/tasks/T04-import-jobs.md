---
task_id: "T04"
title: "Migrate import to DB jobs"
status: "planned"
depends_on: ["T01", "T03"]
implements: ["FR#10", "FR#11", "FR#12", "AC#9", "AC#10", "AC#11"]
---

## Summary

Add a small DB-backed jobs table and migrate import spawning from PID-file-only coordination to durable job records. Keep the worker one-shot and preserve hook stdout. Leave sync-current, summary backfill, migration, and embedding backfill on their existing process models for this pass.

## Target Files

- modify: `src/ccrecall/schema.py`
- create: `src/ccrecall/jobs.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `src/ccrecall/hooks/memory_setup.py`
- modify: `src/ccrecall/hooks/import_conversations.py`
- modify: `src/ccrecall/runtime_files.py`
- modify: `tests/test_db.py`
- modify: `tests/test_sync_hook.py`
- modify: `tests/test_import_pipeline.py`
- modify: `tests/test_legacy_migration.py`
- read: `design/specs/001-architecture-cleanup/design.md`
- read: `design/specs/001-architecture-cleanup/tasks/context.md`

## Prompt

Implement the `Architecture -> DB-Backed Jobs` section for import only.

Add `jobs` DDL with fields and indexes from the design. Create `src/ccrecall/jobs.py` with focused helpers to enqueue by `dedupe_key`, claim one queued/stale job transactionally, mark success, mark failure with `last_error`, and determine stale-running jobs by `heartbeat_at` timeout.

Dedupe must not suppress future imports forever. Enqueue by dedupe key should no-op only when an existing job is `queued` or non-stale `running`. If the existing job is `succeeded`, `failed`, or stale `running`, enqueue should reopen that row as `queued`, clear `last_error` and terminal timestamps as appropriate, and leave enough attempt/accounting information for the worker to update on claim/failure.

Modify `memory_setup.py` so first install or `_needs_reimport()` enqueues an import job such as `dedupe_key='import:all'`, then spawns a one-shot worker command. Keep hook stdout JSON-only and log failures best-effort.

Add a CLI/internal command for the one-shot import worker using the existing cyclopts wrapper style. The worker should claim one import job, run the existing import implementation, and mark terminal status. Keep a transitional PID guard around the worker if needed, but do not use PID files as the durable source of truth.

Do not migrate `sync-current`, summary backfill, migration, warm-model, or embedding backfill to jobs in this task.

## Focus

`memory_setup._spawn_background()` currently guards import, summary backfill, migration, and warm-model with PID files. `tests/test_sync_hook.py` has many tests for `_spawn_background()` and warm-model spawning; update only the import path expectations, not every PID-managed process. `tests/test_legacy_migration.py` expects warm-model spawns on SessionStart; T02 may gate warm-model on semantic availability, so align those tests with the semantic-availability helper.

## Verify

- [ ] FR#10: SessionStart hook still prints valid JSON if job enqueue or worker spawn fails.
- [ ] FR#11: Import background work is represented by a DB job with dedupe and terminal status.
- [ ] FR#12: Import job processing uses a one-shot worker and no daemon.
- [ ] AC#9: SessionStart/Stop stdout tests still pass after import job changes.
- [ ] AC#10: Repeated SessionStart setup attempts create at most one queued/running import job for the same dedupe key, while a later reimport can reopen a terminal `import:all` job.
- [ ] AC#11: A worker claims an import job, marks running, then marks succeeded or failed with `last_error`.
