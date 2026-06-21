---
task_id: "T03"
title: "Migrate test consumers off migrations; remove migration-behavior tests"
status: "planned"
depends_on: ["T02"]
implements: ["AC#6"]
---

## Summary
The "migrate callers then delete" half of the wave, applied to the test suite. Now that `SCHEMA` is complete (T02), every test that built a DB via `executescript(SCHEMA)` + `migrate_columns(conn)` can drop the `migrate_columns` call, and every `from ccrecall.migrations import ...` can go. The migration-behavior tests in `test_db.py` (which test code being deleted in T04) are removed; still-valid schema assertions among them are re-expressed against `SCHEMA`. After this task `migrations.py` still exists and `db.py` still calls it, but **no test references it** — so T04 can delete the module with the suite staying green. The full suite must pass at the end of this task.

## Target Files
- modify: `tests/conftest.py`
- modify: `tests/test_db.py`
- modify: `tests/test_summarizer.py`
- modify: `tests/test_integration.py`
- modify: `tests/test_search.py`
- modify: `tests/test_sync_hook.py`
- modify: `tests/test_session_ops.py`
- modify: `tests/test_recent_chats.py`
- read: `src/ccrecall/migrations.py`
- read: `src/ccrecall/schema.py`
- read: `design/specs/003-migrations-squash-v6-baseline/design.md`

## Prompt
For every test file that builds a DB with `executescript(SCHEMA)` followed by `migrate_columns(conn)`, remove the `migrate_columns(conn)` call and the `from ccrecall.migrations import migrate_columns` (or similar) import. `SCHEMA` is now complete, so the fixture produces the same schema without it.

1. **`tests/conftest.py`** — in `make_vec_conn` (line ~30) and the `memory_db` fixture (line ~49), delete the `migrate_columns(conn)` call. Remove the `from ccrecall.migrations import migrate_columns` import (line 11). Update the `make_vec_conn` docstring that enumerates "executescript SCHEMA, migrate_columns, ..." to drop the `migrate_columns` step.
2. **`tests/test_summarizer.py`, `tests/test_integration.py`, `tests/test_search.py`, `tests/test_sync_hook.py`, `tests/test_session_ops.py`** — remove every `migrate_columns(conn)` / `migrate_columns(c)` call that follows a `SCHEMA` apply, and remove the `from ccrecall.migrations import migrate_columns` import from each.
3. **`tests/test_recent_chats.py`** — update the fixture docstring (line ~75) that references "SCHEMA + migrate_columns" if it no longer calls it; if it has an actual `migrate_columns` call, remove it.
4. **`tests/test_db.py`** — this is the big one:
   - Remove `from ccrecall.migrations import _migrate_project_paths, migrate_columns, migrate_db` (line 27); after this task `test_db.py` must not import from `ccrecall.migrations` at all (the T01 pin and T02 AC#2 test use `get_db_connection` / `SCHEMA`, not migrations).
   - Delete the migration-behavior suites that exercise code being removed in T04: `TestMigrateColumns` (the migrate_columns-specific assertions), `TestMigrateDb` (lines ~427+), the `_versioned_db` helper (lines ~198-231) and all versioned-DML test classes/functions that depend on it (the v1→v2 backfill, v4, v5, v6 classes; the `_migrate_project_paths` source-inspection test at ~1104; the backup-blocks-nuke test at ~904-930), and any other test whose body calls `migrate_columns`/`migrate_db`/`_migrate_project_paths` to test migration behavior.
   - **Re-express, don't just delete, the still-valid schema assertions:** tests that asserted "a fresh DB has the embedding columns / `idx_branches_embedding_version`" are valid *properties of `SCHEMA`* — keep that coverage but source it from a schema-only DB (these may already be covered by the T02 AC#2 test; if so, delete the migrate_columns-based duplicates rather than keep both). Use judgment: if an assertion only restates AC#2's coverage, remove it; if it covers something AC#2 doesn't, re-express it against `SCHEMA`/`get_db_connection`.
   - Any remaining `test_db.py` test that called `migrate_columns(conn)` merely to *build a working DB* (not to test migration behavior) should switch to a schema-only build (`executescript(SCHEMA)`) or `get_db_connection`.

Do NOT touch `src/` in this task — `migrations.py`, `db.py`, and `memory_setup.py` are deleted/edited in T04. Do NOT touch the token-DB tests (`test_ingest_token_data.py`, the `token_db`/`populated_token_db` fixtures) — they use `ensure_schema`, which is unchanged.

At the end, run the **full** suite (`uv run pytest -q`) — it must pass. `migrations.py` still exists and `db.py` still calls `migrate_columns` (a now-no-op), so production is unaffected; the point of this task is that the *tests* no longer reference migrations.

See `## Test Strategy → Existing Tests to Adapt` and `→ Tests to Remove` in the design doc.

## Focus
- The full list of test files importing migrations (verified via grep): `conftest.py`, `test_summarizer.py`, `test_session_ops.py`, `test_search.py`, `test_db.py`, `test_integration.py`, `test_sync_hook.py`. `test_recent_chats.py` references it in a docstring only.
- `test_search.py` has multiple `migrate_columns` call sites (lines ~32, 296, 688, 724, 843, 863, 876) — catch all of them; some use `migrate_columns(c)` with a different conn var name.
- `test_db.py` is large (~1490 lines) and migration tests are interleaved with non-migration tests (DB path resolution, FTS detection, vec schema, etc.). Only remove migration-behavior tests; preserve everything else. Read class/function boundaries carefully before deleting.
- After removing imports, grep the test tree for any lingering `migrate_columns`/`migrate_db`/`_migrate_project_paths`/`ccrecall.migrations` reference — there must be zero in `tests/` when this task is done.
- Counts in this prompt (line numbers, call-site counts) are pointers from a snapshot, not guarantees — find the real occurrences with grep before editing.

## Verify
- [ ] AC#6: `grep -rn "ccrecall.migrations\|migrate_columns\|migrate_db\|_migrate_project_paths" tests/` returns nothing, and the full suite passes (`uv run pytest -q`) with `migrations.py` still present.
