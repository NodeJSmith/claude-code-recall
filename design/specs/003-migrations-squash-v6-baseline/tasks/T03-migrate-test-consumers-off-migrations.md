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
   - Remove **both** `migrations` import lines: `import ccrecall.migrations as migrations_module` (line 16) **and** `from ccrecall.migrations import _migrate_project_paths, migrate_columns, migrate_db` (line 27). After this task `test_db.py` must not reference `ccrecall.migrations`, `migrations_module`, `migrate_columns`, `migrate_db`, `_migrate_project_paths`, or `user_version` at all (the T01 pin and T02 AC#2 test use `get_db_connection` / `SCHEMA`, not migrations).
   - Delete these migration-behavior test classes wholesale (verified to exist by name; confirm line numbers with grep before editing, as they shift during edits): `TestMigrateColumns`, `TestVersionedMigration`, `TestMigrateDb`, `TestMigrateProjectPaths`, `TestV4Migration`, `TestMigrateDbBackupGuard`, `TestV5Migration`, `TestV6Migration`. Also delete the module-level setup helpers whose only callers are these deleted classes. The complete orphan list (verified — every caller is inside a class being deleted): `_pre_migration_db`, `_versioned_db`, `_project_path_db`, `_v3_db_with_messages`, `_v4_db_for_v5_tests`. **These helpers contain none of the verify-grep tokens, so the AC#6 grep will NOT flag them if left behind — delete them by name.** Keep `_vec_available_in_env` / `_VEC_AVAILABLE` (~line 1358-1370) — they serve the preserved vec test classes.
     - **Critical — do not rely on the grep alone:** `TestV5Migration.test_migrations_run_on_get_db_connection` exercises migration behavior via `get_db_connection()` + `PRAGMA user_version` and contains **no** `migrate_columns`/`migrate_db` token, so a grep for those would miss it — but it will FAIL in T04 (after `get_db_connection` stops writing `user_version`). Deleting `TestV5Migration` by name removes it. The broadened verify grep (includes `user_version`) is the backstop.
     - Do NOT delete the non-migration classes in this file: `TestSchemaCreation`, `TestLoadSettings`, `TestLoadConfig`, `TestLoadSettingsWithConfig`, `TestLogHookException`, `TestCurrentOnboardingVersion`, `TestVecAvailable`, `TestVecSchema`, `TestLoadVecParameter`.
   - **`TestNewBranchColumns`** (the embedding-columns class, ~line 1373) asserts the three embedding columns + `idx_branches_embedding_version` exist via `migrate_columns`. This coverage is now owned by the T02 AC#2 test (schema-only). Delete `TestNewBranchColumns` (or, if it asserts anything AC#2 doesn't, re-express only that delta against a schema-only DB). Do not keep `migrate_columns`-based duplicates.
   - Any remaining `test_db.py` test that called `migrate_columns(conn)` merely to *build a working DB* (not to test migration behavior) should switch to a schema-only build (`executescript(SCHEMA)`) or `get_db_connection`.
   - **Explicit case — `TestVecSchema.test_raw_no_vec_connection_unaffected` (~line 1490):** this is a **preserved** non-migration test, but its body calls `migrate_columns(conn)` after `executescript(SCHEMA)` and asserts `branch_vec` is never auto-created. The asserted behavior (branch_vec only exists on `load_vec=True` connections) is still true post-squash. Do NOT delete this test — instead **delete just the `migrate_columns(conn)` line** (the `executescript(SCHEMA)` build above it stands on its own) and reword its docstring from "migrate_columns never creates branch_vec" to e.g. "the plain schema path never creates branch_vec". This is the one preserved class containing a `migrate_columns` call; converting it (not deleting it) is what makes the verify grep reach zero without losing valid coverage.

Do NOT touch `src/` in this task — `migrations.py`, `db.py`, and `memory_setup.py` are deleted/edited in T04. Do NOT touch the token-DB tests (`test_ingest_token_data.py`, the `token_db`/`populated_token_db` fixtures) — they use `ensure_schema`, which is unchanged.

At the end, run the **full** suite (`uv run pytest -q`) — it must pass. `migrations.py` still exists and `db.py` still calls `migrate_columns` (a now-no-op), so production is unaffected; the point of this task is that the *tests* no longer reference migrations.

See `## Test Strategy → Existing Tests to Adapt` and `→ Tests to Remove` in the design doc.

## Focus
- The full list of test files importing migrations (verified via grep): `conftest.py`, `test_summarizer.py`, `test_session_ops.py`, `test_search.py`, `test_db.py`, `test_integration.py`, `test_sync_hook.py`. `test_recent_chats.py` references it in a docstring only.
- `test_search.py` has multiple `migrate_columns` call sites (lines ~32, 296, 688, 724, 843, 863, 876) — catch all of them; some use `migrate_columns(c)` with a different conn var name.
- `test_db.py` is large (~1725 lines) and migration tests are interleaved with non-migration tests (DB path resolution, FTS detection, vec schema, etc.). Only remove migration-behavior tests; preserve everything else. Read class/function boundaries carefully before deleting.
- After removing imports, grep the test tree for any lingering `migrate_columns`/`migrate_db`/`_migrate_project_paths`/`migrations_module`/`ccrecall.migrations`/`user_version` reference — there must be zero in `tests/` when this task is done. (`user_version` currently appears only in `test_db.py`'s migration tests, all of which are deleted here, so a zero result confirms completeness.)
- Counts in this prompt (line numbers, call-site counts) are pointers from a snapshot, not guarantees — find the real occurrences with grep before editing.

## Verify
- [ ] AC#6: `grep -rn "ccrecall.migrations\|migrations_module\|migrate_columns\|migrate_db\|_migrate_project_paths\|user_version" tests/` returns nothing, and the full suite passes (`uv run pytest -q`) with `migrations.py` still present.
