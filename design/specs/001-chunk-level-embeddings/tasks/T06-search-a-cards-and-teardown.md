---
task_id: "T06"
title: "Wire Entrypoint A to chunk-KNN cards and tear down branch_vec"
status: "planned"
depends_on: ["T01", "T02", "T03", "T04", "T05"]
implements: ["FR#2", "FR#3", "FR#9", "FR#11", "FR#12", "FR#8"]
---

## Summary

Switch Entrypoint A (`ccrecall search`) from branch-summary KNN + transcript dump to chunk-KNN +
best-chunk rollup + scored session cards, and complete the migration by tearing down the obsolete
`branch_vec` table and its now-unused helpers. The rank/dedup/RRF algorithm is unchanged; what
changes is the unit ranked (chunk) and the terminal render (card, not dump). This task ends the
additive-first migration: every `branch_vec` reader/writer has migrated, so the table and helpers
are removed here.

## Target Files

- modify: `src/ccrecall/search_conversations.py`
- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/legacy.py`
- modify: `src/ccrecall/hooks/import_conversations.py`
- modify: `tests/test_search.py`
- modify: `tests/test_db.py`
- modify: `tests/test_legacy_migration.py`
- read: `src/ccrecall/fusion.py`
- read: `src/ccrecall/formatting.py`
- read: `design/specs/001-chunk-level-embeddings/output-format-contract.md`
- read: `design/specs/001-chunk-level-embeddings/design.md`
- read: `design/specs/001-chunk-level-embeddings/tasks/context.md`

## Prompt

Implement Entrypoint A per design.md `## Architecture → (4) Search + output` (Entrypoint A section)
and the `## Replacement Targets` (chunk-KNN replaces `_get_vec_branch_ids`; A's message hydration
removed). Then perform the `branch_vec` teardown per `## Migration` and `## Architecture → (1)
Schema` (unconditional drop).

1. **Chunk-KNN + best-chunk rollup** — replace `_get_vec_branch_ids` (branch-vec KNN) with a chunk
   path: `SELECT chunk_id, distance FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY
   distance`, then JOIN `chunks → branches → sessions → projects`, filter
   `chunks.embedding_version = EMBEDDING_VERSION AND chunks.embedding_model = EMBEDDING_MODEL`
   (chunk-grain version exclusion — **FR#9**) AND `branches.is_active = 1` plus the existing
   `--project`/`--session`/`--path` filters, and **keep the first (best-distance) chunk per branch**
   (preserves KNN order = the max rollup expressed as ranks). Return best `(branch_id, distance,
   chunk_id)`. Catch `sqlite3.Error` to degrade (never a bare except) — see context.md Convention
   Examples.

2. **Fusion + score** — feed the best-per-branch chunk ids into the **unchanged**
   `rrf([fts_ids, vec_ids])` → `_dedup_by_session` pipeline, AND call `rrf_scored` (T05) to carry the
   raw fused `score_raw`. Normalization to the presented `score` happens at **render time** over the
   final bounded result set (T05 envelope), not in fusion.

3. **Overfetch** — the chunk-KNN `k` for A is
   `max(max_results * OVERFETCH_MULTIPLIER * CHUNK_COLLAPSE_FACTOR, OVERFETCH_FLOOR)` (define
   `CHUNK_COLLAPSE_FACTOR`, start ~8) so post-rollup distinct sessions still fill `max_results`.
   When the post-rollup session count is `< max_results`, **emit a diagnostic log line** (pre-rollup
   chunk count, post-rollup session count, collapse ratio) — observability for chronic under-fill
   (challenge M13). The one-shot adaptive retry is a follow-up, not required here.

4. **Card hydration (no transcript)** — replace the `_hydrate_branches` → `format_markdown_session`
   terminal step with the T05 **card renderer**, reading `context_summary_json` (topic/disposition)
   + the branch row's `files_modified`/`commits`/`tool_counts` + the branch/session/project join
   columns + the fused score. **Do NOT call `fetch_branch_messages` on the A path.** Two required
   precise points (challenge C4, M19):
   - **Graceful degrade (contract FR#11):** when a branch has no `context_summary_json`, derive
     `topic` from the first user message via a **targeted single-row** query
     (`SELECT m.content FROM branch_messages bm JOIN messages m ON bm.message_id = m.id WHERE
     bm.branch_id = ? AND m.role = 'user' ORDER BY m.timestamp ASC LIMIT 1`) and counts from the
     branch row; disposition omitted; never crash. This `LIMIT 1` probe is NOT full hydration.
   - **`tool_counts` PRAGMA guard:** the card hydrator reads `tool_counts`, which is absent on
     pre-column DBs — apply the same `PRAGMA table_info(branches)` guard `recent_chats.py:37-41`
     uses (or a cached one-time introspection in `db.py`), or it raises `OperationalError`.

5. **Ranked signal to the renderer** (challenge M17) — `rrf_scored` is never called on the LIKE-only
   rung, so `search_sessions` returns a `(results, ranked: bool)` pair (a small wrapper); the
   renderer uses `ranked == False` to emit `score:null`/`score_raw:null` per result and
   `ranked:false` in the envelope. Wire the existing FTS5→FTS4→LIKE cascade's bottom rung to the
   unranked shape.

6. **`print_status`** — report **chunk coverage** (current-version chunks / total chunks) and
   branch-watermark coverage, not the old "embedded branches N/M". Use the chunk-grain counts; keep
   the vec-extension and model lines.

7. **`branch_vec` TEARDOWN** (now safe — all callers migrated in T03/T04 and this task):
   - In `db.py` `_ensure_vec_schema`: add an **explicit, unconditional**
     `DROP TRIGGER IF EXISTS branches_vec_ad; DROP TABLE IF EXISTS branch_vec;` — **NOT** routed
     through the dimension self-heal (which never fires at the unchanged `float[512]`, challenge H5).
     Keep the `chunk_vec` creation + watermark-reset-on-chunk_vec-drop from T01.
   - Delete `upsert_branch_vec` and `write_branch_embedding` from `db.py` (no callers remain — verify
     with grep). Rename/replace the `branch_vec_queryable` guard usages in `search_conversations.py`
     with `chunk_vec_queryable` (the A path now needs the chunk table queryable).
   - Remove the now-dead `_get_vec_branch_ids`/`_hydrate_branches` A-path message loading per
     `## Replacement Targets` (note `_hydrate_branches` is only on the A path; if no other caller
     remains, remove it; `format_markdown_session`/`format_json_sessions` stay in `formatting.py`).
   - **Docs in code:** fix `legacy.py`'s docstrings (`:18-22`, `:147-148`) that describe
     vector-neutralization via "the branch_vec dimension self-heal" — after teardown the mechanism
     is the `chunk_vec` repopulation + `EMBEDDING_VERSION` bump + backfill; update the prose to match.
     Fix the `import_conversations.py:173` comment that names `branch_vec` (it now never queries
     `chunk_vec`).

8. **Tests:**
   - `tests/test_search.py` — rewrite the vector path for chunk-KNN: mid-session recall (a query
     matching only a middle exchange surfaces a >8-exchange session — **AC#1**); one scored card per
     session, best-chunk fusion, no transcript in the list (**AC#2**, FR#12); chunk-grain staleness
     (mixed current/stale chunks → only current returned — **AC#8**, FR#9); per-session dedup;
     `--project`/`--session`/`--path` filters preserved; degrade-to-keyword when chunk_vec absent.
     This file currently imports `upsert_branch_vec` (`:10`) and seeds `branch_vec` directly — swap
     those to chunk seeding.
   - `tests/test_db.py` — adapt the `branch_vec` existence/trigger tests: post-`_ensure_vec_schema`
     `branch_vec` is **absent**, `chunk_vec` present; keep the chunk cascade tests from T01.
   - `tests/test_legacy_migration.py` — the migration test (`:39-138`) seeds a legacy DB with
     `branch_vec` and asserts its SQL after migration (`:138`). After teardown, post-migration
     `branch_vec` is dropped and `chunk_vec` exists with watermarks reset (`embedding_version = 0`).
     Adapt the assertions accordingly.
   - Add an `AC#8`/no-full-transcript guard: no A result list contains a full transcript.

## Focus

- `_get_vec_branch_ids` is at `search_conversations.py:135-197`; `_hydrate_branches` at `:228-289`;
  `search_sessions` at `:292-362`; `print_status` at `:376-414`; `OVERFETCH_*` at `:41-42`; the
  overfetch `top_k` at `:315`. The existing chunk-grain staleness exclusion to mirror is the
  current branch-grain `embedding_version`/`embedding_model` filter at `:173-176`.
- `_dedup_by_session` (`:200-225`) and `rrf` (fusion) are **unchanged** — the ranking algorithm is a
  behavioral invariant (design.md `## Impact → Behavioral Invariants`).
- `context_summary_json` carries `topic`/`disposition` and (under `metadata`) the counts — prefer
  the join columns as source of truth, treat the summary's copies as fallback (contract field-
  provenance table). `render_context_summary` is NOT replaced (different consumer).
- This is the last task that touches `branch_vec`; after it, `grep -rn branch_vec src/` should match
  only intentional history/comments — verify nothing live remains.
- Migration-window closes here: once chunk-KNN is live and the corpus is backfilled (T04), vector
  search is restored at chunk grain.

## Verify

- [ ] FR#2: A query matching any single exchange surfaces its session, including exchanges outside
      the old summary window (AC#1).
- [ ] FR#3: Entrypoint A ranks sessions by best-matching chunk (max rollup) composed with FTS via
      the unchanged RRF (AC#2).
- [ ] FR#9: Stale (previous-version / wrong-model) chunk vectors are excluded at the **chunk grain**
      in the query, so a partially re-embedded branch still returns its current chunks (AC#8).
- [ ] FR#11: A's card + envelope JSON/markdown conform to `output-format-contract.md` (no extra
      output fields), via the T05 renderers.
- [ ] FR#12: Entrypoint A emits one scored card per session (deduped to best-ranked branch) with no
      full transcript in the list (AC#2).
- [ ] FR#8: `branch_vec` is dropped losslessly (derived data) and `upsert_branch_vec`/
      `write_branch_embedding` removed; `messages`/`branches`/`branch_messages` are untouched;
      `test_legacy_migration.py` confirms migration preserves history while dropping `branch_vec`.
- [ ] AC#1: A query whose only match is a middle exchange of a >8-exchange session returns that
      session in Entrypoint A results.
- [ ] AC#2: Entrypoint A returns one scored card per session, ranked by best-chunk fusion, with no
      full transcript in the list.
- [ ] AC#8: A branch with mixed current/stale chunk vectors returns its current-version chunks and
      omits the stale ones.
