---
task_id: "T02"
title: "Lift load-bearing embedding DDL into SCHEMA_CORE"
status: "planned"
depends_on: ["T01"]
implements: ["FR#2", "AC#2"]
---

## Summary
Move the only load-bearing DDL that `migrate_columns` adds beyond `SCHEMA_CORE` — the three `branches` embedding columns and the `idx_branches_embedding_version` index — into `SCHEMA_CORE` itself. After this task, `SCHEMA` (= `SCHEMA_CORE + SCHEMA_FTS5`) is the complete schema, so `executescript(SCHEMA)` alone produces a DB equivalent to `SCHEMA + migrate_columns` (minus `token_snapshots`). This is the lift half of lift-before-delete; the T01 pin must remain green (the production `get_db_connection` path produces the identical schema — now `SCHEMA` contributes the columns and `migrate_columns`' adds become no-ops).

## Target Files
- modify: `src/ccrecall/schema.py`
- modify: `tests/test_db.py`
- read: `src/ccrecall/migrations.py`
- read: `design/specs/003-migrations-squash-v6-baseline/design.md`

## Prompt
In `src/ccrecall/schema.py`, inside the `SCHEMA_CORE` string's `CREATE TABLE IF NOT EXISTS branches (...)` block:
1. Add three columns immediately after `summary_version INTEGER DEFAULT 0,` and before the `UNIQUE(session_id, leaf_uuid)` clause, in this exact order:
   - `embedding_version INTEGER DEFAULT 0,`
   - `embedding_model TEXT,`
   - `summary_version_at_embed INTEGER,`
2. Add the index alongside the other branch indexes (after `idx_branches_summary_version`):
   - `CREATE INDEX IF NOT EXISTS idx_branches_embedding_version ON branches(embedding_version);`

Match the column types/defaults exactly to what `migrate_columns` uses today (`src/ccrecall/migrations.py:158-165`): `embedding_version INTEGER DEFAULT 0`, `embedding_model TEXT`, `summary_version_at_embed INTEGER`. Do NOT add `idx_branches_summary_version` — it is already present in `SCHEMA_CORE` (schema.py:56) and is also (harmlessly) created by `migrate_columns`; re-adding it is wrong.

Do not reorder, rename, or retype any existing column. Do not touch the `messages` table (its three migrate_columns columns — `tool_summary`, `is_notification`, `origin` — are already in `SCHEMA_CORE`). Do not touch `SCHEMA_FTS5`/`SCHEMA_FTS4`.

Then add a test to `tests/test_db.py` (AC#2) asserting that a **schema-only** fresh DB — built with `executescript(SCHEMA)` alone, no `migrate_columns` — has:
- The three embedding columns as the **last three rows** of `PRAGMA table_info(branches)`, in the order `embedding_version`, `embedding_model`, `summary_version_at_embed` (the `UNIQUE(...)` constraint is not a column and does not appear as a `table_info` row).
- The `idx_branches_embedding_version` index present.

Run the T01 pin after this change — it must still pass, since `get_db_connection` now gets these columns from `SCHEMA` and `migrate_columns`' `ALTER` becomes a skipped no-op.

See `## Architecture` step 1 and `## Convention Examples` in the design doc for the exact placement.

## Focus
- `SCHEMA_CORE` is `src/ccrecall/schema.py:12-95`; the `branches` table is lines 36-53, ending `summary_version INTEGER DEFAULT 0,` then `UNIQUE(session_id, leaf_uuid)`. Branch indexes are lines 54-56.
- `SCHEMA = SCHEMA_CORE + SCHEMA_FTS5` (schema.py:174) — fixtures and the AC#2 test that use `SCHEMA` automatically pick up the new columns once they're in `SCHEMA_CORE`.
- Column order is load-bearing: `migrate_columns` appends via `ALTER TABLE ADD COLUMN`, which puts them at the end. Placing them last in the `CREATE TABLE` reproduces that order so positional access / `SELECT *` is identical (design `## Edge Cases` — column-order drift). The T01 pin enforces this.
- Existing test_db.py tests at lines ~1376-1490 assert the embedding columns exist via `migrate_columns` (`test_new_columns_exist_via_migrate_columns`, the `memory_db`-fixture column test, the `idx_branches_embedding_version` test). Leave those for now — they still pass (migrate_columns still exists and is idempotent). They are removed/re-expressed in T03/T04. The AC#2 test you add here is the SCHEMA-sourced replacement.

## Verify
- [ ] FR#2: A fresh DB built from `SCHEMA` alone has `branches.embedding_version`, `branches.embedding_model`, `branches.summary_version_at_embed` (as the final three columns) and the `idx_branches_embedding_version` index.
- [ ] AC#2: A `tests/test_db.py` test asserts the three embedding columns are the last three `PRAGMA table_info(branches)` rows (in order) and `idx_branches_embedding_version` exists, on a schema-only DB; and the T01 pin still passes.
