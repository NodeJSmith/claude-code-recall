---
task_id: "T02"
title: "Fix content hash and parameterize cap"
status: "planned"
depends_on: ["T01"]
implements: ["FR#1", "FR#2", "FR#3", "FR#4", "FR#13", "AC#1", "AC#2", "AC#8"]
---

## Summary

The core memory fix: change `content_hash` derivation to use raw text (preventing ping-pong), add a `max_tokens` parameter to `cap_for_embedding`, parameterize the attention budget in `embed_batch`, and thread `sync_path_token_limit` from settings through `sync_session` → `sync_branch` → `embed_branch_chunks`. After this task, the sync path caps at 4096 tokens with a ~4 GB worst-case peak, and backfill continues at 8192.

## Target Files

- modify: `src/ccrecall/embeddings.py` — add `max_tokens` param to `cap_for_embedding` (lines 268-317), add `max_token_cap` param to `embed_batch` (line 239) and `_plan_embed_batches` (line 209), compute budget as `max_token_cap²`
- modify: `src/ccrecall/embed_ops.py` — change `_prepare_exchange_data` (line 72) to accept `cap_limit` param, hash raw `combined` before capping, store `cap_tokens` in exchange_data dict, thread cap_limit through `embed_branch_chunks` (line 190) to `embed_batch`
- modify: `src/ccrecall/branch_ops.py` — add `sync_path_token_limit` param to `sync_branch` (line 192), thread to `embed_branch_chunks`
- modify: `src/ccrecall/session_ops.py` — add `settings` param to `sync_session` (line 40), read `sync_path_token_limit`, clamp to `[1, MODEL_TOKEN_LIMIT]`, thread to `sync_branch`
- modify: `src/ccrecall/hooks/sync_current.py` — pass `settings` to `sync_session` (line 199)
- modify: `src/ccrecall/hooks/backfill_embeddings.py` — pass `MODEL_TOKEN_LIMIT` as cap limit to `embed_branch_chunks` (line 341)
- modify: `src/ccrecall/_write_embedded_chunks` in `embed_ops.py` (line 118) — add `cap_tokens` to the INSERT statement
- modify: `tests/test_embeddings.py` — adapt `cap_for_embedding` tests for `max_tokens` param, add `_plan_embed_batches` test for dynamic budget
- create: `tests/test_embed_ops.py` — tests for raw-text hash, cap_tokens storage, cap parameterization
- modify: `tests/test_session_ops.py` — adapt `embed_branch_chunks` tests for new signature (lines 669+)
- read: `src/ccrecall/embed_ops.py` — full file, especially `_prepare_exchange_data` (72-99), `_write_embedded_chunks` (118-152), `embed_branch_chunks` (190-263)
- read: `src/ccrecall/embeddings.py` — `cap_for_embedding` (268-317), `embed_batch` (239-265), `_plan_embed_batches` (209-236)

## Prompt

Implement the content-hash fix, cap parameterization, and attention budget changes.

**Content hash fix (embed_ops.py:_prepare_exchange_data, line 72):** Add a `cap_limit: int` parameter (default `MODEL_TOKEN_LIMIT`). Change the hash derivation order: compute `content_hash = hashlib.sha256(combined.encode()).hexdigest()` BEFORE calling `cap_for_embedding`. Then call `text, was_capped = cap_for_embedding(combined, max_tokens=cap_limit)`. Add `"cap_tokens": cap_limit if was_capped else None` to the exchange_data dict. Also pass `max_tokens=cap_limit` to the per-turn `cap_for_embedding` calls for `user_text` and `assistant_text` display columns.

**cap_for_embedding parameterization (embeddings.py:268):** Add `max_tokens: int | None = None` parameter. Inside the function, use `limit = max_tokens if max_tokens is not None else MODEL_TOKEN_LIMIT`. Replace the two sites that reference `MODEL_TOKEN_LIMIT` (lines 292 and 309) with `limit`.

**Attention budget (embeddings.py):** Add `max_token_cap: int = MODEL_TOKEN_LIMIT` parameter to `_plan_embed_batches` (line 209). Replace `EMBED_BATCH_ATTENTION_BUDGET` usage (line 228) with `max_token_cap * max_token_cap`. Add the same parameter to `embed_batch` (line 239), threading it through to `_plan_embed_batches`.

**_write_embedded_chunks (embed_ops.py:118):** Add `cap_tokens` to the INSERT statement's column list and values. The value comes from `ed["cap_tokens"]` (which is `cap_limit if was_capped else None` from `_prepare_exchange_data`).

**embed_branch_chunks (embed_ops.py:190):** Add `cap_limit: int = MODEL_TOKEN_LIMIT` parameter. Thread to `_prepare_exchange_data(exchanges, cap_limit=cap_limit)` and to `embed_batch(texts, max_token_cap=cap_limit)`.

**sync_branch (branch_ops.py:192):** Add `sync_path_token_limit: int = MODEL_TOKEN_LIMIT` parameter. Pass to `embed_branch_chunks(cursor, branch_db_id, embed_msgs, is_active, vec_writable, cap_limit=sync_path_token_limit)`.

**sync_session (session_ops.py:40):** Add a `settings: dict | None = None` parameter. Read `sync_path_token_limit` from settings (with clamp: `min(max(val, 1), MODEL_TOKEN_LIMIT)`). Thread to `sync_branch`. Import `MODEL_TOKEN_LIMIT` from `embeddings` and `SYNC_PATH_TOKEN_LIMIT` from `embeddings` for the default.

**sync_current (hooks/sync_current.py):** Pass settings to `sync_session(...)`.

**backfill_embeddings (hooks/backfill_embeddings.py:341):** Pass `cap_limit=MODEL_TOKEN_LIMIT` to `embed_branch_chunks` explicitly (the default, but making it explicit for clarity).

## Focus

- `_prepare_exchange_data` returns a list of dicts. The `"cap_tokens"` key is new — `_write_embedded_chunks` reads it. Verify the key name matches exactly.
- `embed_branch_chunks` currently calls `embed_batch(texts)` at line 254. The new call is `embed_batch(texts, max_token_cap=cap_limit)`.
- `_plan_embed_batches` line 228: `next_area > EMBED_BATCH_ATTENTION_BUDGET` becomes `next_area > max_token_cap * max_token_cap`. The module constant stays defined (used nowhere else) but is no longer referenced in the function.
- `session_ops.sync_session` currently has no `settings` parameter. It's called from `sync_current.py:199` and `hooks/import_conversations.py`. The import path passes `embed=False` which means `sync_branch` is called but `embed_branch_chunks` is skipped (via `vec_writable=False` check), so the sync_path_token_limit is irrelevant there. Still, the new `settings` parameter should default to `None` and be handled gracefully.
- `tests/test_session_ops.py` has a `TestEmbedBranchChunks` class starting at line 669 with many tests calling `embed_branch_chunks` directly. These all need the updated signature.
- The `cap_for_embedding` fast path at line 292 checks BOTH char budget and token limit. The `max_tokens` parameter only affects the token check, not the char budget. This is a known design decision (see Edge Cases: char/token conflation).

## Verify

- [ ] FR#1: `_prepare_exchange_data` produces the same `content_hash` for the same raw text regardless of `cap_limit` value
- [ ] FR#2: `cap_for_embedding("long text", max_tokens=4096)` caps at 4096 tokens; without `max_tokens` it caps at 8192
- [ ] FR#3: `embed_branch_chunks` with `cap_limit=4096` produces exchange_data with `cap_tokens=4096` for truncated texts
- [ ] FR#4: `embed_branch_chunks` with default cap_limit produces exchange_data with `cap_tokens=8192` for truncated texts
- [ ] FR#13: `_plan_embed_batches` with `max_token_cap=4096` produces batches of at most 1 text when texts are near 4096 tokens
- [ ] AC#1: Two texts differing only past 4096 tokens produce different `content_hash` values; same raw text with different caps produces the same hash
- [ ] AC#2: `embed_branch_chunks` with `cap_limit=4096` caps at 4096; with `MODEL_TOKEN_LIMIT` caps at 8192
- [ ] AC#8: `_plan_embed_batches(max_token_cap=4096)` limits batch size to 1 for near-4096-token texts
