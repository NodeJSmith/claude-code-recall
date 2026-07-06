---
task_id: "T02"
title: "Add v2 schema migration to drop fork_point_uuid and clean orphans"
status: "planned"
depends_on: []
implements: ["FR#4", "FR#5", "AC#4", "AC#5", "AC#6"]
---

## Summary
Add a v2 schema migration that drops the vestigial `fork_point_uuid` column from the `branches` table and deletes orphan `messages` rows left behind by the v1 migration. Also remove `fork_point_uuid` from `parsing.py`'s branch dict and `session_ops.py`'s INSERT column list (the non-schema references to the dead column).

## Target Files
- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/parsing.py`
- modify: `src/ccrecall/session_ops.py`
- modify: `tests/test_db.py`
- modify: `tests/test_parsing.py`
- read: `src/ccrecall/schema.py` (reference for SCHEMA_CORE — do NOT edit)

## Prompt
### v2 migration in db.py

In `src/ccrecall/db.py`:

1. Bump `SCHEMA_VERSION` from 1 to 2 (line 24).

2. Add `_migrate_to_v2(conn: sqlite3.Connection) -> None` following the `_migrate_to_v1` pattern. The migration runs inside the caller's `BEGIN IMMEDIATE` transaction. Steps in order:

   a. **Delete orphan messages**: `DELETE FROM messages WHERE id NOT IN (SELECT DISTINCT message_id FROM branch_messages)`. These are messages linked only to inactive branches that v1 already deleted.

   b. **Rebuild branches table** without `fork_point_uuid`: Use an explicit column list (not `SELECT *`). Create `branches_new` with every column from the current schema EXCEPT `fork_point_uuid`. Copy data with a matching explicit SELECT column list. Drop old `branches`. Rename `branches_new` to `branches`. The column list must match what `_migrate_to_v1` produces (18 columns including `fork_point_uuid`) minus `fork_point_uuid` (17 columns). Reference the v1 rebuild at `db.py:283-315` for the exact column order.

   c. **Re-create indexes and FTS triggers** on the rebuilt table — same 4 indexes and FTS sync triggers as v1 (the DROP TABLE auto-drops them). Copy these from v1's existing recreation code.

3. In `_apply_migrations`, add `if current < 2: _migrate_to_v2(conn)` after the existing v1 check (around line 395).

**CRITICAL**: Do NOT edit `SCHEMA_CORE` in `schema.py` or `_migrate_to_v1`'s DDL in `db.py`. Both must retain `fork_point_uuid` for fresh-install compatibility (v1 uses `SELECT *` which requires column count match between source and target).

### Remove fork_point_uuid references

In `src/ccrecall/parsing.py`:
- Remove `"fork_point_uuid": None,` from the branch dict (line 152)
- Remove the `fork_point_uuid` documentation from the `find_all_branches` docstring (line 120)

In `src/ccrecall/session_ops.py`:
- Remove `fork_point_uuid` from the INSERT column list in `insert_branch_row` (line 222) and remove `NULL` from the corresponding VALUES
- Remove the "fork_point_uuid is always NULL now" comment from the docstring (line 216)

### Tests

In `tests/test_db.py`:
- Update the schema introspection test (lines 860-879): remove the `fork_point_uuid` tuple `(3, "fork_point_uuid", "TEXT", 0, None, 0)` and decrement the `cid` (first element) of every subsequent column tuple by one (14 rows shift from cid 4-17 to cid 3-16).
- Add new tests for the v2 migration:
  - Seed a DB at user_version=1 with some orphan messages (messages not in branch_messages) and a branches table that has `fork_point_uuid`. Run `get_connection()` which triggers v2. Assert: `PRAGMA user_version` returns 2, `PRAGMA table_info(branches)` has no `fork_point_uuid` column, orphan messages query returns 0.
  - Test fresh install: a brand-new DB should end up at user_version=2 with no `fork_point_uuid` column (v1 creates it, v2 drops it).

In `tests/test_parsing.py`:
- Remove the `assert active["fork_point_uuid"] is None` assertion (line 93) — the key no longer exists in the branch dict.

## Focus
- The v1 migration at `db.py:238-338` is the template — follow the same cursor pattern, same error handling
- `_apply_migrations` at `db.py:341-404` shows the migration dispatch pattern: `BEGIN IMMEDIATE`, re-read version under lock, run migrations sequentially, set `PRAGMA user_version`, commit
- The branches table after v1 has columns (from `db.py:279-299`): id, session_id, leaf_uuid, fork_point_uuid, is_active, started_at, ended_at, exchange_count, files_modified, commits, tool_counts, aggregated_content, context_summary, context_summary_json, summary_version, embedding_version, embedding_model, summary_version_at_embed — use explicit column list excluding fork_point_uuid for the v2 rebuild (17 columns remain)
- `_apply_migrations` sets `PRAGMA foreign_keys = OFF` before migrations and restores in `finally` — v2 inherits this, no additional FK handling needed
- The existing test `test_fresh_db_user_version_matches_schema_version` in test_db.py (line 647) will need its expected version updated from 1 to 2

## Verify
- [ ] FR#4: `PRAGMA table_info(branches)` has no `fork_point_uuid` column after v2 migration
- [ ] FR#5: `SELECT COUNT(*) FROM messages m LEFT JOIN branch_messages bm ON bm.message_id = m.id WHERE bm.message_id IS NULL` returns 0 after v2 migration
- [ ] AC#4: `PRAGMA user_version` returns 2 after `get_connection()`
- [ ] AC#5: Zero orphan messages after v2 migration on a DB seeded with orphans
- [ ] AC#6: `fork_point_uuid` absent from `PRAGMA table_info(branches)` output
