---
task_id: "T03"
title: "Thread message-search retry target"
status: "planned"
depends_on: ["T01", "T02"]
implements: ["FR#7", "AC#7"]
---

## Summary
Update the `search_messages()` Track B caller to ask `execute_chunk_knn()` for enough filtered snippet hits, not just the original overfetch window. Add regressions that prove recovered snippets remain in nearest-distance order and are capped by `max_results`. Preserve the existing ranked semantics and no-keyword-fallback behavior.

## Target Files
- modify: `src/ccrecall/search_conversations.py`
- modify: `tests/test_search.py`
- read: `src/ccrecall/search_vector.py`
- read: `src/ccrecall/search_cli.py`
- read: `src/ccrecall/cli/commands.py`

## Prompt
In `src/ccrecall/search_conversations.py`, update the `search_messages()` call to `execute_chunk_knn()` so it passes `target_results=max_results` while keeping the existing `top_k = max(max_results * OVERFETCH_MULTIPLIER, OVERFETCH_FLOOR)` initial window. Keep the existing pattern of slicing `raw[:max_results]` before `hydrate_snippets()`.

Add or adapt `tests/test_search.py` coverage so Track B has out-of-scope nearer chunks, recovers farther in-scope chunks after retry, preserves nearest-distance order among the filtered results, and applies `max_results` after recovery. Do not add keyword fallback or change `ranked` behavior: pre-KNN gates still return `([], False)`, and a KNN run with no matches or DB degradation still returns `([], True)`.

Verify no output/help changes are needed by reading `src/ccrecall/search_cli.py` and `src/ccrecall/cli/commands.py`; do not edit them unless the tests reveal an actual call-contract mismatch.

## Focus
- `search_messages()` is at `src/ccrecall/search_conversations.py:172`; the current raw KNN call is at lines 209-211.
- `hydrate_snippets()` at `src/ccrecall/search_vector.py:108` preserves the order of `chunk_hits`, so the main risk is `execute_chunk_knn()` ordering and the caller's slice point.
- CLI message search in `src/ccrecall/search_cli.py:99` and `src/ccrecall/cli/commands.py:306` passes filters through to `search_messages()` and should not need help/output changes.
- Existing `TestSearchMessages` starts at `tests/test_search.py:1422`; add Track B filtered-retry tests near that class.

## Verify
- [ ] FR#7: `uv run pytest tests/test_search.py -q` passes with `search_messages()` preserving distance ordering and applying `max_results` after filtered KNN candidates are recovered.
- [ ] AC#7: `uv run pytest tests/test_search.py -q` confirms Track B ordering and max-results behavior after filtered retry.
