---
task_id: "T04"
title: "Delete migrations.py and remove its production consumers"
status: "planned"
depends_on: ["T03"]
implements: ["FR#1", "FR#3", "FR#4", "FR#5", "FR#6", "AC#3", "AC#4", "AC#5", "AC#6"]
---

## Summary
The deletion itself. Remove the last production consumers of `migrations.py` (the `get_db_connection` migration step and the redundant `_ensure_schema` hook), delete `migrations.py` entirely, and update the package docstring. After T03 no test references migrations, so this lands with the suite green. Add the tests that flip to passing once the migration step is gone: no `token_snapshots` on a fresh conversation DB (AC#3), and a safe open of an existing v6 DB with rows intact (AC#4).

## Target Files
- delete: `src/ccrecall/migrations.py`
- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/hooks/memory_setup.py`
- modify: `src/ccrecall/__init__.py`
- modify: `tests/test_db.py`
- read: `src/ccrecall/schema.py`
- read: `src/ccrecall/token_schema.py`
- read: `design/specs/003-migrations-squash-v6-baseline/design.md`

## Prompt
1. **`src/ccrecall/db.py` (`get_db_connection`, lines ~241-284):**
   - Remove `from ccrecall.migrations import migrate_columns, migrate_db` (line 16).
   - Remove the entire `migrated = migrate_db(conn)` block and its reconnect branch (lines ~256-261) and the `if not migrated:` guard (line ~263) — replace the whole construct with an **unconditional** schema apply: detect FTS, `executescript(SCHEMA_CORE)`, then the matching `SCHEMA_FTS5`/`SCHEMA_FTS4` variant, then `conn.commit()`. (The branch disappears entirely; it does not merely collapse.)
   - Remove the `migrate_columns(conn)` call (line ~274) and its comment.
   - Leave the `load_vec` / `_ensure_vec_schema` block (lines ~276-282) unchanged.

2. **Delete `src/ccrecall/migrations.py`** entirely (`migrate_db`, `migrate_columns`, `_backup_db_before_migration`, `_reaggregate_notification_branches`, `_backfill_origin`, `_migrate_project_paths`, `_migrate_v5`, `_migrate_v6` are all now dead).

3. **`src/ccrecall/hooks/memory_setup.py`:**
   - Delete the `_ensure_schema` function (lines ~75-79) and its call site (line 144, inside the `else:` existing-DB branch). The immediately-following `_needs_reimport(settings)` opens its own `get_db_connection`, which applies the schema — so removing `_ensure_schema` preserves behavior. Remove any now-unused imports it pulled in (e.g. `get_db_connection` if `_ensure_schema` was its only user — but `_needs_reimport`/`_needs_backfill` also use it, so likely keep it; verify).
   - Fix the stale comment on `_needs_reimport` (line ~84) that says NULL `file_hash` is "set by v3 migration" — it is written by the normal sync path (`session_ops.py` sync path uses `file_hash=None`). Reword to reflect that; do NOT change the function's logic.

4. **`src/ccrecall/__init__.py`:** update the module-overview docstring line (~line 7) that describes `migrations` / `migrate_db` / `migrate_columns` — remove it or rephrase since the module is gone.

5. **`tests/test_db.py` — add the tests that now pass:**
   - **AC#3:** a fresh conversation DB (via `get_db_connection` against a temp path) has **no** `token_snapshots` table (`SELECT name FROM sqlite_master WHERE type='table' AND name='token_snapshots'` returns nothing). Also confirm (or rely on existing token-DB tests) that `token_schema.ensure_schema` still creates `token_snapshots` on a token DB — a one-line assert against a `token_db` fixture / `ensure_schema` call suffices for FR#4.
   - **AC#4:** build a DB that looks like an existing v6 DB — apply `SCHEMA`, insert sample rows into `projects`/`sessions`/`branches`/`messages` and create+populate a `token_snapshots` table (simulating an inert legacy table), set `PRAGMA user_version = 6`, close. Then open it via `get_db_connection` and assert it returns successfully and the sample rows in `branches`/`messages`/`token_snapshots` are intact (counts unchanged, no table dropped).

Run the full suite (`uv run pytest -q`) — it must pass. Confirm `grep -rn "ccrecall.migrations\|migrate_columns\|migrate_db" src/ tests/` returns nothing (AC#5).

See `## Architecture` steps 2-5, `## Migration`, and `## Edge Cases` (existing-v6-DB open) in the design doc.

## Focus
- `get_db_connection` is `src/ccrecall/db.py:241`. The schema-apply block it already contains (lines 263-271, inside `if not migrated:`) is exactly what the unconditional version should be — you are hoisting that block out of the guard and deleting the `migrate_db`/reconnect/`migrate_columns` scaffolding around it. `SCHEMA_CORE`, `SCHEMA_FTS5`, `SCHEMA_FTS4`, `detect_fts_support` are already imported in `db.py`.
- The T01 pin (schema-equivalence) and T02 AC#2 test are the safety net: after this deletion, a fresh `get_db_connection` DB applies `SCHEMA` only — the pin proves its schema still matches the pre-squash snapshot (minus `token_snapshots`). If the pin goes red, the DDL lift in T02 was incomplete — fix `schema.py`, do not weaken the pin.
- `memory_setup.py`: `_ensure_schema` is at lines ~75-79; the setup sequence is `main()` lines ~131-158. The call is in the `else` branch (existing DB). `_needs_reimport` (lines ~82-93) and `_needs_backfill` (lines ~96+) both call `get_db_connection`, so the import stays. Verify before removing any import.
- `token_schema.ensure_schema` (`src/ccrecall/token_schema.py:113`) is the sole owner of `token_snapshots` after this — do not edit it. It is already schema-complete (adds `data_source`/`cache_*` via `ALTER TABLE`, lines 149-156).
- Existing-DB safety (AC#4): the only DDL `get_db_connection` now runs is `CREATE TABLE/INDEX IF NOT EXISTS` — no-ops on populated tables. Confirm nothing drops `token_snapshots` on an existing DB.
- After deletion, do a final grep across `src/` and `tests/` for `migrations`, `migrate_db`, `migrate_columns`, `user_version` (the only remaining `user_version` references should be gone from `src/`; any in tests were removed in T03).

## Verify
- [ ] FR#1: `get_db_connection` initializes a fresh DB by applying `SCHEMA_CORE` + detected FTS with no migration call (no `migrate_db`/`migrate_columns` in `db.py`).
- [ ] FR#3: A fresh conversation DB has no `token_snapshots` table.
- [ ] FR#4: `token_schema.ensure_schema` still creates `token_snapshots` on the token DB (token-DB tests pass; `token_schema.py` unchanged).
- [ ] FR#5: No `PRAGMA user_version` read or write remains in `src/` (grep clean).
- [ ] FR#6: Opening an existing v6 DB via `get_db_connection` succeeds and leaves its rows/columns intact (AC#4 test passes).
- [ ] AC#3: A `test_db.py` test asserts a fresh conversation DB has no `token_snapshots`, and `ensure_schema` still creates it on a token DB.
- [ ] AC#4: A `test_db.py` test opens a populated v6-style DB and confirms rows in `branches`/`messages`/`token_snapshots` are intact.
- [ ] AC#5: `migrations.py` no longer exists and `grep -rn "ccrecall.migrations" src/ tests/` returns nothing.
- [ ] AC#6: The full suite passes (`uv run pytest -q`).
