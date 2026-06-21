---
task_id: "T01"
title: "Pin DB-write-path behavior before any split"
status: "planned"
depends_on: []
implements: ["AC#1", "AC#2", "AC#4"]
---

## Summary
Establish the characterization-test baseline that every later split must keep green, on the **current** (unrefactored) code. The migration suite already largely exists in `tests/test_db.py` (`TestMigrateColumns`, `TestVersionedMigration`, `TestMigrateDb`, with `_pre_migration_db`/`_versioned_db` old-schema builders) — relocate it into a new `tests/test_migrations.py`, gap-fill any `user_version` gate or row-transform it doesn't already assert, and add a DB-state golden pin for any `sync_session` write path the existing `test_session_ops.py`/`test_import_pipeline.py`/`test_sync_hook.py` leave unasserted. This task changes **tests only** — no `src/` changes. It must be green before T02/T03/T04 begin.

## Target Files
- create: `tests/test_migrations.py`
- modify: `tests/test_db.py`
- modify: `tests/test_session_ops.py`
- read: `src/ccrecall/migrations.py`
- read: `src/ccrecall/session_ops.py`
- read: `tests/conftest.py`
- read: `tests/test_import_pipeline.py`
- read: `tests/test_sync_hook.py`
- read: `design/specs/002-db-write-path-refactor/design.md`

## Prompt
Read `design/specs/002-db-write-path-refactor/design.md` (especially `## Test Strategy`, `## Edge Cases`, `## Acceptance Criteria`) and `tasks/context.md`.

**Relocate the migration suite.** Move these from `tests/test_db.py` into a new `tests/test_migrations.py`, verbatim (keep the assertions exactly): the classes `TestMigrateColumns`, `TestVersionedMigration`, `TestMigrateDb`, and the module-level helpers they use (`_pre_migration_db`, `_versioned_db`, and any `_v3_db*`/`_v4_db*` builders). Carry their imports. Leave any genuinely db-general tests (`TestSchemaCreation`, `TestLoadSettings`) in `test_db.py`. After the move, both files must import-resolve and the full suite must pass unchanged — this is a pure relocation, green before and after.

**Gap-fill the migration pins** so they cover every behavior a later split could break. Cross-check the existing assertions against `migrate_columns`/`migrate_db` in `src/ccrecall/migrations.py` and add any missing pin for:
- Each `user_version` gate end state (v1 notification backfill, v2 teammate backfill, v3 origin backfill, v4 origin-nullify of `'task-notification'`, v5 aggregated_content recompute + INTERRUPTED→ABANDONED disposition rewrite, v6 oversized-JSON truncation including the `last_exchanges=[]` second-pass fallback).
- Idempotence: `migrate_columns` run twice leaves schema **and** `user_version` unchanged on the second call (AC#2).
- The DDL adds (messages: `tool_summary`/`is_notification`/`origin`; branches columns + indexes; `token_snapshots` table + `data_source`).
- Note (do NOT add behavior): the v4 gate calls `_backup_db_before_migration` and **discards** its return — if you pin v4, the DML must run regardless of backup outcome. Do not assert a guard that doesn't exist.

Do **not** add any `foreign_keys` assertion (here or anywhere) — T02's recreate-path `foreign_keys=ON` is non-observable (connection closed after DDL, pragma connection-scoped), so there is nothing to assert; the design names this as an observability gap. `TestMigrateDb`'s boolean/decision matrix is the migrate_db pin and stays unchanged.

**Add a `sync_session` DB-state pin** to `tests/test_session_ops.py` only where the existing tests leave a write path unasserted. Concretely, pin the exact `import_log` row outcome on the NULL-hash-stale path (a stored NULL `file_hash` with a provided `file_hash` is treated as stale and re-processed → row updated, not a `-1` skip) and the exact `-1`-return on an exact non-NULL hash match. Reuse the `memory_db` fixture and `fixtures/*.jsonl`.

Run `uv run pytest -q tests/test_migrations.py tests/test_db.py tests/test_session_ops.py` and then the full `uv run pytest -q` — everything must pass on current `src/`.

## Focus
- `tests/conftest.py`'s `memory_db` runs `executescript(SCHEMA)` then `migrate_columns`. `SCHEMA_CORE` does **not** set `user_version`, so `memory_db` starts at `user_version = 0` and migrate_columns runs **all** gates v1–v6 on every construction — the existing `_versioned_db(user_version=N)` builder is how you pin a specific starting version.
- The migration suite imports from `ccrecall.migrations` (`_migrate_project_paths`, `migrate_columns`, `migrate_db`) — preserve those imports in the relocated file.
- `_migrate_v5`'s final FTS rebuild is wrapped in `contextlib.suppress(Exception)` (FTS table may be absent on FTS-disabled SQLite). Pin the aggregated_content/disposition rewrite, not the FTS index contents, so the pin is portable across FTS builds.
- This is the RED-baseline of the refactor sequence (`sequence-verifiable-units.md`): the pins are the evidence. Commit them as their own unit, green on unrefactored code.

## Verify
- [ ] AC#1: `tests/test_migrations.py` exists and pins `migrate_columns` resulting `table_info` sets, `user_version`, and the v1/v2/v3/v4/v5/v6 row transformations for old-schema starting states; passes on current `src/`.
- [ ] AC#2: a `migrate_columns`-run-twice test asserts the second call leaves schema and `user_version` unchanged; passes on current `src/`.
- [ ] AC#4: a `sync_session` DB-state pin covers the NULL-hash-stale `import_log` update path and the exact-hash `-1` skip; existing `test_session_ops.py`/`test_import_pipeline.py`/`test_sync_hook.py` still pass unchanged.
