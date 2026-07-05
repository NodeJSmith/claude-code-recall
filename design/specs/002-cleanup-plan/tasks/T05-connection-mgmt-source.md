---
task_id: "T05"
title: "Wrap get_connection as context manager and migrate source callers"
status: "planned"
depends_on: ["T04"]
implements: ["FR#16", "AC#11"]
---

## Summary

Rename `get_db_connection` to `get_connection` in `db.py` and wrap it as a `@contextlib.contextmanager` that commits on success, rolls back on exception, and always closes. Migrate all source callers in `src/` to use `with get_connection(...) as conn:`. This fixes the live connection leak in `sync_current.py`. Test callers are migrated in T06.

## Target Files

- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/search_conversations.py`
- modify: `src/ccrecall/recent_chats.py`
- modify: `src/ccrecall/hooks/memory_setup.py`
- modify: `src/ccrecall/hooks/memory_context.py`
- modify: `src/ccrecall/hooks/sync_current.py`
- modify: `src/ccrecall/hooks/import_conversations.py`
- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- modify: `src/ccrecall/hooks/backfill_summaries.py`
- modify: `tests/test_db.py`
- read: `design/specs/002-cleanup-plan/design.md` (§ Architecture → Connection management)

## Prompt

### Rename and wrap in db.py

Rename `get_db_connection` to `_open_connection` (private). Add a new public context manager:

```python
@contextlib.contextmanager
def get_connection(settings: dict | None = None, load_vec: bool = False):
    conn = _open_connection(settings, load_vec)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

### Migrate source callers

Every `conn = get_db_connection(...)` in `src/` becomes `with get_connection(...) as conn:`. Remove explicit `conn.commit()` and `conn.close()` from the `with` body.

**Call sites** (read each file to determine correct `with` block scope):

1. `hooks/sync_current.py` (line ~226): The leak site. Wrap in `with`.
2. `hooks/import_conversations.py` (lines ~184, ~249): Currently `contextlib.closing(get_db_connection(...))`. Replace with `with get_connection(...)`.
3. `hooks/memory_context.py` (line ~583): Replace.
4. `hooks/memory_setup.py` (lines ~102, ~115): Two call sites. Replace both.
5. `hooks/backfill_embeddings.py` (lines ~233, ~311): Replace.
6. `hooks/backfill_summaries.py` (line ~44): Replace.
7. `search_conversations.py` (lines ~719, ~753, ~845): Three call sites. Replace all.
8. `recent_chats.py` (line ~184): Replace.

**Important**: Some callers do work after `conn.commit()` but before `conn.close()` (e.g., spawning subprocesses). The `with` block should encompass only the DB work. Read each call site fully to determine the correct scope.

### New test — connection leak characterization

Add a test in `tests/test_db.py` proving the context manager closes on exception (AC#11):

```python
def test_connection_closed_on_exception(tmp_path):
    db_path = tmp_path / "test.db"
    with pytest.raises(ValueError):
        with get_connection({"db_path": str(db_path)}) as conn:
            conn.execute("SELECT 1")
            raise ValueError("simulate failure")
    with pytest.raises(ProgrammingError):
        conn.execute("SELECT 1")
```

Do NOT update test files that mock/reference `get_db_connection` — that is T06's scope. After changes, `grep -rn 'get_db_connection' src/` should return zero hits (test hits will remain until T06).

Run `uv run pytest` — some tests may fail due to stale mocks referencing `get_db_connection`. That's expected and fixed in T06.

## Focus

- `backfill_embeddings.py` has complex connection usage with retries and multiple connection opens. Read the full function before migrating.
- The `contextlib.closing(get_db_connection(...))` pattern in `import_conversations.py` handles close but not rollback. The new context manager is strictly better.
- Some callers call `conn.commit()` at intermediate points within their work. Decide whether those intermediate commits should stay (explicit partial commits within the `with` block are fine — the context manager's auto-commit at exit is additive).

## Verify

- [ ] FR#16: `grep -rn 'get_db_connection' src/` returns zero hits
- [ ] FR#16: `get_connection` is a `@contextlib.contextmanager` in `db.py`
- [ ] AC#11: Test proves connection is closed on both success and exception paths
- [ ] FR#16: `uv run pytest` passes with zero failures (or only test-mock failures fixed in T06)
