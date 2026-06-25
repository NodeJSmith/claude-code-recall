---
task_id: "T02"
title: "Bump embedding version, add token-aware cap, carry exchange uuid"
status: "planned"
depends_on: []
implements: ["FR#10"]
---

## Summary

Provide the embedding-layer primitives the chunk write/backfill paths need: bump
`EMBEDDING_VERSION` 2→3 so the whole corpus becomes eligible for re-embedding at chunk grain, add
a **token-aware head+tail cap** helper so over-long exchanges degrade one chunk's signal instead
of discarding the exchange or tripping the content-error sentinel, and extend `build_exchange_pairs`
to carry the first message's `uuid` for the Track B locator. Touches `embeddings.py` and
`summarizer.py` only — disjoint from T01, so the two foundation tasks are independent.

## Target Files

- modify: `src/ccrecall/embeddings.py`
- modify: `src/ccrecall/summarizer.py`
- modify: `tests/test_embeddings.py`
- modify: `tests/test_summarizer.py`
- modify: `tests/test_context_injection.py`
- read: `design/specs/001-chunk-level-embeddings/design.md`
- read: `design/specs/001-chunk-level-embeddings/tasks/context.md`

## Prompt

Implement per design.md `## Migration` (version bump) and `## Architecture → (2) Write path`
(step 3 — the token-aware cap), plus the `## Dependencies and Assumptions` note that
`build_exchange_pairs` is extended to carry the first message's uuid.

1. **`embeddings.py` — bump `EMBEDDING_VERSION` 2 → 3** (line 23). Update the inline comment to
   note the granularity change (per-branch summary → per-exchange chunk). Do NOT change
   `EMBEDDING_MODEL` or `EMBEDDING_DIM`.

2. **`embeddings.py` — add `cap_for_embedding(text: str) -> tuple[str, bool]`** (returns the
   capped text and a `was_capped` flag). It lives here because it is about the model's token limit
   and the fastembed tokenizer is reachable via the already-loaded model. Behavior (design.md
   `## Architecture → (2) Write path` step 3, and `## Edge Cases` "Dense content"):
   - Head+tail-cap to a character budget (define a module constant, e.g. an `EMBED_CHAR_BUDGET`),
     keeping the head and the tail and dropping the middle — **never** a plain middle-dropping
     truncation that loses the tail.
   - Then **verify token count**: tokenize the capped text via the fastembed model's tokenizer and,
     while `len(tokens) > MODEL_TOKEN_LIMIT` (define `MODEL_TOKEN_LIMIT = 8192`), tighten the cap
     and re-check, so dense content (minified JSON / base64) under the char budget but over the
     token limit cannot reach `embed_text` and trip `CONTENT_ERROR`.
   - If the text is already within both budgets, return it unchanged with `was_capped=False`.
   - Worst case (a single exchange that cannot fit even fully capped) may still raise downstream —
     that is the genuine pathological `CONTENT_ERROR`, not a routine density miss.
   - Reach the tokenizer through `get_model()` (already the singleton accessor). Keep this the only
     embedding code path — do not construct a second model/tokenizer.

3. **`summarizer.py` — `build_exchange_pairs`** (`summarizer.py:124-164`): also carry
   `first_message_uuid` on each exchange dict — the `uuid` of the exchange's **first (user)**
   message. Read it with `m.get("uuid")` so messages that lack a uuid (e.g. context-injection
   callers, older fixtures) yield `first_message_uuid = None` without error. The exchange unit
   itself (user/assistant/timestamp/index) is unchanged.

4. **Tests:**
   - `tests/test_embeddings.py` — cover `cap_for_embedding`: short text passes through unchanged
     (`was_capped=False`); an over-budget text is head+tail-capped (head and tail both present,
     middle dropped) and returns `was_capped=True`; a dense over-token-but-under-char text is
     tightened until it fits the token limit (this is **AC#9**'s cap mechanism — the capped form
     still produces a usable 512-dim vector). Mock only the true external boundary (the model /
     tokenizer) per the in-memory-fixture convention.
   - `tests/test_summarizer.py` — `build_exchange_pairs` now returns `first_message_uuid`; add a
     case with uuids present (correct value carried) and confirm existing assertions
     (`user`/`assistant`/`index`) still hold, plus a case where messages have no `uuid` →
     `first_message_uuid is None`.
   - `tests/test_context_injection.py` — this file calls `build_exchange_pairs` (line ~691) on the
     untouched context-injection path; confirm it still passes with the additive key (adjust only
     if an assertion does exact-dict equality).

## Focus

- `EMBEDDING_VERSION` is imported by `db.py`, `search_conversations.py`, `backfill_embeddings.py`,
  `legacy.py`, and several tests — all reference the **constant**, so bumping it is transparent;
  tests seed `embedding_version` from the constant (confirmed: `tests/test_search.py:478`,
  `:613` uses `EMBEDDING_VERSION - 1`). No callers hardcode `2`.
- **Migration-window note (runtime, not test):** once the constant reads 3, real `ccrecall search`
  against an existing DB returns no *branch-level* vector hits (the old `branch_vec` is at v2) until
  the search path switches to chunk-KNN in T06 — this is the design's accepted migration-window
  degradation (`## Architecture → (2) Write path`). It does not break tests, which seed at the
  constant.
- `embed_one`/`embed_text` (`embeddings.py:95-112`) already L2-normalize via `normalize`; the cap
  is upstream of `embed_text` and does not touch normalization.
- Keep `from __future__ import annotations` out; use `X | None`; no lazy imports (project checks
  enforce all three).

## Verify

- [ ] FR#10: `cap_for_embedding` produces a **head+tail-capped** form (never middle-dropping that
      loses the tail) and tightens until `len(tokens) <= MODEL_TOKEN_LIMIT`, so an over-long or
      dense exchange yields a usable vector rather than discarding the exchange — covered by
      `tests/test_embeddings.py` (this is the AC#9 cap mechanism).
- [ ] AC#9: An exchange longer than the cap is embedded from a head+tail form and still produces a
      usable 512-dim vector (the capped text round-trips through the model), verified in
      `tests/test_embeddings.py`.
