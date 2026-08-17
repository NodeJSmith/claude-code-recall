---
task_id: "T01"
title: "Split db.py: extract vec-dependent code into db_vec.py"
status: "planned"
depends_on: []
implements: ["FR#1", "FR#2", "FR#3", "FR#7"]
---

## Target Files

- create: `src/ccrecall/db_vec.py`
- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/branch_ops.py`
- modify: `src/ccrecall/session_ops.py`
- modify: `src/ccrecall/embed_ops.py`
- modify: `src/ccrecall/status.py`
- modify: `src/ccrecall/search_conversations.py`
- modify: `src/ccrecall/search_cli.py`
- modify: `src/ccrecall/recent_chats.py`
- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- modify: `src/ccrecall/hooks/backfill_tool_content.py`
- modify: `src/ccrecall/hooks/backfill_status.py`
- modify: `src/ccrecall/hooks/sync_current.py`
- modify: `src/ccrecall/hooks/import_conversations.py`
- modify: `tests/conftest.py`
- modify: `tests/test_db.py`
- modify: `tests/test_session_ops.py`
- modify: `tests/test_search.py`

## Prompt

Split `src/ccrecall/db.py` to decouple `get_connection()` from the numpy/fastembed import chain. This is issue #137.

### What moves to `db_vec.py`

Create `src/ccrecall/db_vec.py` with these functions/constants moved from `db.py`:
- `vec_available(conn)` — loads sqlite-vec extension
- `chunk_vec_queryable(conn)` — probes chunk_vec virtual table
- `upsert_chunk_vec(cursor, chunk_id, embedding)` — vec0 DELETE+INSERT
- `write_chunk_embedding(cursor, chunk_id, embedding, embedding_version, embedding_model)` — vector + bookkeeping write
- `fetch_branch_messages(cursor, branch_id, include_notifications)` — branch message query
- `branch_embedding_coverage(conn)` — watermark coverage report
- `_ensure_vec_schema(conn)` — vec DDL (chunk_vec, triggers, self-heal)
- `TRIGGER_CHUNKS_VEC_AD` constant
- `VEC_BUSY_TIMEOUT_MS` constant

`db_vec.py` imports `sqlite_vec`, `from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION`, and `from ccrecall.db import CHUNK_EMBEDDABLE_BRANCH_FILTER, CONTENT_ERROR_VERSION` (the filter constants stay in db.py since they're plain strings with no heavy deps).

Add a module-level `log = logging.getLogger(LOGGER_NAME)` since `vec_available` and `chunk_vec_queryable` both log.

Also create a public function in `db_vec.py`:
```python
def ensure_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec and create vec schema. Returns True if vec is available."""
    if vec_available(conn):
        _ensure_vec_schema(conn)
        conn.commit()
        conn.execute(f"PRAGMA busy_timeout = {VEC_BUSY_TIMEOUT_MS}")
        return True
    return False
```

### What stays in `db.py`

- `get_connection`, `_open_connection` — but remove the `from ccrecall.embeddings import ...` line
- `apply_base_pragmas` (delegates to db_base)
- `escape_like`, `parse_project_filter`, `resolve_db_settings`
- `EMBEDDABLE_BRANCH_FILTER`, `CHUNK_EMBEDDABLE_BRANCH_FILTER`, `CONTENT_ERROR_VERSION`
- `SCHEMA_VERSION` re-export from db_base
- `DEFAULT_PROJECTS_DIR` re-export from config
- All migration re-exports (`_migrate_to_v1` through `_migrate_to_v7`, `_apply_migrations`)

### Modify `_open_connection` in db.py

Replace the inline vec loading with a conditional import:
```python
def _open_connection(settings: dict | None = None, load_vec: bool = False) -> sqlite3.Connection:
    conn = db_base.open_connection(settings, apply_migrations_callback=_apply_migrations)
    if load_vec:
        from ccrecall.db_vec import ensure_vec
        ensure_vec(conn)
    return conn
```

This is the one accepted lazy import — it exists to preserve the hook-hot-path invariant (CLAUDE.md invariant #3). The `from ccrecall.embeddings` import is removed from module scope entirely.

### Fix `_apply_migrations` in db.py (CRITICAL)

`db.py`'s `_apply_migrations` currently passes `prepare=vec_available` to `db_base._apply_migrations`. After moving `vec_available` to `db_vec.py`, this would be a NameError on every fresh-DB connection (where `user_version < SCHEMA_VERSION`).

Fix: wrap in a deferred-import closure so numpy is only imported when `prepare` actually fires:

```python
def _apply_migrations(conn: sqlite3.Connection) -> None:
    def _prepare_vec(c: sqlite3.Connection) -> bool:
        from ccrecall.db_vec import vec_available
        return vec_available(c)
    db_base._apply_migrations(
        conn,
        prepare=_prepare_vec,
        migrate_to_v1=_migrate_to_v1,
        migrate_to_v2=_migrate_to_v2,
    )
```

On a fully migrated DB, `_prepare_vec` is never called (the `if current < SCHEMA_VERSION` branch is skipped), so numpy is never imported on the hot path. This preserves the hook-hot-path invariant.

Also remove `import sqlite_vec` from `db.py`'s module-scope imports — it moved to `db_vec.py`.

### Update callers

Update these files to import vec-specific symbols from `ccrecall.db_vec` instead of `ccrecall.db`:

1. `src/ccrecall/branch_ops.py`: `fetch_branch_messages` → `from ccrecall.db_vec import fetch_branch_messages`
2. `src/ccrecall/session_ops.py`: `chunk_vec_queryable` → `from ccrecall.db_vec import chunk_vec_queryable`
3. `src/ccrecall/embed_ops.py`: `write_chunk_embedding` → `from ccrecall.db_vec import write_chunk_embedding`
4. `src/ccrecall/status.py`: `branch_embedding_coverage, chunk_vec_queryable, vec_available` → `from ccrecall.db_vec import branch_embedding_coverage, chunk_vec_queryable, vec_available` (keep `get_connection` from `ccrecall.db`)
5. `src/ccrecall/search_conversations.py`: `branch_embedding_coverage, chunk_vec_queryable` → `from ccrecall.db_vec import ...`
6. `src/ccrecall/search_cli.py`: identify which imports are vec-specific and move them. Read the file to determine exactly which symbols come from db — likely `vec_available`, `chunk_vec_queryable`, `branch_embedding_coverage`. Keep non-vec imports from `ccrecall.db`.
7. `src/ccrecall/recent_chats.py`: identify vec-specific imports and move them. Read the file first.
8. `src/ccrecall/hooks/backfill_embeddings.py`: `chunk_vec_queryable, fetch_branch_messages` → `from ccrecall.db_vec import ...` (keep `get_connection, CONTENT_ERROR_VERSION` from `ccrecall.db`)
9. `src/ccrecall/hooks/backfill_status.py`: `chunk_vec_queryable` → `from ccrecall.db_vec import chunk_vec_queryable` (keep `CHUNK_EMBEDDABLE_BRANCH_FILTER, CONTENT_ERROR_VERSION` from `ccrecall.db`)
10. `src/ccrecall/hooks/sync_current.py`: `chunk_vec_queryable` → `from ccrecall.db_vec import chunk_vec_queryable` (keep `DEFAULT_PROJECTS_DIR, get_connection` from `ccrecall.db`)
11. `src/ccrecall/hooks/backfill_tool_content.py`: `VEC_BUSY_TIMEOUT_MS` → `from ccrecall.db_vec import VEC_BUSY_TIMEOUT_MS` (keep `get_connection` from `ccrecall.db`)
12. `src/ccrecall/hooks/import_conversations.py`: identify vec-specific imports and move them. Read the file first.

For each file: read it, find the exact `from ccrecall.db import ...` line, split vec symbols to `from ccrecall.db_vec import ...`, keep non-vec symbols on the existing `from ccrecall.db import ...` line.

### Update tests

Four test files import vec-specific symbols from `ccrecall.db` — all must be updated:

1. `tests/conftest.py:10` — `from ccrecall.db import _ensure_vec_schema` → `from ccrecall.db_vec import _ensure_vec_schema`
2. `tests/test_db.py` — read the file; update any vec-specific imports from `ccrecall.db` to `ccrecall.db_vec`
3. `tests/test_session_ops.py:14` — `from ccrecall.db import _ensure_vec_schema, vec_available` → `from ccrecall.db_vec import _ensure_vec_schema, vec_available`
4. `tests/test_search.py:12-16` — `from ccrecall.db import upsert_chunk_vec, vec_available, write_chunk_embedding` (and possibly others) → `from ccrecall.db_vec import ...`

Without fixing all four, `uv run pytest` fails at collection with ImportError.

### Critical constraint

Do NOT re-export vec functions from `db.py` — that would re-introduce the numpy import chain and defeat the purpose. Every caller must be updated.

## Verify

- [ ] FR#1: `python -c "from ccrecall.db import get_connection; import sys; assert 'numpy' not in sys.modules"` exits 0
- [ ] FR#2: `uv run pytest -q` — all tests pass, no import errors
- [ ] FR#3: `python -c "from ccrecall.db_vec import vec_available, chunk_vec_queryable, write_chunk_embedding"` exits 0
- [ ] FR#7: Test count unchanged (1178 passed)
- [ ] AC#7: `uvx prek run --all-files` passes clean
