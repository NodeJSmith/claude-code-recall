---
task_id: "T04"
title: "Repoint mixed callers to config.py"
status: "done"
depends_on: ["T03"]
implements: ["AC#7"]
---

## Summary

Complete the `db.py` split by repointing all remaining callers that import symbols from `db.py` which now live in `config.py`. These "mixed callers" import from both modules after the split — they get two import lines. Verify that `embeddings.py` has no config-candidate imports. Verify neither `config.py` nor `db.py` exceeds 400 lines.

## Target Files

- modify: `src/ccrecall/hooks/memory_setup.py`
- modify: `src/ccrecall/hooks/memory_context.py`
- modify: `src/ccrecall/hooks/sync_current.py`
- modify: `src/ccrecall/hooks/import_conversations.py`
- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- modify: `src/ccrecall/hooks/backfill_summaries.py`
- modify: `src/ccrecall/search_conversations.py`
- modify: `src/ccrecall/recent_chats.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `tests/test_db.py`
- modify: `tests/test_health.py`
- read: `src/ccrecall/embeddings.py` (verify no config.py-candidate imports)
- read: `design/specs/002-cleanup-plan/design.md` (§ Architecture → db.py split)

## Prompt

For each file listed below, read its current `from ccrecall.db import (...)` line and split it into two imports: one `from ccrecall.config import (...)` for symbols that moved, one `from ccrecall.db import (...)` for symbols that stayed.

**Symbols that moved to config.py** (from T03): `RUNTIME_DIR`, `DEFAULT_DB_PATH`, `CONFIG_PATH`, `DEFAULT_LOG_PATH`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`, `DEFAULT_SETTINGS`, `SYNC_TEMP_PREFIX`, `CLEAR_HANDOFF_FILENAME`, `PID_FILE_MODE`, `load_config`, `load_settings`, `get_db_path`, `ensure_parent_dir`, `pid_file_path`, `remove_pid_file`, `log_hook_exception`, `setup_logging`, `atomic_write_json`.

**Symbols that stayed in db.py**: `get_db_connection`, `apply_base_pragmas`, `vec_available`, `chunk_vec_queryable`, `write_chunk_embedding`, `_ensure_vec_schema`, `branch_embedding_coverage`, `escape_like`, `fetch_branch_messages`, `parse_project_filter`, `resolve_db_settings`, `upsert_chunk_vec`, `DEFAULT_PROJECTS_DIR`, `CONTENT_ERROR_VERSION`, `EMBEDDABLE_BRANCH_FILTER`, `CHUNK_EMBEDDABLE_BRANCH_FILTER`, `VEC_BUSY_TIMEOUT_MS`.

### Files to update

For each file, read the current import block, determine which symbols are from config vs db, and split:

1. `hooks/memory_setup.py` — imports many symbols from db.py
2. `hooks/memory_context.py` — imports get_db_connection + config symbols
3. `hooks/sync_current.py` — imports get_db_connection + config symbols
4. `hooks/import_conversations.py` — imports get_db_connection + config symbols
5. `hooks/backfill_embeddings.py` — imports get_db_connection + config symbols
6. `hooks/backfill_summaries.py` — imports get_db_connection + config symbols
7. `search_conversations.py` — imports get_db_connection + config symbols
8. `recent_chats.py` — imports get_db_connection + config symbols
9. `cli/commands.py` — imports DEFAULT_DB_PATH (→ config) + DEFAULT_PROJECTS_DIR (stays in db)

### Test files

10. `tests/test_db.py` — check import block; update any references to config-bound symbols
11. `tests/test_health.py` — check if it mocks `ccrecall.db` for config-bound symbols; update mock paths

### Verification

Read `src/ccrecall/embeddings.py` and verify it does not import any symbols that moved to config.py. If it doesn't, no change needed (as expected by the design doc).

After all changes, run `uv run pytest` and `uvx prek run --all-files`. Then verify line counts:
```bash
wc -l src/ccrecall/config.py src/ccrecall/db.py
```

## Focus

- This is mechanical work — read each file's import line, classify each symbol, split into two lines. No logic changes.
- `session_tail.py` imports `DEFAULT_PROJECTS_DIR` from `db.py` — that stays in `db.py`, so `session_tail.py` needs no change.
- Some files may import `db` as a module alias (`import ccrecall.db as db_module`) in tests. These need checking too.
- After this task, every source file imports config-bound symbols from `ccrecall.config` and db-bound symbols from `ccrecall.db`. No file should import config-bound symbols from `ccrecall.db`.

## Verify

- [ ] AC#7: `wc -l` on `config.py` and `db.py` both show under 400 lines
- [ ] AC#7: `grep -rn 'from ccrecall.db import.*load_settings\|from ccrecall.db import.*setup_logging\|from ccrecall.db import.*log_hook_exception' src/` returns no hits (config symbols no longer imported from db)
- [ ] AC#7: `uv run pytest` passes with zero failures
- [ ] AC#7: `uvx prek run --all-files` passes
