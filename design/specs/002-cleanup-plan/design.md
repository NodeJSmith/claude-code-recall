# Design: ccrecall Productionization Cleanup

**Date:** 2026-07-05
**Status:** approved
**Scope-mode:** hold

## Problem

The ccrecall codebase biases Claude toward writing messy code that matches the existing patterns. It has accumulated three dead subsystems (~3,238 lines of source that serve no active purpose), a branch identity bug that silently creates churn snapshots on every sync (43% of inactive rows are churn, not genuine rewinds — and 88% of all branch rows are inactive), a monolithic `db.py` that drags heavy imports (fastembed/numpy/sqlite-vec, ~1800ms) onto every code path including hooks that don't need them, an 879-line search module mixing five concerns, inconsistent connection management with a live connection leak, sparse logging that misses entire subsystems, no schema versioning, and a dead FTS index with write-amplifying triggers that nothing reads.

The tool works — but every change risks perpetuating these patterns, and debugging production issues is harder than it should be.

## Goals

- Clean codebase that biases Claude toward good patterns when writing new code
- All tests pass after restructuring
- Hooks run within their timeouts (10s for SessionStart, 30s for Stop)
- Search returns equivalent results (minus abandoned/rewound content that was never supposed to surface)
- Every significant operation is logged to a per-process rotating file
- Schema changes are version-tracked and applied transactionally
- DB connections are managed via context managers — no leak paths

## Non-Goals

- New search capabilities or CLI commands
- Changes to the JSONL parsing contract (parsing.py tracks an undocumented external format)
- Changes to the embedding model or vector dimensions
- Changes to the plugin skill files (skills/ directory), except `skills/ccr-tokens/` which must be removed alongside the token analytics subsystem it depends on
- Changes to the hook stdout event contract (`{"continue": true}`)
- Per-message keyword search (filed as issue #53 for future work)

## User Scenarios

### Developer: ccrecall maintainer
- **Goal:** Make changes to ccrecall without fighting the codebase
- **Context:** Working in any of the source modules

#### Clean module boundaries
1. **Opens a search-related file**
   - Sees: a focused module (~130-215 lines) with a single concern
   - Decides: which module to edit based on the concern (query building, vector execution, hydration, orchestration, or CLI)
   - Then: edits without needing to understand 879 lines of mixed concerns

#### Debugging a production issue
1. **Checks the logs after a user reports stale search results**
   - Sees: per-process log files (ccrecall-sync.log, ccrecall-import.log, etc.) with INFO-level entries for every DB write, search query, and embedding operation
   - Decides: which process's log to inspect based on the symptom
   - Then: traces the issue through timestamped log entries without needing to reproduce

### User: ccrecall consumer
- **Goal:** Use ccrecall for conversation recall without noticing the restructuring
- **Context:** Normal Claude Code session with ccrecall hooks firing

#### Transparent upgrade
1. **Starts a new Claude Code session after upgrading ccrecall**
   - Sees: SessionStart hooks fire within timeout, context injection works, search returns relevant results
   - Decides: nothing — the upgrade is invisible
   - Then: works normally; the one-time migration cleans dead branch data in the background

## Functional Requirements

- **FR#1** The token analytics subsystem (token_schema, token_parser, token_analytics, token_output, token_insights, token_dashboard) is removed from the package, including CLI commands, test fixtures, test helpers, and the `skills/ccr-tokens/` skill directory
- **FR#2** The onboarding system (hooks/onboarding.py, hooks/write_config.py) is removed and replaced with "create config with defaults if config.json is missing"
- **FR#3** The legacy migration system (legacy.py) is removed, including hook control flow in memory_setup.py and CLI commands
- **FR#4** Branch rows are keyed by session_id (one row per session, updated in place) instead of by leaf_uuid (which creates a new row on every incremental sync)
- **FR#5** `find_all_branches()` returns only the active branch's message UUIDs; abandoned-fork detection code is removed
- **FR#6** The `is_active` column and all `is_active = 1` read filters are retained permanently as guards against pre-existing inactive rows
- **FR#7** A one-time migration deletes `is_active = 0` branch rows and their associated `branch_messages`, `chunks`, and `chunk_vec` rows in FK-safe order
- **FR#8** `db.py` is split into `config.py` (paths, config loading, PID files, logging, settings — zero heavy dependencies) and `db.py` (connections, schema, vec operations — imports sqlite_vec and embeddings)
- **FR#9** `health.py` imports only from `config.py`, not from `db.py` — verified by a transitive-import test
- **FR#10** Hook entry points remain as 5 separate console scripts (6 minus `ccrecall-onboarding`); hooks that don't need DB connections import only `config.py`
- **FR#11** `search_conversations.py` is decomposed into 5 modules: `search_query.py`, `search_vector.py`, `search_hydrate.py`, `search_conversations.py` (orchestrators), and `search_cli.py` (CLI entry points)
- **FR#12** Every DB write, search query, embedding operation, background process spawn, and error is logged at INFO or DEBUG level
- **FR#13** Each process type writes to its own rotating log file (e.g., `ccrecall-sync.log`, `ccrecall-import.log`)
- **FR#14** Schema version is tracked via `PRAGMA user_version`; DDL deltas and version bumps are wrapped in a single `BEGIN IMMEDIATE ... COMMIT` transaction
- **FR#15** Recurring self-heal checks (like `_ensure_vec_schema`'s dimension-mismatch detection) run on every connection, outside the version gate
- **FR#16** `get_db_connection()` is renamed to `get_connection()` and returns a context manager; all callers use `with get_connection(settings) as conn:`
- **FR#17** The `messages_fts` virtual table and its insert/update/delete triggers are dropped
- **FR#18** A branch-count invariant check (one active branch per session) is added to `ccrecall stats` and logged at WARNING when violated
- **FR#19** The `onboarding_completed` gate in `memory_context.py` (lines 593-603) is removed so context injection works for fresh installs

## Edge Cases

- **Concurrent sync + import**: `import_conversations` and `sync_current` have independent PID guards with no cross-guard. With session-keyed single rows, two concurrent writers to the same session could race on `diff_branch_messages`. The `import_log` file-hash check does NOT skip recently-synced sessions — `sync_current` writes `file_hash=NULL` (treated as stale), so a full import will reprocess the same session. Mitigated primarily by SQLite's WAL-mode busy timeout (5000ms base) serializing the writes, and by the fact that both writers converge on the same correct state (the active branch's current messages). A stale import's `diff_branch_messages` might temporarily remove links the live sync just added, but the next sync re-adds them. This is a pre-existing race, not introduced by this cleanup — the session-keyed identity change doesn't worsen it.
- **Pre-existing inactive rows**: The 3,295 inactive branch rows in the live DB must be cleaned before the migration is "complete," but the tool must remain functional during the cleanup (queries still filter on `is_active = 1`).
- **Partial migration crash**: If the one-time migration crashes mid-delete, remaining inactive rows are still guarded by `is_active = 1` filters. The migration is re-entrant — re-running deletes whatever's left.
- **Stale log files**: Per-process log files accumulate independently. Each process type rotates its own file (1MB, 2 backups = 3MB max per process type). With 9 process types, max total is ~27MB.
- **Fresh install with empty DB**: On a genuinely fresh install (`user_version` = 0, no tables), the version-gated migration's DML (DELETE, table rebuild) would fail if it ran before schema creation. Mitigated by running the version check **after** `executescript(SCHEMA_CORE)` — the idempotent `CREATE TABLE IF NOT EXISTS` statements create tables first, then the migration runs against a DB that has the expected schema. On fresh install with no inactive rows, the DELETE is a no-op and the table rebuild applies the new constraint cleanly.
- **Empty config on fresh install**: With onboarding removed, `load_config()` already returns `{}` on missing file. `load_settings()` merges defaults, so all keys are always present.

## Acceptance Criteria

- **AC#1** `uv run pytest` passes with zero failures (FR#1-FR#19)
- **AC#2** `uvx prek run --all-files` passes (lint, format, type-check, custom checks)
- **AC#3** Importing `ccrecall.hooks.memory_sync` in a subprocess does not load `fastembed`, `onnxruntime`, or `sqlite_vec` into `sys.modules` (FR#8, FR#10)
- **AC#4** Importing `ccrecall.health` in a subprocess does not load `fastembed`, `onnxruntime`, or `sqlite_vec` into `sys.modules` (FR#9)
- **AC#5** After running the one-time migration, `SELECT COUNT(*) FROM branches WHERE is_active = 0` returns 0 (FR#7)
- **AC#6** Syncing a session 10 times incrementally produces exactly 1 branch row with `is_active = 1` (FR#4, FR#18)
- **AC#7** No module that was restructured by this work (`db.py` split, `search_conversations.py` decomposition) exceeds 400 lines. Pre-existing large modules (`session_ops.py` 752, `hooks/memory_context.py` 701, `hooks/backfill_embeddings.py` 502) are out of scope for this cleanup (FR#8, FR#11)
- **AC#8** `ccrecall search --query "test"` returns results equivalent to pre-restructuring (no abandoned content surfaced) (FR#5, FR#6)
- **AC#9** Log files exist at `~/.ccrecall/ccrecall-<process>.log` after running hooks (FR#12, FR#13)
- **AC#10** `PRAGMA user_version` on the conversations DB returns the current schema version after connection (FR#14)
- **AC#11** `get_connection()` used as a context manager closes the connection on both success and exception paths (FR#16)

## Key Constraints

- The embedding watermark protocol (clear-first/set-last in `embed_branch_chunks`) must be preserved exactly — it ensures mid-crash safety for chunk vectors. The session-keyed row identity change must not alter the watermark's transaction boundary.
- Hook entry points must print valid JSON to stdout on every exit path, including after unhandled exceptions. The `log_hook_exception` + out-of-try-block print pattern must be maintained.
- The `no-lazy-imports` lint rule is absolute — the `db.py` split uses module-level restructuring, not function-level lazy loading. No lint exceptions.
- `PRAGMA foreign_keys = ON` is set on every connection (production and test). The dead-branch cleanup migration must delete in FK-safe order: `branch_messages` first, then `chunks` (cascade handles `chunk_vec`), then `branches`.

## Dependencies and Assumptions

- SQLite's `PRAGMA user_version` is an integer stored in the DB header — atomic to read/write, survives WAL checkpoints, available in all SQLite versions ccrecall supports.
- The `prek` pre-commit framework enforces no-future-annotations, no-lazy-imports, and custom checks. All restructured code must pass these.
- Both install paths (pip/uv package + Claude Code plugin) must work. Changes to console-script entry points in `pyproject.toml` must be reflected in `hooks/hooks.json` for the plugin path.
- The live production DB (~166MB, 3,730 branch rows, 26,456 messages, 191,881 branch_messages links) is the real-world test case for migration correctness.

## Architecture

### Deletion pass (tokens, onboarding, legacy)

Delete the three subsystems and clean their blast radius into shared files. Order: tokens first (largest, most isolated), then onboarding, then legacy (has a dependency on onboarding via `hooks/onboarding.py` importing `legacy.find_legacy_db`).

**Token analytics** (2,936 source + 1,715 test lines): Delete 6 source files (`token_schema.py`, `token_parser.py`, `token_analytics.py`, `token_output.py`, `token_insights.py`, `token_dashboard.py`), 5 test files (`test_ingest_token_data.py`, `test_token_output.py`, `test_token_insights.py`, `test_token_parser.py`, `token_helpers.py`), and clean shared files: `cli/commands.py` (remove `cmd_tokens` import + command), `conftest.py` (remove token-specific imports at lines 8, 13-15; remove `token_db`/`populated_token_db` fixtures including their `@pytest.fixture` decorators), `test_db.py` (remove `token_schema` import, `TestNoTokenSnapshotsOnConversationDb` class, token_snapshots simulation test), `test_boundary_validation.py` (remove `token_parser` import, `_jnl` helper at lines 75-81, `TestTokenValidation` class at lines 84-120).

**Onboarding** (130 source + 356 test lines): Delete `hooks/onboarding.py`, `hooks/write_config.py`, `test_onboarding.py`, `test_write_config.py`. Clean shared files: `cli/commands.py` (remove `cmd_write_config` import + command), `db.py` (remove `CURRENT_ONBOARDING_VERSION`), `hooks/memory_context.py` (remove onboarding gate at lines 593-603, keep line 591), `test_db.py` (remove `TestCurrentOnboardingVersion`). Replace onboarding flow: `load_config()` already returns `{}` on missing file; `load_settings()` already merges defaults. No new code needed — just remove the gate.

**Legacy migration** (172 source + 230 test lines): Delete `legacy.py`, `test_legacy_migration.py`. Clean shared files: `hooks/memory_setup.py` — remove imports at lines 26-27, `MIGRATION_NOTICE` string at line 36, and the legacy-specific code within lines 161-182. Carefully preserve the essential first-run logic in the same range: the `if db_absent:` initial-import trigger and the `elif _needs_reimport(settings):` re-import trigger must stay — only remove `legacy_db = find_legacy_db()`, the `if legacy_db is not None:` branch that spawns `ccrecall migrate` and sets `MIGRATION_NOTICE`, and the `legacy_db is None and` guard on the backfill trigger. Also: `cli/commands.py` (remove `cmd_migrate` import + command), `tests/test_sync_hook.py` (remove 3 monkeypatches at lines 784, 806, 825).

Remove `ccrecall-onboarding` from `pyproject.toml` console scripts and `hooks/hooks.json`.

### Branch identity fix

Change `upsert_branch` to key on `session_id` instead of `leaf_uuid`. The current pattern builds an `existing_branches` dict keyed by `leaf_uuid` (`session_ops.py:737-738`) and does a dict lookup to decide insert vs update. The new pattern queries for an existing branch by `session_id` directly:

```python
cursor.execute("SELECT id FROM branches WHERE session_id = ? AND is_active = 1", (session_id,))
row = cursor.fetchone()
if row:
    branch_db_id = row[0]
    update_branch_row(cursor, branch_db_id, ...)
else:
    branch_db_id = insert_branch_row(cursor, session_id, ...)
```

This eliminates the `existing_branches: dict[str, int]` parameter from `upsert_branch`'s signature and the dict-building query at `session_ops.py:737-738` (`SELECT id, leaf_uuid FROM branches WHERE session_id = ?`) — both become dead code since lookup is now a direct query by `session_id`. The `sync_branch` function that threads this parameter also drops it.

Remove `enforce_single_active_branch` — no longer needed since there's only ever one row per session. Remove `fork_point_uuid` from inserts/updates (always NULL now). Continue writing `leaf_uuid` on each sync (it's still a useful diagnostic field showing the latest message UUID, even though it's no longer the identity key — the table rebuild drops it from the UNIQUE constraint but keeps the column). Remove `MAX_BRANCH_DEPTH` constant from `parsing.py` (only used by deleted abandoned-fork detection code). Simplify `find_all_branches` to return only the active branch (delete the abandoned-fork detection code at `parsing.py:167-220` — preserve line 221 which is the function's `return branches` statement). The `UNIQUE(session_id, leaf_uuid)` constraint changes to `UNIQUE(session_id)` via a table rebuild in the migration (see Migration section for sequencing).

### Dead branch cleanup migration

A one-time migration runs on `get_db_connection` when `user_version < target_version`. Delete order (FK-safe):
1. `DELETE FROM branch_messages WHERE branch_id IN (SELECT id FROM branches WHERE is_active = 0)`
2. `DELETE FROM chunks WHERE branch_id IN (SELECT id FROM branches WHERE is_active = 0)` (cascade trigger handles `chunk_vec`)
3. `DELETE FROM branches WHERE is_active = 0`

This is re-entrant — re-running on a DB where some rows were already deleted is safe (the subqueries return fewer IDs).

### Drop messages_fts

Remove the `messages_fts` virtual table definition and its three triggers (`messages_ai`, `messages_ad`, `messages_au`) from `schema.py` — note that `schema.py` defines these in two places: once in `SCHEMA_FTS5` and once in the `SCHEMA_FTS4` fallback. Both copies must be removed. **Do not touch the adjacent `branches_fts` definition and its triggers (`branches_ai`, `branches_ad`, `branches_au`) — `branches_fts` is the live keyword search index queried by `search_conversations.py::_get_fts_branch_ids`.** Add a DDL delta in the migration: `DROP TABLE IF EXISTS messages_fts`. The FTS shadow tables are automatically cleaned up when the virtual table is dropped.

### db.py split

Create `config.py` with these functions/constants moved from `db.py`: `RUNTIME_DIR`, `DEFAULT_DB_PATH`, `CONFIG_PATH`, `DEFAULT_LOG_PATH`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`, `DEFAULT_SETTINGS`, `SYNC_TEMP_PREFIX`, `CLEAR_HANDOFF_FILENAME`, `PID_FILE_MODE`, `load_config`, `load_settings`, `get_db_path`, `ensure_parent_dir`, `pid_file_path`, `remove_pid_file`, `log_hook_exception`, `setup_logging`, `atomic_write_json`. Zero imports from `embeddings`, `sqlite_vec`, or any heavy dependency. Note: `CONFIG_PATH` is used by `load_config()` and `DEFAULT_LOG_PATH` is used by `setup_logging()` — both must move with their callers.

Constants remaining in `db.py` that are not in either list (not load-bearing for the split, stay where they are): `DEFAULT_PROJECTS_DIR`, `CONTENT_ERROR_VERSION`, plus functions `parse_project_filter`, `resolve_db_settings`, `upsert_chunk_vec`.

`db.py` retains (with `get_db_connection` renamed to `get_connection` and wrapped as a context manager): `get_connection`, `apply_base_pragmas`, `vec_available`, `chunk_vec_queryable`, `write_chunk_embedding`, `_ensure_vec_schema`, `branch_embedding_coverage`, `EMBEDDABLE_BRANCH_FILTER`, `CHUNK_EMBEDDABLE_BRANCH_FILTER`, `VEC_BUSY_TIMEOUT_MS` (note: `BUSY_TIMEOUT_MS` is imported from `models.py`, not defined in `db.py`), `escape_like`, `fetch_branch_messages`. These import from `config.py` for paths/settings and from `embeddings`/`sqlite_vec` for vec operations.

Repoint `health.py` imports from `db` to `config` (`PID_FILE_MODE`, `RUNTIME_DIR`, `atomic_write_json`). Add a transitive-import test: subprocess import of `ccrecall.health`, assert `fastembed`/`onnxruntime`/`sqlite_vec` not in `sys.modules`.

### Per-process logging

Modify `setup_logging` (moving to `config.py`) to accept a `process_name` parameter. Each process type writes to `~/.ccrecall/ccrecall-<process_name>.log` with independent rotation (1MB, 2 backups). Canonical process names and their mapping from existing `log_hook_exception` context strings:

| Process name | Log file | Call-site context string |
|---|---|---|
| `setup` | `ccrecall-setup.log` | `"memory-setup"` → rename to `"setup"` |
| `sync` | `ccrecall-sync.log` | `"memory-sync"` → rename to `"sync"` in `memory_sync.py`; also add `process_name="sync"` to `sync_current.py`'s existing `setup_logging()` call at line 172 (the detached worker doing the actual DB writes) |
| `context` | `ccrecall-context.log` | (memory_context uses `sys.exit(0)`, no `log_hook_exception` call — add one) |
| `clear-handoff` | `ccrecall-clear-handoff.log` | `"clear-handoff"` (keep as-is) |
| `import` | `ccrecall-import.log` | (background process, uses `setup_logging` directly) |
| `backfill-embed` | `ccrecall-backfill-embed.log` | (background process, uses `setup_logging` directly) |
| `backfill-summary` | `ccrecall-backfill-summary.log` | (background process, uses `setup_logging` directly) |
| `warm-model` | `ccrecall-warm-model.log` | (background process, no logging today — add `setup_logging("warm-model")` call) |
| `cli` | `ccrecall-cli.log` | (CLI commands, no logging today — add `setup_logging("cli")` call in CLI entry point) |

### Schema versioning

Add `PRAGMA user_version` check in `get_connection`, **after** the existing `conn.executescript(SCHEMA_CORE)` and FTS setup (which use `CREATE TABLE IF NOT EXISTS` and are safe to re-run). This ordering is critical: on a fresh install the tables must exist before the migration's DML runs. Current schema gets version 1. On version mismatch:
1. `BEGIN IMMEDIATE`
2. Run idempotent DDL deltas for each version step
3. `PRAGMA user_version = <target>`
4. `COMMIT`

On exception: transaction rolls back, `user_version` stays at old value, next connection retries. `_ensure_vec_schema`'s self-heal checks stay outside the version gate — they run on every vec-loaded connection as today.

### Connection management

Wrap `get_db_connection` in a `@contextlib.contextmanager`:

```python
@contextlib.contextmanager
def get_connection(settings, load_vec=False):
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

Migrate all callers. Use the `sync_current.py` connection leak (conn opened, abandoned on exception) as the characterization test case proving the new pattern closes on error paths.

### Search decomposition

Split `search_conversations.py` (879 lines) into 5 modules:

| Module | Functions | Est. lines |
|---|---|---|
| `search_query.py` | `scope_filter_clause`, `_get_fts_branch_ids`, constants | ~134 |
| `search_vector.py` | `_execute_chunk_knn`, `_get_vec_chunk_ids`, `_hydrate_snippets` | ~170 |
| `search_hydrate.py` | `_dedup_by_session`, `_hydrate_cards` | ~165 |
| `search_conversations.py` | `search_sessions`, `search_messages`, `_compute_caveat` | ~175 |
| `search_cli.py` | `run`, `run_messages`, `print_status`, format wrappers, `MAX_SEARCH_RESULTS` | ~215 |

Boundary types are simple: branch IDs (`int`), score tuples (`tuple[int, float]`), result dicts (`dict`). No custom types cross module boundaries. Existing `formatting.py` (381 lines) and `fusion.py` (40 lines) are unchanged.

### Branch invariant check

Add to `ccrecall stats`: `SELECT session_id, COUNT(*) as cnt FROM branches WHERE is_active = 1 GROUP BY session_id HAVING cnt > 1`. Log WARNING if any rows returned. This lands in the same commit as the branch identity fix.

## Implementation Preferences

- **Module naming**: lightweight split = `config.py`, heavy = `db.py`
- **Logging**: per-process-type rotating files via `RotatingFileHandler`, process name passed to `setup_logging`
- **Schema versioning**: `PRAGMA user_version` with transactional DDL, no external migration framework
- **Connection management**: `@contextlib.contextmanager` wrapper with commit-on-success, rollback-on-exception
- **CLI framework**: cyclopts (existing — no change)
- **Build backend**: setuptools (existing — no change)

## Replacement Targets

| Target | Replaced by | Action |
|---|---|---|
| Token analytics (6 source files, 5 test files) | Nothing — capability removed | Delete entirely |
| Onboarding system (2 source files, 2 test files) | Default config on missing file (existing `load_config`/`load_settings` behavior) | Delete + remove gate |
| Legacy migration (1 source file, 1 test file) | Nothing — one-time migration already ran | Delete + clean hooks |
| `upsert_branch` leaf_uuid keying | Session-keyed single row identity | Rewrite function |
| `enforce_single_active_branch` | Session-keyed identity (one row by construction) | Delete |
| `find_all_branches` multi-branch detection | Single active branch return | Simplify (delete ~55 lines) |
| `db.py` (monolithic) | `config.py` + `db.py` (split) | Split module |
| `search_conversations.py` (879 lines) | 5 focused modules | Decompose |
| `setup_logging` (shared rotating file) | Per-process-type rotating files | Modify |
| `get_db_connection` (raw connection) | Context manager wrapper | Wrap |
| `messages_fts` + triggers | Nothing — dead index | Drop |

## Migration

### Migration sequence (order matters)

The migration runs within the version-gated transaction (`BEGIN IMMEDIATE ... COMMIT` with `PRAGMA user_version` bump). Steps execute in this order:

1. **DML first — delete dead rows** before constraint changes: delete all `is_active = 0` branch rows in FK-safe order (`branch_messages` → `chunks` cascade to `chunk_vec` → `branches`). This cleans 3,295 inactive branch rows, ~165k `branch_messages` links, and 1,696 embedded chunk vectors from the live DB. Must run before step 3 or the new UNIQUE constraint will fail on duplicate `session_id` values.

2. **Drop `messages_fts`** virtual table and its triggers (both the FTS5 version in `SCHEMA_FTS5` and the FTS4 fallback in `SCHEMA_FTS4` — both copies must be removed from `schema.py`).

3. **Rebuild `branches` table** to change the UNIQUE constraint from `UNIQUE(session_id, leaf_uuid)` to `UNIQUE(session_id)`. Because `schema.py` uses `CREATE TABLE IF NOT EXISTS`, editing the inline constraint has no effect on an existing table — an explicit SQLite table-rebuild is required: create new table with the target schema, copy data, drop old, rename new. This runs after step 1 so no duplicate `session_id` values remain. **Critical:** dropping the old `branches` table auto-drops all triggers defined on it, including the `branches_fts` sync triggers (`branches_ai`, `branches_ad`, `branches_au`) that keep the live `branches_fts` keyword search index up to date. These triggers must be re-created after the rename — run the FTS trigger DDL from `schema.py`'s `SCHEMA_FTS5` (or `SCHEMA_FTS4` fallback) after the rebuild step. Verify `branches_fts` is still queryable after the migration.

4. **Set `PRAGMA user_version`** to the target version.

Migration is re-entrant: step 1's DELETE is safe to re-run (subqueries return fewer IDs each time), step 2's DROP IF EXISTS is idempotent, step 3 can check whether the old table exists before rebuilding.

### Reversibility
- The DB is a derived cache of JSONL transcripts. Full recovery = drop DB + re-import via `ccrecall import`. The migration is not reversible in-place, but full rebuild from source-of-truth is always available.

## Convention Examples

### Hook error handling — try/except + log_hook_exception

**Source:** `src/ccrecall/hooks/memory_setup.py:148-208`

```python
def main():
    additional_context: str | None = None
    try:
        # ... all hook logic ...
    except Exception:
        log_hook_exception("memory-setup")

    # OUTSIDE the try -- always runs
    output: dict = {"continue": True}
    if additional_context is not None:
        output["hookSpecificOutput"] = { ... }
    print(json.dumps(output))
```

### PID-file concurrency guard — O_CREAT|O_EXCL atomic lock

**Source:** `src/ccrecall/hooks/sync_current.py:130-168`

```python
pid_path = pid_file_path(PID_KEY)
ensure_parent_dir(pid_path)
while True:
    try:
        lock_fd = os.open(str(pid_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, PID_FILE_MODE)
    except FileExistsError:
        try:
            existing_pid = int(pid_path.read_text().strip())
            os.kill(existing_pid, 0)
            return  # another instance alive
        except PermissionError:
            return  # can't probe — treat as alive, don't take lock
        except ValueError:
            with contextlib.suppress(OSError):
                pid_path.unlink()
            continue
        except OSError:
            with contextlib.suppress(OSError):
                pid_path.unlink()
            continue
    else:
        try:
            os.write(lock_fd, str(os.getpid()).encode())
        finally:
            os.close(lock_fd)
        break

try:
    # ... guarded work ...
finally:
    remove_pid_file(PID_KEY)
```

### Embedding watermark protocol — clear-first / set-last

**Source:** `src/ccrecall/session_ops.py:500-570`

```python
# Step 1: Clear watermark BEFORE embed loop
if needing_embed_full:
    cursor.execute("UPDATE branches SET embedding_version = 0 WHERE id = ?", (branch_db_id,))

# Step 2: Embed loop — vector FIRST, bookkeeping LAST
for ed in needing_embed:
    cursor.execute("INSERT INTO chunks (..., embedding_version) VALUES (..., 0)", ...)
    chunk_id = cursor.lastrowid
    vec = embed_text(ed["text"])
    write_chunk_embedding(cursor, chunk_id, vec, EMBEDDING_VERSION, EMBEDDING_MODEL)

# Step 3: Set watermark ONLY after all exchanges are current
if all_current:
    _stamp_branch_watermark(cursor, branch_db_id)
```

### Test fixture — in-memory SQLite with schema

**Source:** `tests/conftest.py:52-64`

```python
@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    yield conn
    conn.close()
```

DO: Always set `PRAGMA foreign_keys = ON` to match production. DON'T: Skip it — FK-violating deletes pass in tests but fail at runtime.

### Connection lifecycle — current pattern (being replaced)

**Source:** `src/ccrecall/hooks/sync_current.py:226-254`

```python
# CURRENT (raw connection, manual close, leak on exception):
conn = get_db_connection(settings, load_vec=True)
# ... work that can raise ...
conn.commit()
conn.close()  # never reached on exception

# NEW (context manager, auto-close on all paths):
with get_connection(settings, load_vec=True) as conn:
    # ... work ...
    # commit on success, rollback + close on exception
```

## Alternatives Considered

**Ground-up rewrite**: Would give a perfectly clean codebase but risks introducing subtle regressions in tricky logic (JSONL parsing, branch detection, embedding watermark protocol). The middle path — restructure, not rewrite — keeps working core logic while reorganizing module boundaries. Lower risk, still achieves clean codebase.

**Unified hook dispatcher**: A single `ccrecall hook <event>` entry point would simplify packaging. Challenge proved this is impossible without lazy imports — a module-level dict dispatch eagerly imports all handlers, dragging the heavy fastembed stack onto every hook invocation. Also loses per-hook failure isolation. Keeping 6 separate entry points is the only option that actually delivers the import-time win.

**Drop `is_active` column entirely**: Since new code only writes active branches, the column becomes always-true and seems redundant. But the live DB has 3,295 pre-existing inactive rows with embedded chunks — dropping the filter without cleaning them resurrects abandoned content in search. Keeping the filter is cheaper than ensuring the migration has run on every machine before the filter can be safely removed.

**Alembic for schema migrations**: A full migration framework would provide rollback support and migration history. But `PRAGMA user_version` with transactional DDL is proportionate for a single-DB tool where the DB is a derived cache of JSONL transcripts. Full rebuild from source-of-truth is always available as the nuclear rollback.

## Test Strategy

### Existing Tests to Adapt

- `tests/conftest.py`: Remove only the token-specific imports (lines 8, 13-15: `token_helpers`, `token_analytics`, `token_parser`, `token_schema` — do NOT delete lines 10-12 which import `_ensure_vec_schema`, `health`, and `SCHEMA` used by non-token fixtures). Remove the `token_db`/`populated_token_db` fixture functions including their `@pytest.fixture` decorators (start from the decorator line, not the `def` line)
- `tests/test_db.py`: Remove `token_schema` import (line 26), remove `CURRENT_ONBOARDING_VERSION` from the `from ccrecall.db import (...)` block (line 14), `TestCurrentOnboardingVersion` class (including `class` line), `TestNoTokenSnapshotsOnConversationDb` class (including `class` line). In `TestExistingV6DbOpen`, remove only the token_snapshots-specific lines (CREATE TABLE token_snapshots, INSERT, and the final token_snapshots assertion) — preserve the core test assertions (PRAGMA user_version, get_db_connection reopen, projects/sessions/branches/messages row-count checks). Add schema-version tests.
- `tests/test_boundary_validation.py`: Remove `token_parser` import (line 15), `_jnl` helper (lines 75-81), `TestTokenValidation` (lines 84-120)
- `tests/test_sync_hook.py`: Remove 3 legacy monkeypatches (lines 784, 806, 825). Add/update tests for session-keyed branch identity.
- `tests/test_session_ops.py`: Rewrite branch upsert tests for session-keyed identity. Remove `enforce_single_active_branch` tests. Update `existing_branches` dict usage.
- `tests/test_parsing.py`: Structural rewrite of ~150/384 lines: remove `TestFindAllBranchesProperties` Hypothesis class (lines 108-153), synthetic multi-branch generators (lines 53-105), per-fixture branch-count expectations for rewind fixtures (lines 42-43). Simplify to single-branch assertions.
- `tests/test_search.py`: Update imports for 5-module split. Update `is_active` fixture data.
- `tests/test_import_pipeline.py`: Remove inactive branch references. Update import paths.
- `tests/test_context_injection.py`: Remove onboarding gate tests. Update `is_active` references.
- `tests/test_backfill_embeddings.py`: Update `is_active` references and import paths.
- `tests/test_integration.py`: Update for session-keyed identity and split imports.
- `tests/test_summarizer.py`, `tests/test_recent_chats.py`: Update `is_active` references.

### New Test Coverage

- **FR#4, FR#18**: Test that N sequential `sync_session` calls against a growing JSONL file produce exactly 1 branch row with `is_active = 1` (characterization test — run against current code first to confirm it fails, then against fix to confirm it passes)
- **FR#7**: Test one-time migration: seed DB with inactive branches + branch_messages + chunks, run migration, assert all deleted in correct order with no FK violations
- **FR#8, FR#9, FR#10**: Transitive-import tests for `config.py`, `health.py`, and `memory_sync` — subprocess import, assert heavy modules not in `sys.modules`
- **FR#14**: Test schema versioning: open connection at version 0, verify DDL applied and version bumped. Test crash recovery: partially apply DDL, verify next connection retries.
- **FR#16**: Test context manager closes connection on success and exception paths (use `sync_current.py` leak scenario as pin test)
- **FR#17**: Test `messages_fts` no longer exists after migration

### Tests to Remove

- `tests/test_ingest_token_data.py` (620 lines) — token analytics
- `tests/test_token_output.py` (211 lines) — token analytics
- `tests/test_token_insights.py` (530 lines) — token analytics
- `tests/test_token_parser.py` (313 lines) — token analytics
- `tests/token_helpers.py` (41 lines) — token test builders
- `tests/test_onboarding.py` (170 lines) — onboarding
- `tests/test_write_config.py` (186 lines) — onboarding config writer
- `tests/test_legacy_migration.py` (230 lines) — legacy migration

## Documentation Updates

- **CLAUDE.md**: Update architecture section (mention `config.py` split, session-keyed branches, per-process logging). Fix the "Two invariants to preserve" heading — it already documents three invariants despite saying "two"; update the heading to match the actual count after adding the branch-count invariant. Remove references to `token_*` modules. Update console-script/hook entry point list: remove `ccrecall-onboarding`, add `ccrecall-warm-model` (currently missing from the Names table). Update "Commands" section: remove `ccrecall tokens`/`ccrecall migrate`/`ccrecall write-config`.
- **pyproject.toml**: Remove `ccrecall-onboarding` from console scripts. Remove token-related entry points if any. Update module references.
- **hooks/hooks.json**: Remove `ccrecall-onboarding` hook entry.
- **.claude-plugin/plugin.json**: Update if it references removed hooks or commands.
- **CHANGELOG**: Handled automatically by release-please via conventional commits — no manual entry needed during implementation.

## Impact

<!-- Gap check 2026-07-05: 2 gaps included — hooks/clear_handoff.py (line 9: imports CLEAR_HANDOFF_FILENAME, get_db_path, load_settings, log_hook_exception from db.py, all move to config.py) → T03 Focus, hooks/warm_model.py (line 13: imports remove_pid_file from db.py, moves to config.py) → T03 Focus -->

### Changed Files

**Deleted files (source):**
- delete `src/ccrecall/token_schema.py`
- delete `src/ccrecall/token_parser.py`
- delete `src/ccrecall/token_analytics.py`
- delete `src/ccrecall/token_output.py`
- delete `src/ccrecall/token_insights.py`
- delete `src/ccrecall/token_dashboard.py`
- delete `src/ccrecall/legacy.py`
- delete `skills/ccr-tokens/` (skill directory — depends on removed token subsystem)
- delete `src/ccrecall/hooks/onboarding.py`
- delete `src/ccrecall/hooks/write_config.py`

**Deleted files (tests):**
- delete `tests/test_ingest_token_data.py`
- delete `tests/test_token_output.py`
- delete `tests/test_token_insights.py`
- delete `tests/test_token_parser.py`
- delete `tests/token_helpers.py`
- delete `tests/test_onboarding.py`
- delete `tests/test_write_config.py`
- delete `tests/test_legacy_migration.py`

**Created files:**
- create `src/ccrecall/config.py` — lightweight module split from db.py
- create `src/ccrecall/search_query.py` — FTS query building/execution
- create `src/ccrecall/search_vector.py` — KNN/vector execution + snippet hydration
- create `src/ccrecall/search_hydrate.py` — result hydration + dedup
- create `src/ccrecall/search_cli.py` — CLI entry points for search

**Modified files (source — cross-cutting, highest risk):**
- modify `src/ccrecall/db.py` — split out config/utils to config.py; add schema versioning; context manager wrapper; remove token/onboarding constants
- modify `src/ccrecall/schema.py` — drop messages_fts; update branch unique constraint
- modify `src/ccrecall/session_ops.py` — session-keyed branch identity; remove enforce_single_active_branch; remove fork_point_uuid
- modify `src/ccrecall/parsing.py` — simplify find_all_branches to return only active branch
- modify `src/ccrecall/cli/commands.py` — remove cmd_tokens, cmd_migrate, cmd_write_config; update imports
- modify `src/ccrecall/hooks/memory_setup.py` — remove legacy imports and migration control flow
- modify `src/ccrecall/hooks/memory_context.py` — remove onboarding gate (lines 593-603)

**Modified files (source — lower risk):**
- modify `src/ccrecall/__init__.py` — remove token submodule references from package docstring
- modify `src/ccrecall/health.py` — repoint imports from db to config
- modify `src/ccrecall/hooks/memory_sync.py` — repoint imports from db to config
- modify `src/ccrecall/hooks/sync_current.py` — use connection context manager
- modify `src/ccrecall/hooks/import_conversations.py` — use connection context manager; remove abandoned branch count
- modify `src/ccrecall/hooks/backfill_embeddings.py` — use connection context manager; update is_active references
- modify `src/ccrecall/hooks/backfill_summaries.py` — use connection context manager
- modify `src/ccrecall/search_conversations.py` — becomes orchestrator-only (functions moved to new modules)
- modify `src/ccrecall/recent_chats.py` — update imports from db to config where applicable
- modify `src/ccrecall/formatting.py` — update imports if needed
- modify `src/ccrecall/embeddings.py` — no functional change, but verify no config.py-candidate imports

**Modified files (tests):**
- modify `tests/conftest.py` — remove token fixtures
- modify `tests/test_db.py` — remove token/onboarding test classes; add schema version tests
- modify `tests/test_boundary_validation.py` — remove token validation class
- modify `tests/test_sync_hook.py` — remove legacy patches; add branch identity tests
- modify `tests/test_session_ops.py` — rewrite for session-keyed identity
- modify `tests/test_parsing.py` — structural rewrite (~150 lines) for single-branch
- modify `tests/test_search.py` — update imports for 5-module split
- modify `tests/test_import_pipeline.py` — update branch references
- modify `tests/test_context_injection.py` — remove onboarding gate tests
- modify `tests/test_backfill_embeddings.py` — update is_active references
- modify `tests/test_integration.py` — update for session-keyed identity
- modify `tests/test_summarizer.py` — update is_active references
- modify `tests/test_recent_chats.py` — update is_active references

**Modified files (config/packaging):**
- modify `pyproject.toml` — remove ccrecall-onboarding console script
- modify `hooks/hooks.json` — remove onboarding hook entry
- modify `.claude-plugin/plugin.json` — update if referencing removed hooks
- modify `CLAUDE.md` — update architecture docs

### Behavioral Invariants

- Hook stdout contract: every hook prints valid JSON with `"continue": true` on every exit path
- Search result equivalence: `ccrecall search` returns the same sessions/snippets for the same queries (minus content from abandoned/rewound branches that was already filtered by `is_active = 1`)
- Embedding watermark protocol: clear-first/set-last transaction boundary preserved exactly
- `PRAGMA foreign_keys = ON` on every connection (production and test)
- Plugin skill invocation: `/ccr-recall`, `/ccr-resume` continue working. `/ccr-tokens` is removed (skill directory deleted alongside the token subsystem)

### Blast Radius

- **ccrecall consumers** (the user): transparent — hooks, search, and context injection work the same
- **Plugin installs**: `hooks/hooks.json` changes (onboarding hook removed) — plugin consumers get the updated wiring on next install
- **CI**: test suite changes are substantial (~2,300 lines deleted, ~150 lines structurally rewritten, imports updated across ~13 files) — CI must pass before merge
- **Release-please**: conventional commits drive changelog automatically — no manual intervention needed

## Open Questions

None — all resolved during grill, investigation, and challenge phases.
