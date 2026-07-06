---
task_id: "T03"
title: "Create config.py and repoint config-only callers"
status: "done"
depends_on: ["T01", "T02"]
implements: ["FR#8", "FR#9", "FR#10", "AC#3", "AC#4"]
---

## Summary

Create `src/ccrecall/config.py` by moving lightweight functions and constants out of `db.py` — paths, config loading, PID files, logging, settings. Zero heavy dependencies. Repoint the four callers that import ONLY config-bound symbols (`health.py`, `memory_sync.py`, `clear_handoff.py`, `warm_model.py`) so they import from `config.py` instead of `db.py`. Add transitive-import guard tests proving these modules don't pull in fastembed/onnxruntime/sqlite_vec.

## Target Files

- create: `src/ccrecall/config.py`
- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/health.py`
- modify: `src/ccrecall/hooks/memory_sync.py`
- modify: `src/ccrecall/hooks/clear_handoff.py`
- modify: `src/ccrecall/hooks/warm_model.py`
- modify: `tests/test_db.py`
- read: `design/specs/002-cleanup-plan/design.md` (§ Architecture → db.py split)

## Prompt

Create `src/ccrecall/config.py` by extracting functions and constants from `db.py`. Then repoint the four config-only callers and add import guard tests.

### What moves to config.py

**Constants:** `RUNTIME_DIR`, `DEFAULT_DB_PATH`, `CONFIG_PATH`, `DEFAULT_LOG_PATH`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`, `DEFAULT_SETTINGS`, `SYNC_TEMP_PREFIX`, `CLEAR_HANDOFF_FILENAME`, `PID_FILE_MODE`.

**Functions:** `load_config`, `load_settings`, `get_db_path`, `ensure_parent_dir`, `pid_file_path`, `remove_pid_file`, `log_hook_exception`, `setup_logging`, `atomic_write_json`.

`config.py` must have **zero imports** from `ccrecall.embeddings`, `sqlite_vec`, `ccrecall.schema`, or any heavy dependency. Its imports should be limited to stdlib (`json`, `os`, `sqlite3`, `logging`, `pathlib`, `tempfile`, `contextlib`, `logging.handlers`) and `ccrecall.models` (for `LOGGER_NAME`).

### What stays in db.py

**Constants:** `DEFAULT_PROJECTS_DIR`, `CONTENT_ERROR_VERSION`, `EMBEDDABLE_BRANCH_FILTER`, `CHUNK_EMBEDDABLE_BRANCH_FILTER`, `VEC_BUSY_TIMEOUT_MS`.

**Functions:** `get_db_connection`, `apply_base_pragmas`, `vec_available`, `chunk_vec_queryable`, `write_chunk_embedding`, `_ensure_vec_schema`, `branch_embedding_coverage`, `escape_like`, `fetch_branch_messages`, `parse_project_filter`, `resolve_db_settings`, `upsert_chunk_vec`.

Add `from ccrecall.config import ...` in `db.py` for any config symbols it still needs (e.g., `DEFAULT_DB_PATH`, `get_db_path`, `load_settings`, `setup_logging`).

### Repoint config-only callers

These four files import ONLY symbols that move to config.py — update their imports from `ccrecall.db` to `ccrecall.config`:

1. `health.py`: `PID_FILE_MODE, RUNTIME_DIR, atomic_write_json` → `from ccrecall.config import ...`
2. `hooks/memory_sync.py`: `SYNC_TEMP_PREFIX, log_hook_exception` → `from ccrecall.config import ...`
3. `hooks/clear_handoff.py`: `CLEAR_HANDOFF_FILENAME, get_db_path, load_settings, log_hook_exception` → `from ccrecall.config import ...`
4. `hooks/warm_model.py`: `remove_pid_file` → `from ccrecall.config import ...`

Do NOT repoint the mixed callers (hooks that import from both config and db) — that is T04's scope.

### Import guard tests

Add tests (in `tests/test_db.py` or a new section) that verify import isolation:

1. **AC#3**: Subprocess import of `ccrecall.hooks.memory_sync`, assert `fastembed`, `onnxruntime`, `sqlite_vec` not in `sys.modules`.
2. **AC#4**: Subprocess import of `ccrecall.health`, assert same.
3. **Bonus**: Subprocess import of `ccrecall.config`, assert same.

Pattern: `subprocess.run([sys.executable, "-c", "import ccrecall.config; import sys; heavy = {'fastembed', 'onnxruntime', 'sqlite_vec'}; found = heavy & set(sys.modules); assert not found, f'Heavy modules loaded: {found}'"])`.

After all changes, run `uv run pytest` and `uvx prek run --all-files`.

## Focus

- The critical constraint is that `config.py` must not transitively import anything heavy. `setup_logging` and `log_hook_exception` use only stdlib + `ccrecall.models.LOGGER_NAME`. Verify this before writing.
- `hooks/clear_handoff.py` and `hooks/warm_model.py` are not in the design doc's Impact section but DO need repointing (gap-check findings).
- Leave ALL mixed callers (memory_setup, memory_context, sync_current, import_conversations, backfill_embeddings, backfill_summaries, search_conversations, recent_chats, cli/commands.py) unchanged — they still import from `ccrecall.db` and will be repointed in T04.
- `db.py` after this task will still have the moved functions' code removed but may temporarily have some import adjustments. It must still work — all mixed callers still import from it.

## Verify

- [ ] FR#8: `config.py` exists with all listed functions/constants; `grep -rn 'from ccrecall.config' src/` shows it's imported
- [ ] FR#9: `health.py` imports from `ccrecall.config`, not `ccrecall.db`
- [ ] FR#10: `memory_sync.py`, `clear_handoff.py`, `warm_model.py` import only from `ccrecall.config`
- [ ] AC#3: Subprocess import of `ccrecall.hooks.memory_sync` does not load fastembed/onnxruntime/sqlite_vec
- [ ] AC#4: Subprocess import of `ccrecall.health` does not load fastembed/onnxruntime/sqlite_vec
- [ ] FR#8: `uv run pytest` passes with zero failures
- [ ] FR#8: `uvx prek run --all-files` passes
