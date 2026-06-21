# Design: Migrations Squash to v6 Baseline (Follow-up to Issue #20)

**Date:** 2026-06-21
**Status:** approved
**Scope-mode:** reduce

> **Origin:** Split out of spec 002 (`sync_session` split, PR for part 2a of #20). During that investigation the conversation-DB migration machinery was found effectively dead for the maintainer's solo workflow. Rather than refactor it for readability, the decision was to **subtract it** — squash to a v6 baseline. This spec is deletion-only with a schema-equivalence guarantee.

## Problem

The conversation database carries three layers of migration machinery that no longer run on any of the maintainer's machines:

1. **The versioned DML ladder (`migrate_columns`, v1→v6).** Each `if version < N` block only transforms an *in-place populated* DB whose `PRAGMA user_version` is below 6. Every machine is either already at v6 (this machine) or rebuilt fresh when the semantic model changes (other machines). On a fresh/empty DB every `UPDATE` hits zero rows and is a no-op before `user_version` is bumped to 6. The ladder never does work.
2. **`migrate_db` (pre-v3 nuke-and-recreate).** Fires only when a DB has no `branches` table — i.e. a pre-v3 schema. No pre-v3 DB exists on any machine.
3. **A duplicate `token_snapshots` table on the conversation DB.** `migrate_columns` creates it, but nothing ever reads it there. The live table lives on the separate token-analytics DB (`token_schema.ensure_schema` creates it, `token_analytics.backfill_token_snapshots` writes it, `token_dashboard` reads it).

This is ~575 lines in `migrations.py` plus ~30 migration-specific tests in `test_db.py`, all dead. It is reader-load with no payoff: every future schema change forces a maintainer to reason about a versioned migration path that will never execute. Subtract it.

The squash has one hazard: `migrate_columns` also performs **load-bearing DDL** that a fresh DB genuinely needs — three `branches` embedding columns and one index that are *not* in `SCHEMA_CORE`. Deleting `migrate_columns` without lifting those into `SCHEMA_CORE` first would silently drop columns the embed-on-write path depends on.

## Goals

- `src/ccrecall/migrations.py` is deleted; `get_db_connection` opens the DB and applies the schema with no migration step.
- A freshly created conversation DB has a schema **identical** (tables, columns, column order, indexes) to today's fresh DB — minus the dead `token_snapshots` table.
- `token_schema.py` is the sole owner of `token_snapshots`.
- The full test suite passes after migration-specific tests are removed and DB-building fixtures drop their `migrate_columns` call.

## Non-Goals

- **No change to the token-analytics DB or `token_schema.py`'s `token_snapshots` definition.** It is already the live owner; this spec only stops the *conversation* DB from creating a duplicate. (Note for AC#3's test author: `token_schema.ensure_schema` is already schema-complete — it adds `data_source` (and `cache_*` columns) to `token_snapshots` via `ALTER TABLE` at `token_schema.py:149-156`, not in its inline `CREATE TABLE`. Don't read a schema gap into the inline DDL.)
- **No fix to `_needs_reimport`'s pre-existing behavior.** Its NULL-`file_hash` check counts rows written by the normal sync path (not just the old v3 migration), so its comment is already misleading. Correcting the comment is in scope; changing the function's behavior is not (no smuggled behavior changes).
- No change to the FTS schema, the vec schema, or any read/search path.
- No data migration for existing DBs — they are already at v6 and keep working untouched. Their stored `user_version` is left as-is (harmless once nothing reads it).
- The remaining #20 read/render-path splits (`summarizer.py`, `hooks/memory_context.py`, `hooks/backfill_embeddings.py`).

## User Scenarios

### Maintainer (Jessica): sole developer on ccrecall
- **Goal:** change the conversation schema (add a column, an index) without reasoning about a versioned migration ladder that never runs.
- **Context:** editing the schema, or onboarding to `db.py` and tracing what happens on connection open.

#### Add a schema element after the squash
1. **Open `schema.py`.**
   - Sees: `SCHEMA_CORE` containing the complete table/column/index set — the single source of truth.
   - Decides: add the column to the `CREATE TABLE`.
   - Then: a fresh DB gets it; there is no `migrate_columns` to also update, no `user_version` to bump.

#### Open a DB on a fresh machine
1. **First `get_db_connection` call.**
   - Sees (internally): `SCHEMA_CORE` + FTS applied idempotently; no migration branch.
   - Then: the DB has every column the write/embed path needs, identical to before.

## Functional Requirements

- **FR#1** `get_db_connection` initializes a fresh conversation DB by applying `SCHEMA_CORE` plus the detected FTS variant, with no migration call.
- **FR#2** A fresh conversation DB's `branches` table contains `embedding_version`, `embedding_model`, and `summary_version_at_embed` as its final three columns (in that order), and the index `idx_branches_embedding_version` exists.
- **FR#3** A fresh conversation DB does **not** create a `token_snapshots` table.
- **FR#4** `token_schema.ensure_schema` continues to create and maintain `token_snapshots` on the token-analytics DB, unchanged.
- **FR#5** The conversation DB write path performs no read or write of `PRAGMA user_version`.
- **FR#6** Opening an existing v6 conversation DB succeeds and leaves its existing rows and columns unmodified.

## Edge Cases

- **Existing DB with `user_version = 6` and a populated `token_snapshots`.** Opening it after the squash must not error and must not drop the existing `token_snapshots` table or its data (CREATE/ALTER are gone, so nothing touches it; it simply becomes inert). Verified by FR#6.
- **Column-order drift.** ALTER TABLE appends columns at the end. The three embedding columns are appended *after* `summary_version` today; placing them in the same trailing position in the `CREATE TABLE` preserves positional access and `SELECT *` ordering. A schema-snapshot pin guards this.
- **FTS-disabled SQLite build.** `detect_fts_support` may return `None`; the connection path must still apply `SCHEMA_CORE` and succeed (unchanged from today).
- **A genuinely pre-v3 DB is opened post-squash (does not exist on any machine, but for correctness).** `SCHEMA_CORE`'s `CREATE TABLE IF NOT EXISTS` will not recreate or repair the old tables; the DB would be inconsistent. This is the accepted, explicitly-chosen consequence of removing `migrate_db` (confirmed: no such DB exists). Documented, not guarded.

## Acceptance Criteria

- **AC#1** A schema-snapshot pin captures, from a fresh DB built the *current* way (`SCHEMA` + `migrate_columns`), the full set of tables, per-table column names+order+types (`PRAGMA table_info`), and indexes — excluding `token_snapshots`. After the squash, a fresh DB (schema only) reproduces that snapshot exactly. (FR#1, FR#2, FR#3)
- **AC#2** A test asserts a fresh conversation DB has the three `branches` embedding columns as the last three rows of `PRAGMA table_info(branches)` (the `UNIQUE(...)` clause is a constraint, not a column, so it does not appear as a `table_info` row) and the `idx_branches_embedding_version` index exists. (FR#2)
- **AC#3** A test asserts a fresh conversation DB has no `token_snapshots` table, and the existing token-DB tests confirm `ensure_schema` still creates it on the token DB. (FR#3, FR#4)
- **AC#4** A test opens a DB pre-populated to look like an existing v6 DB (with rows in `branches`/`messages`/`token_snapshots`) and confirms `get_db_connection` returns successfully with those rows intact. (FR#6)
- **AC#5** `migrations.py` no longer exists and no module imports from it. (FR#1, FR#5)
- **AC#6** The full test suite passes with zero failures. (all)

## Key Constraints

- **Lift-before-delete ordering.** The embedding DDL and `idx_branches_embedding_version` must be present in `SCHEMA_CORE` *before* `migrate_columns` is deleted — sequence the schema lift first so the equivalence pin stays green at every step.
- **Preserve trailing column order.** The three embedding columns go after `summary_version` in the `branches` `CREATE TABLE`, before the `UNIQUE(...)` clause — matching the ALTER-append order. Do not reorder existing columns.
- **No smuggled behavior changes.** `_needs_reimport` behavior is preserved exactly; only its stale comment is corrected. If the squash surfaces a latent bug, note and file it — do not fix it here.
- **Token DB is untouched.** No edits to `token_schema.py`'s schema SQL or `token_analytics.py`. The conversation and token analytics databases are separate files with separate owners.
- **Existing-DB safety.** Removing `migrate_db` and the DML ladder must not delete, recreate, or rewrite any table on an existing DB. The only DDL that runs is `CREATE TABLE/INDEX IF NOT EXISTS` (no-ops on a populated DB).

## Dependencies and Assumptions

- No external systems. In-process SQLite.
- Assumes (verified during reconnaissance) that `token_snapshots` is never read from the conversation DB — the only writer/readers are on the token-analytics DB via `connect_token_db`.
- Assumes (verified) the only `migrate_columns` DDL absent from `SCHEMA_CORE` is the three embedding columns + `idx_branches_embedding_version`; all message columns and other branch columns are already present. Note: `idx_branches_summary_version` is created in *both* `migrate_columns` (line 164) and `SCHEMA_CORE` (line 56) — it is already covered and requires **no** lift. Do not re-add it.
- Assumes the existing DB-building test fixtures (`memory_db`, `make_vec_conn` in `conftest.py`) plus the schema snapshot are sufficient to prove equivalence.

## Architecture

The change is subtraction in a deliberate sequence so the equivalence pin never goes red:

1. **Lift load-bearing DDL into `SCHEMA_CORE` (`schema.py`).** Add `embedding_version INTEGER DEFAULT 0`, `embedding_model TEXT`, `summary_version_at_embed INTEGER` to the `branches` `CREATE TABLE`, immediately after `summary_version` and before `UNIQUE(session_id, leaf_uuid)`. Add `CREATE INDEX IF NOT EXISTS idx_branches_embedding_version ON branches(embedding_version);` alongside the other branch indexes. After this step, `SCHEMA` (= `SCHEMA_CORE + SCHEMA_FTS5`) is the complete schema, and `executescript(SCHEMA)` alone produces a DB equivalent to `SCHEMA + migrate_columns` (minus `token_snapshots`).

2. **Simplify `get_db_connection` (`db.py`).** Remove the `migrate_db` call and its reconnect branch; the entire `migrated = migrate_db(...)` / `if migrated: reconnect` / `if not migrated: apply schema` construct (db.py ~257-271) is **replaced** by an unconditional `SCHEMA_CORE`/FTS apply (the branch disappears, not merely collapses). Remove the `migrate_columns(conn)` call. Remove `from ccrecall.migrations import migrate_columns, migrate_db`.

3. **Delete `src/ccrecall/migrations.py`.** Every function in it is now dead: `migrate_db`, `migrate_columns`, `_backup_db_before_migration`, `_reaggregate_notification_branches`, `_backfill_origin`, `_migrate_project_paths`, `_migrate_v5`, `_migrate_v6`.

4. **Remove the redundant `_ensure_schema` (`hooks/memory_setup.py`).** Its sole purpose was triggering `migrate_columns` to create `token_snapshots`. It is called only in the **existing-DB** branch (`memory_setup.py:144`, inside `else:` — the fresh-DB branch spawns `ccrecall import` and never calls it, so fresh-DB startup is unaffected by its removal). In the existing-DB path, the immediately-following `_needs_reimport(settings)` opens its own `get_db_connection`, which applies the schema regardless — so removing `_ensure_schema` preserves behavior (schema still applied before the `import_log` query). Delete the function and its call at line 144. Fix the now-stale "set by v3 migration" comment on `_needs_reimport` (NULL `file_hash` is written by the normal sync path, not the deleted migration).

5. **Update `__init__.py`** module-overview line that names `migrations` / `migrate_db` / `migrate_columns`.

The cross-cutting file is `schema.py` (step 1) — it must land first and stay green; every later step depends on `SCHEMA` being complete.

## Replacement Targets

- **`src/ccrecall/migrations.py` (entire module)** → replaced by the now-complete `SCHEMA_CORE` in `schema.py` for DDL; the DML ladder and pre-v3 nuke have no replacement (dead). Remove the file.
- **`token_snapshots` block in `migrate_columns`** → replaced by `token_schema.ensure_schema` as the sole owner (already exists; no new code). The conversation-side copy is removed, not migrated.
- **`_ensure_schema` in `hooks/memory_setup.py`** → replaced by the schema application already performed inside `get_db_connection` on the next call. Remove the function.
- **`migrate_columns` calls in test fixtures/tests** → replaced by `executescript(SCHEMA)` alone (now complete). Remove the calls and the `from ccrecall.migrations import ...` lines.

## Migration

Existing conversation DBs are already at `user_version = 6` with all columns and (harmlessly) a populated `token_snapshots`. The squash performs **no** data migration:
- No table is dropped, recreated, or rewritten. Only `CREATE TABLE/INDEX IF NOT EXISTS` runs, which no-ops on a populated DB.
- The inert `token_snapshots` table on existing conversation DBs is left in place (dropping it would be a destructive migration this spec explicitly avoids). It simply stops being created on *new* DBs.
- Stored `user_version` is left untouched; nothing reads it after the squash.

This is irreversible only in the sense that the migration code is deleted from history's tip — but `migrate_db`/`migrate_columns` remain recoverable from git history if a versioned migration is ever needed again (at which point a fresh, minimal versioning scheme would be reintroduced).

## Convention Examples

### Schema as a single source of truth — `CREATE TABLE IF NOT EXISTS` with inline indexes
**Source:** `src/ccrecall/schema.py` (`SCHEMA_CORE`, branches table)
```sql
CREATE TABLE IF NOT EXISTS branches (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  ...
  summary_version INTEGER DEFAULT 0,
  -- lifted from migrate_columns, kept in ALTER-append order (trailing):
  embedding_version INTEGER DEFAULT 0,
  embedding_model TEXT,
  summary_version_at_embed INTEGER,
  UNIQUE(session_id, leaf_uuid)
);
CREATE INDEX IF NOT EXISTS idx_branches_embedding_version ON branches(embedding_version);
```

### The token-DB schema owner this squash defers to (do NOT duplicate on the conversation DB)
**Source:** `src/ccrecall/token_schema.py` (`ensure_schema`)
```python
def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    # token_snapshots lives here — sole owner after the squash
    conn.executescript("""CREATE TABLE IF NOT EXISTS token_snapshots ( ... );""")
```

### Test DB-build fixture — the migrate_columns call that gets removed
**Source:** `tests/conftest.py` (`memory_db`) — DO/DON'T for post-squash fixtures
```python
# DON'T (post-squash): migrate_columns no longer exists
conn.executescript(SCHEMA); conn.commit(); migrate_columns(conn)
# DO: SCHEMA is complete on its own
conn.executescript(SCHEMA); conn.commit()
```

## Alternatives Considered

- **Keep `migrate_db` as a pre-v3 safety net, squash only the DML.** Rejected — the user explicitly confirmed removal; no pre-v3 DB exists on any machine, so the ~60 lines are pure dead guard (reader-load with no payoff).
- **Move `token_snapshots` DDL into `SCHEMA_CORE` (keep it on the conversation DB).** Rejected — it is never read from the conversation DB; keeping it anywhere on that DB perpetuates a dead duplicate. The token-analytics DB is its real and only home.
- **Refactor/split `migrations.py` for readability (the original spec-002 plan).** Rejected — refactoring dead code is wasted effort; subtract-first says delete it.
- **Do nothing.** Rejected — every future schema change pays the cost of reasoning about a migration ladder that never executes.

## Test Strategy

### Existing Tests to Adapt
- `tests/conftest.py` (`memory_db`, `make_vec_conn`) — drop the `migrate_columns(conn)` call and the `from ccrecall.migrations import migrate_columns` import; `SCHEMA` alone now suffices.
- `tests/test_summarizer.py`, `tests/test_integration.py`, `tests/test_search.py`, `tests/test_sync_hook.py`, `tests/test_session_ops.py` — each builds DBs via `executescript(SCHEMA)` + `migrate_columns`; remove the `migrate_columns` calls and the migrations import. Behavior must be unchanged (these don't test migrations, they use them only to build a schema).
- `tests/test_recent_chats.py` — update the fixture docstring that mentions `migrate_columns` if it still calls it.

### New Test Coverage
- **Schema-equivalence snapshot pin** (AC#1) — capture the full schema (tables, `table_info` per table, indexes, excluding `token_snapshots`) from a current-style fresh DB; assert a schema-only fresh DB matches. Committed first, green on current code, kept green through the squash.
- **Embedding-DDL-in-SCHEMA assertion** (AC#2) — fresh DB has the three embedding columns last on `branches` + `idx_branches_embedding_version`. (Replaces the deleted `test_new_columns_exist_via_migrate_columns` / `idx_branches_embedding_version` tests with the SCHEMA-sourced equivalent.)
- **No conversation `token_snapshots`** (AC#3) and **existing-v6-DB open is safe** (AC#4).

### Tests to Remove
- `tests/test_db.py` migration-behavior suites: `TestMigrateColumns` (the migrate_columns-specific assertions), `TestMigrateDb`, the `_versioned_db` helper and the versioned-DML classes (v1→v2, v4, v5, v6 — `test_v4_bumps_user_version`, the `_migrate_project_paths` source-inspection test, etc.), and the backup-blocks-nuke test. These pin behavior of code being deleted. Any assertion that remains *valid* as a fresh-schema property (embedding columns exist, indexes exist) is re-expressed against `SCHEMA` per New Test Coverage rather than deleted outright.

## Documentation Updates

- `src/ccrecall/__init__.py` — update the module-overview docstring line that describes `migrations` (line ~7).
- `CHANGELOG` — add an entry referencing #20: "squash conversation-DB migrations to v6 baseline; SCHEMA is now the single source of truth."
- No README/CLI-help references to `migrations`, `migrate_db`, or `migrate_columns` exist — confirmed during reconnaissance.

## Impact

### Changed Files
- `src/ccrecall/schema.py` — modify: lift 3 embedding columns + `idx_branches_embedding_version` into `SCHEMA_CORE` (cross-cutting; lands first).
- `src/ccrecall/db.py` — modify: remove `migrate_db`/`migrate_columns` calls + import; collapse the migration branch in `get_db_connection`.
- `src/ccrecall/migrations.py` — delete: entire module.
- `src/ccrecall/hooks/memory_setup.py` — modify: remove `_ensure_schema` + its call; fix stale `_needs_reimport` comment.
- `src/ccrecall/__init__.py` — modify: update module-overview docstring.
- `tests/conftest.py` — modify: drop `migrate_columns` from `memory_db` and `make_vec_conn`.
- `tests/test_db.py` — modify: delete migration-behavior suites; re-express still-valid schema assertions against `SCHEMA`; add the schema-equivalence snapshot pin, no-token_snapshots, and existing-v6-open tests.
- `tests/test_summarizer.py`, `tests/test_integration.py`, `tests/test_search.py`, `tests/test_sync_hook.py`, `tests/test_session_ops.py`, `tests/test_recent_chats.py` — modify: remove `migrate_columns` calls + migrations imports.

### Behavioral Invariants
- A fresh conversation DB's schema is identical to today's (tables, columns, column order, indexes) except for the absence of `token_snapshots`.
- The embed-on-write path's columns (`embedding_version`, `embedding_model`, `summary_version_at_embed`) and `idx_branches_embedding_version` exist on every fresh DB.
- Opening an existing v6 DB never drops or rewrites a table.
- `token_schema.ensure_schema` and the token-analytics DB are unchanged.
- `_needs_reimport` returns the same value as before for any DB state.

### Blast Radius
- `get_db_connection` is the single entry point for every conversation-DB consumer (sync, import, search, recall, recent-chats, backfill). All are schema-equivalent post-squash, so none are affected.
- `migrations.py` is imported only by `db.py` and tests — both updated in this spec.
- No cross-package or external consumers; the token-analytics subsystem is untouched.

## Open Questions

None.
