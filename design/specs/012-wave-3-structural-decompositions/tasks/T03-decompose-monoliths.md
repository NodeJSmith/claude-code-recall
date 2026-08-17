---
task_id: "T03"
title: "Decompose embed_branch_chunks into named steps"
status: "done"
depends_on: ["T01"]
implements: ["FR#5", "FR#7"]
---

## Target Files

- modify: `src/ccrecall/embed_ops.py`

## Prompt

Decompose `embed_branch_chunks` in `src/ccrecall/embed_ops.py` (198 lines, the largest function in the core library) into named-step helpers within the same file. This is part of issue #138.

**Scope limitation**: The two backfill `run()` functions (`backfill_embeddings.py`, `backfill_tool_content.py`) are NOT unified into a shared runner. Despite structural similarity, they diverge in tested, load-bearing ways: abort vs skip-and-continue on stuck batches, different progress/ETA math, different SAVEPOINT handling. Forcing unification risks silently changing tested behavior. Only `embed_branch_chunks` is decomposed.

### Read before starting

Read `src/ccrecall/embed_ops.py` in full. Understand the 8-step watermark protocol documented in the function's docstring and inline comments. The imports at the top of the file (`hashlib`, `EMBEDDING_MODEL`, `EMBEDDING_VERSION`, `embed_batch`, `build_exchange_pairs`, `cap_for_embedding`, `write_chunk_embedding` — note: after T01, `write_chunk_embedding` comes from `ccrecall.db_vec`) are already in scope for the helpers.

### Extract these helpers (all within `embed_ops.py`)

1. **`_prepare_exchange_data(exchanges: list[dict]) -> list[dict]`** (~25 lines)
   Step 3: for each exchange, compute combined user+assistant text, cap it for embedding, hash it, cap user/assistant display text separately. Returns a list of dicts with keys: `index`, `text`, `was_capped`, `content_hash`, `timestamp`, `first_message_uuid`, `user_text`, `assistant_text`.

2. **`_diff_exchanges(exchange_data: list[dict], existing_chunks: dict[int, dict]) -> tuple[list[dict], set[int]]`** (~15 lines)
   Step 5: compare exchange_data against existing_chunks. Returns `(needing_embed, indices_to_prune)`. `needing_embed` = exchanges with no chunk row or changed content_hash. `indices_to_prune` = chunk indices not in current exchanges. Does NOT include version-stale chunks (those are backfill's job per design H6).

3. **`_write_embedded_chunks(cursor: sqlite3.Cursor, branch_db_id: int, needing_embed: list[dict], vecs: list) -> None`** (~20 lines)
   Step 6b: for each exchange+vector pair, DELETE old chunk by (branch_id, exchange_index), INSERT new chunk row (with embedding_version=0, embedding_model=NULL), call `write_chunk_embedding` to write the vector and stamp the version. Order invariant: vector FIRST, bookkeeping LAST.

4. **`_prune_stale_chunks(cursor: sqlite3.Cursor, branch_db_id: int, indices_to_prune: set[int]) -> None`** (~8 lines)
   Step 7: delete chunks whose exchange_index no longer exists. The `chunks_vec_ad` cascade trigger handles chunk_vec cleanup.

5. **`_should_stamp_watermark(exchange_data: list[dict], embedded_indices: set[int], existing_chunks: dict[int, dict]) -> bool`** (~15 lines)
   Step 8: check whether every exchange now has a current-version chunk with correct content_hash. Returns True if the watermark should be set.

### Resulting orchestrator shape

After extraction, `embed_branch_chunks` should be ~50-60 lines:
1. Guard: not active or not vec_writable → return 0
2. `exchanges = build_exchange_pairs(branch_msgs)` — if empty, stamp watermark, return 0
3. `exchange_data = _prepare_exchange_data(exchanges)`
4. Load existing chunks from DB
5. `needing_embed, indices_to_prune = _diff_exchanges(exchange_data, existing_chunks)`
6. Early return: nothing to do → idempotent watermark repair if all chunks current
7. Clear-first: if needing_embed, clear watermark to 0
8. Apply max_embeds cap
9. `vecs = embed_batch(texts)`
10. `_write_embedded_chunks(cursor, branch_db_id, needing_embed, vecs)`
11. `_prune_stale_chunks(cursor, branch_db_id, indices_to_prune)`
12. `if _should_stamp_watermark(...): _stamp_branch_watermark(cursor, branch_db_id)`
13. Return len(needing_embed)

### Critical invariants to preserve

- **Clear-first/set-last watermark protocol**: watermark cleared BEFORE embed loop (step 5a), set AFTER all succeed (step 8)
- **Vector-first/bookkeeping-last per chunk**: `write_chunk_embedding` writes vector before stamping version
- **Embed-before-write**: `embed_batch(texts)` called BEFORE any DB writes — a failed embed leaves existing chunks intact
- **The function signature and return value** (`int`: number of exchanges embedded) must not change
- **The `max_embeds` cap** bounds per-sync inference cost; backfill passes `None` (no cap)
- **Idempotent watermark repair**: when nothing needs embedding but all chunks are current, stamp the watermark

### Verification

After decomposition:
- `embed_branch_chunks` body under 80 lines
- Each extracted helper under 80 lines
- `uv run pytest -q` — all tests pass unchanged
- `uvx prek run --all-files` passes clean

## Verify

- [ ] FR#5: `embed_branch_chunks` body under 80 lines; each extracted helper under 80 lines
- [ ] FR#7: `uv run pytest -q` — all tests pass, count unchanged (1178)
- [ ] AC#7: `uvx prek run --all-files` passes clean
