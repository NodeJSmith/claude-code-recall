# Context: Migrations Squash to v6 Baseline

## Problem & Motivation
The conversation database (`get_db_connection` in `src/ccrecall/db.py`) runs three layers of migration machinery on every open that no longer execute on any of the maintainer's machines: the versioned DML ladder `migrate_columns` v1→v6 (only transforms in-place populated DBs below v6 — never happens here), `migrate_db`'s pre-v3 nuke-and-recreate (no pre-v3 DB exists), and a duplicate `token_snapshots` table that is created but never read (the live one lives on the separate token-analytics DB). This is ~575 dead lines in `migrations.py` plus ~30 dead tests. Every future schema change forces reasoning about a migration path that will never run. This spec subtracts it: squash to a v6 baseline where `SCHEMA_CORE` is the single source of truth. The one hazard is that `migrate_columns` also performs **load-bearing DDL** (three `branches` embedding columns + one index) absent from `SCHEMA_CORE` — those must be lifted in before deletion.

## Visual Artifacts
None.

## Key Decisions
1. **Subtract, don't refactor.** `migrations.py` is deleted entirely (all of `migrate_db`, `migrate_columns`, and every `_migrate_*`/backup helper). Confirmed dead for the solo workflow.
2. **`SCHEMA_CORE` becomes the single source of truth.** The only load-bearing DDL `migrate_columns` adds beyond `SCHEMA_CORE` is `branches.embedding_version`, `branches.embedding_model`, `branches.summary_version_at_embed`, and `idx_branches_embedding_version`. These lift into `SCHEMA_CORE`; everything else is already there. (`idx_branches_summary_version` is already in both — do NOT re-add it.)
3. **`token_schema.py` is the sole owner of `token_snapshots`.** The conversation-side copy is removed, not migrated. The token-analytics DB is a separate file with a separate owner and is untouched.
4. **Retire `user_version` on the conversation DB.** With the DML ladder gone, nothing reads or writes `PRAGMA user_version`. Existing DBs keep their stored value harmlessly.
5. **Remove `migrate_db` (pre-v3 nuke).** Accepted consequence: a (non-existent) pre-v3 DB would no longer auto-rebuild.
6. **Lift-before-delete, pinned by equivalence.** A schema-snapshot pin lands green on current code first; the DDL lift lands; then the deletion lands — the pin proves a fresh DB's schema is identical to today's minus the dead `token_snapshots`.
7. **Migrate callers (tests) then delete.** Test fixtures drop `migrate_columns` (safe once `SCHEMA` is complete) and migration-behavior tests are removed *before* the production deletion, so the suite is green at every boundary.

## Constraints & Anti-Patterns
- **Preserve trailing column order.** The three embedding columns go after `summary_version` and before `UNIQUE(session_id, leaf_uuid)` in the `branches` `CREATE TABLE` — matching the ALTER-append order so positional access / `SELECT *` is unchanged. Do NOT reorder existing columns. (The `UNIQUE(...)` constraint is not a column and does not appear as a `PRAGMA table_info` row.)
- **No smuggled behavior changes.** `_needs_reimport`'s behavior is preserved exactly; only its stale "set by v3 migration" comment is corrected (NULL `file_hash` is written by the normal sync path). If the squash surfaces a latent bug, note and file it — do not fix it here.
- **Existing-DB safety.** The only DDL that may run on an existing DB is `CREATE TABLE/INDEX IF NOT EXISTS` (no-ops on a populated DB). No table is dropped, recreated, or rewritten. The inert `token_snapshots` on existing conversation DBs is left in place (dropping it would be a destructive migration this spec avoids).
- **Token DB untouched.** No edits to `token_schema.py`'s SQL or `token_analytics.py`. `token_schema.ensure_schema` is already schema-complete — it adds `data_source`/`cache_*` to `token_snapshots` via `ALTER TABLE` (token_schema.py:149-156), not in its inline `CREATE TABLE`. Don't read a schema gap into the inline DDL.
- **Non-goals:** no FTS/vec/read-path change; no data migration for existing DBs; no fix to `_needs_reimport` behavior; no other #20 read/render-path splits.

## Design Doc References
- `## Architecture` — the 5-step subtraction sequence (lift DDL → simplify get_db_connection → delete migrations.py → remove _ensure_schema → update __init__).
- `## Migration` — why no data migration runs; existing DBs left untouched.
- `## Replacement Targets` — what each deleted thing is replaced by (mostly nothing; DDL by SCHEMA_CORE).
- `## Edge Cases` — column-order drift, FTS-disabled build, existing-v6-DB open, pre-v3 DB accepted consequence.
- `## Test Strategy` — schema-equivalence snapshot pin, tests to adapt/remove.
- `## Impact → Changed Files` / `### Behavioral Invariants` — the file inventory and what must not change.

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
