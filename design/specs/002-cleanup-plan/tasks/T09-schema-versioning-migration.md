---
task_id: "T09"
title: "Add schema versioning and run dead-branch migration"
status: "done"
depends_on: ["T06", "T07"]
implements: ["FR#7", "FR#14", "FR#15", "FR#17", "AC#5", "AC#10"]
---

## Summary

Add `PRAGMA user_version` schema versioning to the database connection path. Write the version-1 migration: delete all inactive branch rows (FK-safe order), drop the dead `messages_fts` virtual table, rebuild the `branches` table to change `UNIQUE(session_id, leaf_uuid)` to `UNIQUE(session_id)`, and re-create the `branches_fts` triggers after the rebuild. Keep `_ensure_vec_schema`'s self-heal checks outside the version gate.

## Target Files

- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/schema.py`
- modify: `tests/test_db.py`
- read: `design/specs/002-cleanup-plan/design.md` (§ Architecture → Schema versioning, § Migration)

## Prompt

### Schema versioning framework (FR#14, FR#15)

Add version checking to `_open_connection` in `src/ccrecall/db.py`. The check runs **after** `conn.executescript(SCHEMA_CORE)` and FTS setup — on a fresh install, tables must exist before the migration's DML runs.

```python
SCHEMA_VERSION = 1

def _apply_migrations(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if current < 1:
            _migrate_to_v1(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
```

Call `_apply_migrations(conn)` after schema creation and FTS setup, BEFORE `_ensure_vec_schema` (which stays outside the version gate per FR#15).

### Version 1 migration (FR#7, FR#17)

`_migrate_to_v1` runs the 4-step sequence. All within `BEGIN IMMEDIATE`:

**Step 1 — Delete dead branch rows (FR#7)** in FK-safe order:
```sql
DELETE FROM branch_messages WHERE branch_id IN (SELECT id FROM branches WHERE is_active = 0);
DELETE FROM chunks WHERE branch_id IN (SELECT id FROM branches WHERE is_active = 0);
DELETE FROM branches WHERE is_active = 0;
```

**Step 2 — Drop messages_fts (FR#17):**
```sql
DROP TABLE IF EXISTS messages_fts;
```

**Step 3 — Rebuild branches table** to change UNIQUE constraint:
```sql
CREATE TABLE branches_new (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    leaf_uuid TEXT NOT NULL,
    fork_point_uuid TEXT,
    context_summary TEXT,
    summary_version INTEGER NOT NULL DEFAULT 0,
    embedding_version INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(session_id)
);
INSERT INTO branches_new SELECT * FROM branches;
DROP TABLE branches;
ALTER TABLE branches_new RENAME TO branches;
```

**Critical:** Dropping `branches` auto-drops `branches_fts` triggers. Re-create them after rename. Read `schema.py` for the trigger DDL in `SCHEMA_FTS5`/`SCHEMA_FTS4`. Use `detect_fts_support()` to pick the right variant.

**Step 4**: `PRAGMA user_version = 1` (done by wrapper).

### Remove messages_fts from schema.py (FR#17)

Remove `messages_fts` virtual table definition and its three triggers (`messages_ai`, `messages_ad`, `messages_au`) from BOTH `SCHEMA_FTS5` and `SCHEMA_FTS4`. **Do NOT touch `branches_fts` or its triggers.**

### Tests

Add to `tests/test_db.py`:

1. **Fresh DB**: Open via `get_connection`, verify `PRAGMA user_version` = SCHEMA_VERSION.
2. **Migration from v0**: Seed DB with inactive branches + branch_messages + chunks, run `get_connection`, verify all `is_active = 0` rows deleted, `messages_fts` gone, `UNIQUE(session_id)` enforced, `branches_fts` still queryable.
3. **Re-entrant**: Run migration twice — second is no-op.
4. **FR#15**: Verify `_ensure_vec_schema` self-heal runs on every vec-loaded connection.

Run `uv run pytest` and `uvx prek run --all-files`.

## Focus

- T09 depends on T07 (branch identity code) — the code must produce single rows per session BEFORE the `UNIQUE(session_id)` constraint is applied. Without T07's changes, syncing a session with rewound content through `get_connection` would hit the new UNIQUE constraint and raise IntegrityError. Test fixtures bypass `get_connection` (they use raw `sqlite3.connect`), so CI wouldn't catch this.
- The branches table rebuild is the riskiest operation. After `DROP TABLE branches`, ALL triggers on it are gone — including `branches_fts` triggers. You MUST re-create them.
- The `BEGIN IMMEDIATE ... COMMIT/ROLLBACK` is a nested transaction within the connection's lifecycle. Make sure it doesn't conflict with the context manager's commit/rollback.
- On fresh install, DELETE statements are no-ops and branches rebuild applies the new constraint cleanly.

## Verify

- [ ] FR#14: `PRAGMA user_version` check runs in `_open_connection` after SCHEMA_CORE
- [ ] FR#15: `_ensure_vec_schema` runs on every vec-loaded connection, outside the version gate
- [ ] FR#7: After migration, no `is_active = 0` rows remain in `branches`
- [ ] AC#5: `SELECT COUNT(*) FROM branches WHERE is_active = 0` returns 0 after migration
- [ ] FR#17: `messages_fts` table does not exist after migration; definition removed from schema.py
- [ ] FR#17: `branches_fts` still exists and is queryable after migration
- [ ] AC#10: `PRAGMA user_version` returns SCHEMA_VERSION after connection
- [ ] FR#14: `uv run pytest` passes with zero failures
- [ ] FR#14: `uvx prek run --all-files` passes
