---
task_id: "T01"
title: "Add logging to search_vector.py, embeddings.py, and db.py"
status: "done"
depends_on: []
implements: ["FR#1", "FR#2", "FR#3", "FR#4"]
---

## Target Files

- modify: `src/ccrecall/search_vector.py`
- modify: `src/ccrecall/embeddings.py`
- modify: `src/ccrecall/db.py`

## Prompt

Add diagnostic logging to three modules that silently swallow errors. Read `design/specs/011-wave-1-logging-and-test-infra/tasks/context.md` for conventions.

### search_vector.py

Add at the top (after existing imports):

```python
import logging
from ccrecall.models import LOGGER_NAME
log = logging.getLogger(LOGGER_NAME)
```

In `execute_chunk_knn`, there are 5 `except sqlite3.Error: return []` blocks (at approximately lines 137, 147, 163, 179, 196). Add `log.exception(...)` before each `return []` with a message identifying which step failed:

1. Line ~137 (serialize): `log.exception("vec serialize failed")`
2. Line ~147 (KNN match): `log.exception("chunk KNN query failed")`
3. Line ~163 (filter): `log.exception("chunk KNN filter failed")`
4. Line ~179 (eligible count): `log.exception("eligible chunk count query failed")`
5. Line ~196 (total count): `log.exception("total vec candidate count query failed")`

### embeddings.py

Add at the top (after existing imports, before the constants):

```python
import logging
from ccrecall.models import LOGGER_NAME
log = logging.getLogger(LOGGER_NAME)
```

In `model_available()` (the `except Exception: return False` block around line 170): add `log.warning("embedding model failed to load", exc_info=True)` before `return False`.

In `is_model_cached_on_disk()` (the `except Exception: return False` block around line 153): add `log.debug("embedding cache check failed", exc_info=True)` before `return False`.

### db.py

Add logger declaration near the top (after imports). `db.py` already imports from various ccrecall modules; add:

```python
import logging
from ccrecall.models import LOGGER_NAME
log = logging.getLogger(LOGGER_NAME)
```

Check if `logging` and `LOGGER_NAME` are already imported — if so, just add the `log = ...` line.

In `vec_available()` (the `except Exception:` block around line 97): add `log.warning("sqlite-vec extension failed to load", exc_info=True)` before the `with contextlib.suppress` cleanup line.

### Verification

Run tests: `uv run pytest tests/test_search.py tests/test_db.py tests/test_embeddings.py -v` — all must pass.
Run lint: `uvx prek run --all-files` — must pass.

## Verify

- [ ] FR#1: `grep -c 'log.exception' src/ccrecall/search_vector.py` returns 5
- [ ] FR#2: `grep 'log.warning.*embedding model' src/ccrecall/embeddings.py` matches
- [ ] FR#3: `grep 'log.debug.*cache check' src/ccrecall/embeddings.py` matches
- [ ] FR#4: `grep 'log.warning.*sqlite-vec' src/ccrecall/db.py` matches
