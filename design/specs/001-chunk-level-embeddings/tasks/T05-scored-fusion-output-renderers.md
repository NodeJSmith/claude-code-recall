---
task_id: "T05"
title: "Add score-returning fusion and contract card/snippet/envelope renderers"
status: "planned"
depends_on: []
implements: ["FR#11", "AC#10"]
---

## Summary

Build the pure output layer the absorbed contract requires, independent of search wiring: a
score-returning RRF sibling (`rrf_scored`) and the renderers for the Track A session card, the
Track B matched-exchange snippet, and the shared result envelope — including render-time min-max
score normalization (with the single-result→`null` edge case) and the `ranked: false` unranked
path. These are pure functions over dicts; the search tasks (T06/T07) call them, so the renderers
live in one place and the two search paths never conflict on `formatting.py`.

## Target Files

- modify: `src/ccrecall/fusion.py`
- modify: `src/ccrecall/formatting.py`
- modify: `tests/test_fusion.py`
- modify: `tests/test_formatting.py`
- read: `design/specs/001-chunk-level-embeddings/output-format-contract.md`
- read: `design/specs/001-chunk-level-embeddings/design.md`
- read: `design/specs/001-chunk-level-embeddings/tasks/context.md`

## Prompt

Implement per `output-format-contract.md` (the authoritative shape spec) and design.md
`## Architecture → (4) Search + output` (`rrf_scored`, render-time normalization, ranked signal).
This task adds renderers **alongside** the retained `format_markdown_session` /
`format_json_sessions` (do NOT remove or modify those — `recent_chats.py` still uses them).

1. **`fusion.py` — `rrf_scored(ranked_lists, k=RRF_K) -> list[tuple[int, float]]`**: a sibling of
   `rrf` returning `(id, score)` pairs in descending score order (the raw fused RRF value — the
   card's `score_raw`). `rrf` itself stays **unchanged** (ids-only, used by the dedup pipeline).
   Share the scoring loop if clean, but do not alter `rrf`'s signature or behavior.

2. **`formatting.py` — Track A card renderer.** Add a function that renders one session card from
   a card dict (the fields in `output-format-contract.md` → "Track A — session-summary card"):
   markdown form (score line, `project · git_branch · ended_date`, Topic, Status line with
   disposition + counts, Handle + `→ ccrecall tail {handle}`) and the JSON superset object
   (`score`, `score_raw`, `session_uuid`, `handle`, `project`, `git_branch`, `started_at`,
   `ended_at`, `topic`, `disposition`, `exchange_count`, `files_modified`, `commits`,
   `tool_counts`). The markdown reduces the three metadata lists to counts; JSON carries them in
   full (contract FR#10). **No exchange/message body text** in the card. A `--verbose` markdown card
   expands `files_modified`/`commits` lists + the `tool_counts` dict (JSON always carries the full
   lists regardless of verbose — design.md `## Documentation Updates`, challenge M16/M18).

3. **`formatting.py` — Track B snippet renderer.** Add a function that renders one matched-exchange
   snippet from a snippet dict (`output-format-contract.md` → "Track B — message-summary"):
   markdown (score line, `project/git_branch · handle · exchange {idx} · {time}`, `User:` /
   `Asst:` bounded turns, `→ ccrecall tail {handle}`) and the JSON object (`score`, `score_raw`,
   `session_uuid`, `handle`, `project`, `git_branch`, `exchange_index`, `matched_role`,
   `timestamp`, `user`, `assistant`, `match_terms`). On the vector path `matched_role` is `null`
   and `match_terms` is `[]` (no highlighting); the renderer must accept those as valid.

4. **`formatting.py` — shared envelope + score normalization.** Add an envelope builder producing
   `{query, ranked, count, results}` for either track. **Render-time min-max normalization:** the
   presented `score` is min-max normalized to `[0,1]` (two decimals) over the **bounded result set**
   passed in — NOT inside `rrf_scored`. **Single-result edge case (#31 amendment):** when the set
   has exactly one result, min-max is degenerate (`0/0`) → `score` is `null` (markdown omits the
   score line) while `score_raw` is still emitted. **Unranked path:** when `ranked=false`
   (LIKE-only), every `score`/`score_raw` is `null` and markdown prints the
   "(keyword fallback — unranked, ordered by recency)" marker.
   - Track B `score_raw` is `1.0 - distance` (higher-is-better) — but the renderer receives
     `score_raw` already computed by the caller; normalization here is the same min-max over the set.
   - Reuse `format_time`/`format_time_full` for timestamps (no stdlib `datetime`).

5. **Tests:**
   - `tests/test_fusion.py` — add `rrf_scored` coverage (returns `(id, score)` in descending order;
     handles empty/disjoint lists) alongside the existing ids-only `rrf` tests (which stay).
   - `tests/test_formatting.py` — card shape (markdown + JSON superset, no body text, flat size
     across session lengths, `--verbose` expansion), snippet shape (markdown + JSON, bounded turns,
     `matched_role:null`/`match_terms:[]` accepted), envelope (count/ranked), score normalization
     (multi-result min-max two decimals; **single-result → `score: null`, `score_raw` present**;
     unranked → all null + marker line). Map assertions to `output-format-contract.md` shapes
     (this is the renderer half of **AC#10**). Keep the existing
     `format_markdown_session`/`format_json_sessions` tests intact (those functions are retained).

## Focus

- `output-format-contract.md` is authoritative: the exact markdown templates are at its
  Architecture "Track A — session-summary card" and "Track B — message-summary"; the JSON objects
  and the field-provenance table are right below each. The score representation rules (normalized
  `score`, higher-is-better `score_raw`, single-result `null`, LIKE-fallback nulls) are at its
  "Score representation" section.
- `formatting.py` currently holds `format_markdown_session` (`:89-131`) and `format_json_sessions`
  (`:134-144`) — **retained**; `MAX_FILES_DISPLAYED` (`:9`) and `format_time*` (`:12-33`) are
  reusable for the card/snippet.
- `rrf` is at `fusion.py:7-17`; `RRF_K = 60`. Keep it ids-only.
- These renderers take dicts — they do NOT query the DB. Tests pass synthetic card/snippet dicts;
  this keeps T05 independent of the schema/search tasks (it can run in parallel with T01-T04).
- Immutability: build new dicts/strings; do not mutate the input dicts.

## Verify

- [ ] FR#11: The card, snippet, and envelope renderers emit exactly the fields and markdown/JSON
      forms defined in `output-format-contract.md` (no output fields beyond the contract), including
      the normalized `score` / higher-is-better `score_raw` and the `ranked:false` unranked shape.
- [ ] AC#10: Card and snippet JSON/markdown match `output-format-contract.md` field-for-field in
      `tests/test_formatting.py` (renderer half — search-wiring conformance is verified in T06/T07),
      including the single-result `score:null` and unranked-path cases.
