---
task_id: "T06"
title: "Migrate test callers to get_connection"
status: "planned"
depends_on: ["T05"]
implements: ["FR#16"]
---

## Summary

Update all test files that reference `get_db_connection` by name — imports, direct calls, mock patches, and docstrings. After this task, `grep -rn 'get_db_connection' src/ tests/` returns zero hits across the entire codebase.

## Target Files

- modify: `tests/test_db.py`
- modify: `tests/test_import_pipeline.py`
- modify: `tests/test_backfill_embeddings.py`
- modify: `tests/test_sync_hook.py`
- modify: `tests/test_recent_chats.py`
- read: `design/specs/002-cleanup-plan/design.md` (§ Architecture → Connection management)

## Prompt

Update every test file that references `get_db_connection`:

### test_db.py

Update all `get_db_connection` references: the import, function calls, and any class/method names that reference it (e.g., `TestGetDbConnection` → `TestGetConnection`, method names containing `get_db_connection`). Read the full import block and search for all occurrences.

### test_import_pipeline.py

Lines ~9, ~783, ~799: Update the import (`from ccrecall.db import get_db_connection` → `from ccrecall.db import get_connection`) and all call sites. The calls now need to use the context manager: `with get_connection(...) as conn:`.

### test_backfill_embeddings.py

This file has ~12 mock patches referencing `"ccrecall.hooks.backfill_embeddings.get_db_connection"`. Update ALL to `"ccrecall.hooks.backfill_embeddings.get_connection"`.

The mock return values need to work with the context manager protocol. Since `get_connection` is a `@contextlib.contextmanager`, patches should return a context manager that yields the mock connection. Pattern:

```python
from contextlib import contextmanager

@contextmanager
def _mock_get_conn(conn):
    yield conn

# In the patch:
patch("ccrecall.hooks.backfill_embeddings.get_connection", lambda *a, **k: _mock_get_conn(mock_conn))
```

Or use `MagicMock` with `__enter__`/`__exit__` configured.

### test_sync_hook.py

Line ~895: Update the monkeypatch from `get_db_connection` to `get_connection`. Ensure the mock works with the context manager protocol.

### test_recent_chats.py

Line 3: The module docstring mentions `get_db_connection` — update to `get_connection`.

### Final verification

After all updates: `grep -rn 'get_db_connection' src/ tests/` must return zero hits.

Run `uv run pytest` and `uvx prek run --all-files`.

## Focus

- The `test_backfill_embeddings.py` mock migration is the trickiest part. The current pattern `patch(..., return_value=mock_conn)` won't work because `get_connection` is a context manager, not a regular function. Each patch needs to return something compatible with `with ... as conn:`.
- Some tests use `_NoCloseConn` wrapper — check whether this pattern needs updating for the context manager.
- `test_db.py` may have class names like `TestGetDbConnection` that should be renamed for consistency.

## Verify

- [ ] FR#16: `grep -rn 'get_db_connection' src/ tests/` returns zero hits across the entire codebase
- [ ] FR#16: `uv run pytest` passes with zero failures
- [ ] FR#16: `uvx prek run --all-files` passes
