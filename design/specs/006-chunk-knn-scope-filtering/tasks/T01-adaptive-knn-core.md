---
task_id: "T01"
title: "Implement adaptive KNN retry core"
status: "planned"
depends_on: []
implements: ["FR#1", "FR#6", "FR#9", "FR#10", "AC#5", "AC#9", "AC#10"]
---

## Summary
Replace the single-pass global KNN implementation in `execute_chunk_knn()` with adaptive retry logic. Keep the same return shape while making the KNN and relational filtering phases explicit and reusable. Add narrow count helpers and retry bounds so filtered searches can recover farther valid chunks without extra work on the common filled path.

## Target Files
- modify: `src/ccrecall/search_vector.py`
- modify: `tests/test_search.py`
- read: `src/ccrecall/search_query.py`
- read: `tests/conftest.py`

## Prompt
In `src/ccrecall/search_vector.py`, refactor `execute_chunk_knn()` so it serializes the query vector once, runs sqlite-vec KNN for the current `k`, filters/hydrates those chunk IDs through `chunks`, `branches`, `sessions`, and `projects`, and retries with a larger `k` when fewer than the target number of filtered results are found. Preserve the public return shape `list[tuple[int, int, float]]` as `(chunk_id, branch_id, distance)` and preserve KNN distance order after filtering.

Add `KNN_RETRY_MULTIPLIER = 4`. Start with the caller-provided `top_k`; on underfill, grow by that multiplier toward the total `chunk_vec` candidate ceiling, clamping the final retry exactly to the ceiling. Add private helpers for the total vector candidate count and eligible scoped current vector-backed chunk count. Count lazily: do the initial KNN/filter pass first, then count only if the initial filtered result underfills or retry may be needed. Cache each count once per `execute_chunk_knn()` call.

Add an optional `target_results: int | None = None` parameter to `execute_chunk_knn()`. When omitted, target the existing `top_k` behavior. Use the eligible scoped current count to short-circuit when no eligible current vector-backed chunks exist and to avoid targeting more filtered rows than can exist. Use the total `chunk_vec` count, not eligible count, as the retry ceiling.

Keep all `sqlite3.Error` handling narrow in `search_vector.py`: sqlite3 errors in serialization, KNN, count, or relational filtering return `[]`; non-DB exceptions must propagate. Update direct vector tests in `tests/test_search.py` to cover stale/currentness and error-boundary behavior for the new retry/count path.

## Focus
- `execute_chunk_knn()` currently lives at `src/ccrecall/search_vector.py:11` and performs KNN once, then filters once.
- `get_vec_chunk_ids()` at `src/ccrecall/search_vector.py:75` depends on `execute_chunk_knn()` and must keep returning `(branch_id, distance, chunk_id)` after best-chunk-per-branch rollup.
- `scope_filter_clause()` at `src/ccrecall/search_query.py:36` returns SQL plus params in project, session, path, before, after order; reuse it for the eligible-count and filter queries.
- Existing sqlite-vec tests use `make_vec_conn()` from `tests/conftest.py` and `upsert_chunk_vec()` / `sqlite_vec.serialize_float32()` in `tests/test_search.py`.
- Gap check clean: `search_cli.py`, `cli/commands.py`, and `tests/test_integration.py` call public search functions and do not need output/help updates for this core refactor.

## Verify
- [ ] FR#1: A direct `execute_chunk_knn()` or `get_vec_chunk_ids()` regression proves a valid current chunk beyond the initial KNN window is recovered after retry.
- [ ] FR#6: Stale-version, wrong-model, and inactive nearer chunks remain excluded while a farther current active chunk can still be returned.
- [ ] FR#9: sqlite3 errors from the new KNN, count, or relational filter path return empty vector results instead of raising.
- [ ] FR#10: A non-DB exception raised by the cursor or helper path still propagates instead of being caught.
- [ ] AC#5: `uv run pytest tests/test_search.py -q` includes stale/currentness coverage where stale or inactive nearer chunks do not block a farther current active chunk.
- [ ] AC#9: `uv run pytest tests/test_search.py -q` includes sqlite3 degradation and non-DB propagation coverage for the new retry/count path.
- [ ] AC#10: `uv run pytest tests/test_search.py -q` proves an unfiltered search that fills from the initial KNN window does not issue the new count queries.
