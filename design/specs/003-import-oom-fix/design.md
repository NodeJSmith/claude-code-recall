# Import OOM Fix

**Status:** draft
**Issue:** #48

## Problem

`ccrecall import` on a fresh database with ~5,000 JSONL files (1.3GB) grows to 5GB+ RSS and triggers the Linux OOM killer. Three root causes compound:

1. **Embedding during bulk import.** `import_conversations._run()` opens the DB with `load_vec=True`, so `sync_session` → `sync_branch` → `embed_branch_chunks` loads the fastembed model (~200MB ONNX) and runs inference per active branch. Meanwhile `memory_setup.py` also spawns `ccrecall-warm-model`, so two processes hold the model simultaneously.

2. **Double-parse per file.** `sync_session()` materializes the full file twice — `list(parse_all_with_uuids(filepath))` for branch detection and `list(parse_jsonl_file(filepath))` for message insertion. Both are full in-memory lists of dicts. A 6MB JSONL file expands to ~20-30MB of Python objects per parse; with both alive simultaneously, peak per-file memory is doubled.

3. **No GC between files.** Python's allocator (pymalloc + glibc malloc) doesn't return arena pages to the OS after freeing. Processing 5,000 files in a tight loop with no `gc.collect()` + `malloc_trim()` causes monotonic RSS growth even though most objects are short-lived.

## Architecture

### Mitigation 1: Skip embeddings during bulk import

The import path (`import_conversations._run`) currently passes `load_vec=True` to `get_db_connection`. Change it to `load_vec=False`. This means `chunk_vec_queryable(conn)` returns `False` inside `sync_session`, and `embed_branch_chunks` short-circuits at its first guard (`if not (is_active and vec_writable): return 0`).

The existing `ccrecall backfill embeddings` command already handles historical embedding. The comment in `memory_setup.py:186-188` documents that embedding backfill is intentionally not auto-spawned — it's opt-in via `ccrecall backfill embeddings`. This mitigation aligns with that design: bulk import populates the DB; embedding is a separate, bounded concern.

**Change:** `import_conversations._run()` line 246 — `load_vec=True` → `load_vec=False`.

One line.

### Mitigation 2: Single-pass file reader

`sync_session` reads each file twice:
- `parse_all_with_uuids(filepath)` → all entries with UUIDs (for branch detection + metadata)
- `parse_jsonl_file(filepath)` → user/assistant entries only (for message insertion)

These overlap: every entry yielded by `parse_jsonl_file` is a subset of what `parse_all_with_uuids` yields (user/assistant entries with UUIDs). The second parse exists only to filter to user/assistant types.

**Change:** Parse once via `parse_all_with_uuids`, then derive messages by filtering:

```python
all_entries = list(parse_all_with_uuids(filepath))
messages = [e for e in all_entries if e.get("type") in ("user", "assistant")]
```

This eliminates the second file read and second JSON decode pass. The `messages` list is a list of references to existing dicts (not copies), so it adds negligible memory.

Note: `parse_jsonl_file` also filters out `isMeta` entries without `origin`, but `insert_new_messages` → `build_message_row` already skips non-user/assistant types, so the filter is redundant in this context.

**Files:** `session_ops.py` lines 687-695.

### Mitigation 3: Batch with explicit GC

Process project directories in batches, calling `gc.collect()` between batches. On Linux, also call `ctypes.CDLL("libc.so.6").malloc_trim(0)` to release freed arena pages back to the OS.

**Where:** `import_conversations._run()`, in the `for project_dir in projects_dir.iterdir()` loop. After each project's `conn.commit()`, invoke GC. This is a natural batch boundary — each project directory may contain dozens to hundreds of JSONL files.

For the single-project case (`if project:` branch), no batching is needed — one project is already bounded.

```python
gc.collect()
if sys.platform == "linux":
    with contextlib.suppress(OSError):
        ctypes.CDLL("libc.so.6").malloc_trim(0)
```

**Files:** `import_conversations.py`.

### Not included

- **Memory budget / RSS monitor.** Over-engineering for this fix. The three mitigations above address the root causes; a safety valve is a separate concern if memory issues persist after these changes.
- **Surfacing the import to the user.** Already covered by existing logging (`logger.info`, `print` statements). Adding stderr progress bars or status files is a separate UX concern.
- **Suppressing warm-model during import.** The warm-model process is PID-guarded and short-lived (downloads the model once, then exits). It's not a significant contributor to the OOM — the issue is the import process itself holding the model in memory for the entire run.

## Alternatives Considered

**Streaming/generator approach instead of `list()`.** `find_all_branches` requires random access to the full entry list (builds parent-child UUID chains), so the first parse must materialize. The second parse is what we eliminate. A streaming single-pass that yields entries while also building the branch tree would be more complex for minimal additional gain over the filter approach.

**File-level batching with GC.** Processing N files then GC'ing would help, but project-level batching is the natural boundary (the `conn.commit()` already happens there) and is simpler. If per-project directories are themselves huge (hundreds of files), the per-project GC still bounds growth to one project's worth of transient objects at a time.

## Verification

1. Run `uv run pytest` — all tests pass.
2. On a machine with a large `~/.claude/projects/` directory, delete the DB and re-import. Monitor RSS — it should stay bounded rather than growing monotonically.
3. Confirm embeddings are not populated after import (expected — backfill handles them separately).
