---
task_id: "T06"
title: "Add integration tests for no-ping-pong and backfill lifecycle"
status: "planned"
depends_on: ["T03", "T05"]
implements: ["AC#4", "AC#9"]
---

## Summary

End-to-end integration tests that prove the sync→backfill→sync lifecycle works correctly: no ping-pong between sync and backfill cap tiers, watermark withholding enables backfill to find and upgrade draft-quality chunks, and the full upgrade cycle converges. These tests exercise the assembled behavior across `embed_branch_chunks`, `_diff_exchanges`, `_should_stamp_watermark`, and `build_selection`.

## Target Files

- modify: `tests/test_integration.py` — add integration tests for no-ping-pong and backfill lifecycle
- read: `src/ccrecall/embed_ops.py` — `embed_branch_chunks`, `_diff_exchanges`, `_should_stamp_watermark`
- read: `src/ccrecall/hooks/backfill_query.py` — `build_selection`
- read: `src/ccrecall/embeddings.py` — `MODEL_TOKEN_LIMIT`, `SYNC_PATH_TOKEN_LIMIT`
- read: `tests/conftest.py` — `memory_db`, `make_vec_conn`, `vec_available_in_env` fixtures

## Prompt

Add two integration tests to `tests/test_integration.py`.

**Test 1: No-ping-pong (AC#4)**

Scenario: embed an exchange at sync-path cap (4096), run backfill to upgrade it (8192), then run sync-path embedding again — verify the chunk is NOT re-embedded on the third call.

Steps:
1. Set up a DB with a branch and messages containing one exchange >4096 tokens (but <8192 so backfill doesn't truncate — this tests the `cap_tokens = NULL` after backfill case).
2. Call `embed_branch_chunks(cursor, branch_id, msgs, is_active=True, vec_writable=True, cap_limit=SYNC_PATH_TOKEN_LIMIT)` — verify it embeds (returns 1), chunk row has `cap_tokens=4096`.
3. Call `embed_branch_chunks(cursor, branch_id, msgs, is_active=True, vec_writable=True, cap_limit=MODEL_TOKEN_LIMIT, max_embeds=None)` — verify it re-embeds (returns 1, cap_tokens is now NULL since text fits at 8192 without truncation).
4. Call `embed_branch_chunks(cursor, branch_id, msgs, is_active=True, vec_writable=True, cap_limit=SYNC_PATH_TOKEN_LIMIT)` — verify it does NOT re-embed (returns 0). The hash matches and `cap_tokens IS NULL` (full quality) >= `SYNC_PATH_TOKEN_LIMIT`.

**Test 2: Backfill lifecycle (AC#9)**

Scenario: sync embeds at 4096, watermark is withheld, build_selection finds the branch, backfill upgrades to 8192, watermark is stamped, build_selection no longer finds it.

Steps:
1. Set up a DB with a branch and one exchange >4096 tokens.
2. Call `embed_branch_chunks` at sync-path cap. Verify watermark is NOT stamped (check `branches.embedding_version` is 0). Verify `build_selection` returns SQL that would include this branch (run the query and check it appears).
3. Call `embed_branch_chunks` at backfill cap (MODEL_TOKEN_LIMIT, max_embeds=None). Verify watermark IS stamped. Verify `build_selection` query no longer includes this branch.

**Test infrastructure notes:**
- These tests need sqlite-vec loaded for `embed_branch_chunks` to write chunk_vec rows. Use `vec_available_in_env` and `pytest.mark.skipif` if vec is not available.
- Use `make_vec_conn` from conftest for a DB with vec loaded.
- Mock `embed_batch` to return dummy vectors (avoid loading the actual onnxruntime model in tests) — check existing tests in `test_session_ops.py:TestEmbedBranchChunks` (line 669) for the mocking pattern already used.
- The test messages need a `content` field long enough to exceed 4096 tokens when combined. Approximate: 4096 tokens ≈ 16,000 characters. Generate a synthetic long string.

## Focus

- `tests/test_integration.py` already exists with other integration tests. Add the new tests in a new class (e.g., `TestEmbeddingLifecycle`) at the end of the file.
- `tests/test_session_ops.py:TestEmbedBranchChunks` (line 669) already mocks `embed_batch` and calls `embed_branch_chunks` — follow the same fixture and mock setup pattern.
- After T02/T03, `embed_branch_chunks` has a new `cap_limit` parameter. The integration tests exercise this directly.
- The `build_selection` function returns `(where_clause, params)`. To test whether a branch is selected, execute `SELECT id FROM branches {where}` with the params and check whether the branch_id appears.
- Check whether `_should_stamp_watermark` is called internally by `embed_branch_chunks` — it is (line 260). The integration test verifies the outcome (watermark value in the DB) rather than mocking the internal.

## Verify

- [ ] AC#4: Embed at 4096 → backfill upgrades to 8192 → sync at 4096 again → chunk NOT re-embedded (returns 0)
- [ ] AC#9: Sync at 4096 → watermark NOT stamped, build_selection includes branch → backfill upgrades → watermark stamped, build_selection excludes branch
