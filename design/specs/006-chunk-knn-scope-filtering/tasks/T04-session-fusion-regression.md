---
task_id: "T04"
title: "Prove session-search fusion behavior"
status: "planned"
depends_on: ["T01", "T02", "T03"]
implements: ["FR#8", "AC#8", "AC#11"]
---

## Summary
Add a Track A `search_sessions()` regression proving a scoped vector candidate beyond the initial KNN window can surface through the existing fusion, best-chunk-per-branch rollup, and per-session dedup path. This final task verifies the user-facing session-card path without changing ranking, output shape, keyword fallback, or branch/session collapse behavior.

## Target Files
- modify: `tests/test_search.py`
- read: `src/ccrecall/search_conversations.py`
- read: `src/ccrecall/search_vector.py`
- read: `src/ccrecall/search_hydrate.py`
- read: `src/ccrecall/fusion.py`
- read: `src/ccrecall/search_cli.py`

## Prompt
In `tests/test_search.py`, add an integration-style sqlite-vec regression for `search_sessions()` where the nearest global chunks are outside the requested scope, a farther scoped chunk exists, and the scoped session card is returned through the existing Track A path. Patch `model_available()` and `embed_text()` as current vector tests do, seed keyword rows so both FTS and vector branch IDs participate in RRF fusion, and assert the returned card keeps the existing session-card shape.

Do not change `search_sessions()` unless the test exposes a call-contract bug. Track A should continue to compute `chunk_top_k` with `OVERFETCH_MULTIPLIER * CHUNK_COLLAPSE_FACTOR`, call `get_vec_chunk_ids()`, fuse FTS and vector branch IDs with `rrf_scored()`, deduplicate by session, hydrate cards, and degrade to keyword search only through existing gates or sqlite3 errors.

Add or preserve a local regression proving keyword fallback behavior remains unchanged when the vector path is unavailable or degrades. Existing `TestDegradation` cases may satisfy this if they still pass after the implementation.

Run the targeted test suite and then the full suite. Fix any failures so the full suite passes before the task is complete.

## Focus
- `search_sessions()` vector fusion is at `src/ccrecall/search_conversations.py:83` through line 140.
- `get_vec_chunk_ids()` at `src/ccrecall/search_vector.py:75` performs best-chunk-per-branch rollup after raw chunk retry.
- `dedup_by_session()` and `hydrate_cards()` live in `src/ccrecall/search_hydrate.py`; output shape must remain session cards, not snippets.
- Existing duplicate-session and vector tests in `tests/test_search.py` show how to patch `model_available()` / `embed_text()` and seed chunks.

## Verify
- [ ] FR#8: `uv run pytest tests/test_search.py -q` passes with `search_sessions()` preserving keyword/vector RRF fusion, best-chunk-per-branch rollup, per-session deduplication, and keyword fallback behavior.
- [ ] AC#8: `uv run pytest tests/test_search.py -q` confirms a scoped vector result beyond the initial KNN window can surface through Track A fusion.
- [ ] AC#11: `uv run pytest` passes.
