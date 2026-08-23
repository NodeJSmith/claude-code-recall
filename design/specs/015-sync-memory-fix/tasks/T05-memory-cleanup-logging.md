---
task_id: "T05"
title: "Add memory cleanup and hot-path logging"
status: "planned"
depends_on: ["T02", "T03"]
implements: ["FR#9", "FR#10", "FR#11", "FR#12", "AC#6", "AC#7"]
---

## Summary

Memory baseline reduction and observability: free `all_entries` before the branch loop, add `reclaim_memory()` calls between `sync_branch` phases, add DEBUG logging to `embed_batch`, add path-aware WARNING logging to `embed_branch_chunks`, and add INFO logging with `file_size` to `sync_current.run()`.

## Target Files

- modify: `src/ccrecall/session_ops.py` — add `del all_entries` after line 125 (after `insert_new_messages` and `update_missing_tool_content`), before the branch loop (line 141)
- modify: `src/ccrecall/branch_ops.py` — import `try_load_libc` and `reclaim_memory` from `hooks.subprocess_utils`, call `reclaim_memory` between major phases in `sync_branch` (after `build_aggregated_content` at line 229, after `write_branch_summary` at line 235, before `embed_branch_chunks` at line 241)
- modify: `src/ccrecall/embeddings.py` — add `log.debug("embedding batch", extra={"batch_size": len(batch_texts), "longest_tokens": longest})` before each `model.embed()` call in `embed_batch` (around line 258)
- modify: `src/ccrecall/embed_ops.py` — add `log.warning("exchange exceeds cap", extra={"tokens": token_count, "cap": cap_limit})` in `embed_branch_chunks` when an exchange pre-cap token count exceeds `cap_limit`
- modify: `src/ccrecall/hooks/sync_current.py` — add timing and `log.info("sync complete", extra={"file_size": file_size, "duration_s": elapsed})` at the end of `run()`
- modify: `tests/test_session_ops.py` — verify `all_entries` is freed (AC#6)
- read: `src/ccrecall/hooks/subprocess_utils.py` — `try_load_libc` (25-31), `reclaim_memory` (34-43)
- read: `src/ccrecall/session_ops.py` — `sync_session` (40-147), especially lines 83-141
- read: `src/ccrecall/branch_ops.py` — `sync_branch` (192-250)
- read: `src/ccrecall/hooks/sync_current.py` — `run()` (113-227)

## Prompt

Implement memory cleanup and hot-path logging.

**del all_entries (session_ops.py):** After `update_missing_tool_content` (line 124) and `insert_new_messages` (line 125) complete, and before the `vec_writable` probe (line 139), add `del all_entries`. The `messages` list retains references to the user/assistant dicts, so the freed memory is primarily the list container and non-message entries (notifications, etc.). Add a comment noting this.

**reclaim_memory (branch_ops.py):** Import `try_load_libc, reclaim_memory` from `ccrecall.hooks.subprocess_utils`. In `sync_branch` (line 192), load libc once at function entry: `libc = try_load_libc()`. Add `reclaim_memory(libc)` calls at three points:
1. After `build_aggregated_content` + the UPDATE (after line 233)
2. After `write_branch_summary` (after line 235)
3. Before the `try: embed_msgs = ...` block (before line 239)

Note: the design acknowledges this pattern is slightly different from `backfill_embeddings.py` which loads libc once per `run()` — here it's once per `sync_branch` call, so it loads once per branch in a multi-branch session. Acceptable since `try_load_libc` is cheap.

**embed_batch logging (embeddings.py):** Add `import logging` and `log = logging.getLogger(__name__)` at the top if not already present. In `embed_batch` (line 239), before each `model.embed()` call (inside the batch loop starting around line 253), add:
```python
log.debug("embedding batch", extra={"batch_size": len(batch_texts), "longest_tokens": longest})
```
where `longest` is `token_counts[batch[0]]` (the first index in the batch, which is the longest due to sorting).

**embed_branch_chunks WARNING (embed_ops.py):** In `embed_branch_chunks`, after `exchange_data = _prepare_exchange_data(exchanges, cap_limit=cap_limit)` and before the `_diff_exchanges` call, iterate `exchange_data` and log a WARNING for any exchange whose pre-cap token count exceeds `cap_limit`. This requires knowing the pre-cap token count — use the model's `token_count` on the raw `combined` text. Alternatively, since `_prepare_exchange_data` now caps at `cap_limit` and sets `was_capped`, log a WARNING for any exchange where `was_capped=True` and include the cap_limit.

**sync_current INFO (hooks/sync_current.py):** In `run()`, capture start time before calling `sync_session` and compute elapsed. After the commit, add:
```python
log.info("sync complete", extra={"file_size": file_size, "duration_s": elapsed})
```
The `file_size` is available from `filepath.stat().st_size` (it's already read at line 185-186 for the import log). The sync_current module already has a logger.

## Focus

- `session_ops.py` line 100: `meta = extract_session_metadata(all_entries)` — this is the last use of `all_entries`. After line 125 (`insert_new_messages`), `all_entries` is genuinely unused.
- `branch_ops.py` does NOT currently import from `hooks.subprocess_utils`. Verify the import path works — `hooks.subprocess_utils` is a module in the hooks package, importable as `ccrecall.hooks.subprocess_utils`.
- `embeddings.py` might already have `import logging` — check. It definitely imports from `fastembed`, so adding a logger is safe.
- `sync_current.py` already has `log = logging.getLogger(__name__)` at the module level (check around line 20-30). Use the existing logger.
- The `file_size` in `sync_current.py` is computed at line 185-186 as part of the stat-based skip check. It's available as a local variable in `run()`.

## Verify

- [ ] FR#9: `all_entries` is deleted before the `sync_branch` loop in `sync_session` (verifiable by inspection or reference-count test)
- [ ] FR#10: `reclaim_memory()` is called between the three phases of `sync_branch` (verifiable by mocking/patching `reclaim_memory` and checking call count)
- [ ] FR#11: `embed_batch` emits DEBUG log with `batch_size` and `longest_tokens` before each `model.embed()` call; `embed_branch_chunks` emits WARNING when `was_capped=True`
- [ ] FR#12: `sync_current.run()` emits INFO log with `file_size` and `duration_s` after sync
- [ ] AC#6: `sync_session` does not hold a reference to the parsed JSONL list during `sync_branch`
- [ ] AC#7: After a sync, log output contains INFO with `file_size` and DEBUG with batch metrics
