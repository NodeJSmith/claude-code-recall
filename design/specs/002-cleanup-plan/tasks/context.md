# Context: ccrecall Productionization Cleanup

## Problem & Motivation

The ccrecall codebase biases Claude toward writing messy code that matches existing patterns. Three dead subsystems (~3,238 lines) serve no purpose, a branch identity bug creates churn rows on every sync, a monolithic `db.py` drags heavy imports onto every hook, an 879-line search module mixes five concerns, connection management leaks on exception paths, logging misses entire subsystems, there's no schema versioning, and a dead FTS index writes to triggers nothing reads. The tool works, but every change risks perpetuating these patterns.

## Visual Artifacts

None.

## Key Decisions

1. **Middle path: restructure, not rewrite** — keep working core logic (parsing, branch detection, embed pipeline, PID guards), reorganize module boundaries, add logging/errors systematically, delete dead weight.
2. **Cut three subsystems entirely** — token analytics (2,936 lines), onboarding (130 lines), legacy migration (172 lines). No replacement needed for onboarding (default config on missing file) or legacy (already ran everywhere).
3. **Session-keyed branch identity** — key on `session_id` instead of `leaf_uuid` to eliminate churn rows. Keep `is_active` column + filters permanently as guards against 3,295 pre-existing inactive rows.
4. **Split db.py into config.py + db.py** — config.py (paths, config, PID files, logging — zero heavy deps) + db.py (connections, vec, schema). Hooks that don't need DB import only config.py.
5. **Keep 5 separate hook entry points** — unified dispatcher disproved (would eager-import heavy stack). Separate scripts preserve per-hook failure isolation.
6. **PRAGMA user_version for schema versioning** — DDL deltas wrapped in `BEGIN IMMEDIATE ... COMMIT`. No external migration framework.
7. **Context manager for connections** — `get_connection()` with commit-on-success, rollback-on-exception, always-close. Fixes the `sync_current.py` connection leak.
8. **Per-process rotating log files** — each process type writes to `~/.ccrecall/ccrecall-<process>.log` (1MB, 2 backups). Avoids multi-process rotation races.
9. **Drop messages_fts** — dead FTS index with write-amplifying triggers. `branches_fts` (the live search index) is preserved.

## Constraints & Anti-Patterns

- **Embedding watermark protocol** (clear-first/set-last in `embed_branch_chunks`) must be preserved exactly — it ensures mid-crash safety for chunk vectors.
- **Hook stdout contract**: every hook prints valid JSON with `"continue": true` on every exit path. The `log_hook_exception` + out-of-try-block print pattern must be maintained.
- **No lazy imports** — the `prek` lint rule is absolute. The db.py split uses module-level restructuring, not function-level lazy loading.
- **`PRAGMA foreign_keys = ON`** on every connection (production and test). Migration deletes in FK-safe order: `branch_messages` → `chunks` (cascade handles `chunk_vec`) → `branches`.
- **Do NOT remove `is_active = 1` filters** — the live DB has 3,295 pre-existing inactive rows. Filters stay permanently.
- **Do NOT touch `branches_fts`** — it's the live keyword search index. Only `messages_fts` is being dropped.
- **Do NOT modify `_ensure_vec_schema`'s self-heal checks** — they stay outside the version gate, running on every vec-loaded connection.
- Non-goals: new search capabilities, changes to JSONL parsing contract, changes to embedding model/dimensions, changes to plugin skill files (except removing `skills/ccr-tokens/`).

## Design Doc References

- `## Architecture → Deletion pass` — ordering and blast radius for each subsystem deletion
- `## Architecture → Branch identity fix` — session-keyed upsert_branch pattern with code example
- `## Architecture → Dead branch cleanup migration` — FK-safe delete order
- `## Architecture → Drop messages_fts` — what to remove vs preserve
- `## Architecture → db.py split` — exact functions/constants moving to config.py vs staying in db.py
- `## Architecture → Per-process logging` — process name mapping table
- `## Architecture → Schema versioning` — version check placement after SCHEMA_CORE
- `## Architecture → Connection management` — context manager pattern with code example
- `## Architecture → Search decomposition` — 5-module split table with function assignments
- `## Migration` — 4-step migration sequence (DML first, drop messages_fts, rebuild branches, set version)
- `## Test Strategy` — existing tests to adapt, new coverage, tests to remove
- `## Convention Examples` — hook error handling, PID-file guards, watermark protocol, test fixture, connection lifecycle

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
            return
        except PermissionError:
            return
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
