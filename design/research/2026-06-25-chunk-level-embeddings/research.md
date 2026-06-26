---
proposal: "Embed conversations at exchange/chunk granularity (multiple vectors per branch), aggregate chunk scores up to session ranking, and add direct message-level retrieval — replacing the single per-branch summary vector."
date: 2026-06-25
status: Draft
flexibility: Leaning
motivation: "Semantic search only embeds the per-branch context_summary (first-2 + last-6 exchanges). Middle-of-conversation content is never embedded, so semantic recall misses it."
constraints: "Preserve hook hot-path (direct console-script entry points, no eager fastembed import); no lazy imports; conversations DB is a public contract (don't lose synced history); no migration-DML ladder — embedding bumps trigger re-embed, not in-place migration."
non-goals: "Not changing the summary-generation feature itself (#32); not reworking the FTS keyword index; not changing transcript parsing/models."
depth: normal
---

# Research Brief: Chunk-Level Conversation Embeddings (Issue #31)

**Initiated by**: "Embed conversations at message/chunk granularity, aggregate to sessions (replaces single summary-vector search target)." The user is *leaning chunk-level* and wants the three deferred design decisions settled with explicit trade-offs, both entrypoints (A session-aggregation, B direct message retrieval) in scope.

## Context

### What prompted this
Semantic search embeds exactly one vector per branch, built from the markdown `context_summary` (first 2 + last 6 exchanges, or all if ≤ 8). The embed call site is `embed_branch` in `session_ops.py:371-392`:

```python
with contextlib.suppress(Exception):
    vec = embed_text(summary_md)            # summary_md = context_summary markdown
    write_branch_embedding(cursor, branch_db_id, vec, SUMMARY_VERSION)
```

For any session longer than 8 exchanges, everything between exchange 2 and exchange N−6 is **never embedded**. A user searching for a topic discussed in the middle of a long working session gets no vector hit; only the FTS keyword path can surface it (and only if the exact terms match). This is the recall gap #31 targets.

### Current state (verified against code)

**Embedding** (`embeddings.py`): single source of truth. `EMBEDDING_MODEL = "jinaai/jina-embeddings-v2-small-en"`, `EMBEDDING_VERSION = 2`, `EMBEDDING_DIM = 512` (lines 22-24). `embed_text(text) -> list[float]` returns a 512-dim L2-normalized vector; `embed_texts(texts)` loops one inference per text (not true batching) reusing the model. `DEFAULT_EMBED_THREADS = 1` on interactive write/query paths; backfill exposes `--threads`. The model supports **8192 tokens** (trained at 512, extrapolated via ALiBi) — so token limits do not bind for per-exchange chunks.

**Write path** (`session_ops.py`): `sync_branch` (395-441) → `write_branch_summary` → `embed_branch` (441). Embedding is **synchronous and only for the active leaf** (`if not (summary_md and is_active and vec_writable): return`). Steady-state cost today = **1 inference per active branch per sync**.

**Vec schema** (`db.py`): one virtual table, 1:1 with branches —
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS branch_vec USING vec0(branch_id INTEGER PRIMARY KEY, embedding float[512])
```
Cascade cleanup is a trigger (`db.py:200-204`):
```sql
CREATE TRIGGER branches_vec_ad AFTER DELETE ON branches
  BEGIN DELETE FROM branch_vec WHERE branch_id = OLD.id; END
```
Upsert is DELETE+INSERT because **vec0 rejects `INSERT OR REPLACE`** (`upsert_branch_vec`, `db.py:137-143`). `write_branch_embedding` (146-154) enforces an order invariant: vec upsert FIRST, version columns LAST. `_ensure_vec_schema` self-heals a dimension change by `DROP TABLE branch_vec` (db.py:195) — establishing precedent that **vectors are derived data, safe to drop and regenerate**.

**Search** (`search_conversations.py`): `_get_vec_branch_ids` (135-197) runs vec0 KNN — `SELECT branch_id, distance FROM branch_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance` — then SQL-filters candidates to current `embedding_version`/`embedding_model`. `_get_fts_branch_ids` runs BM25. `search_sessions` (292-363) fuses the two **rank** lists with Reciprocal Rank Fusion (`fusion.py`: `score += 1.0/(RRF_K + rank + 1)`, `RRF_K=60`), then `_dedup_by_session` keeps the single best branch per session, truncates to `max_results`, and `_hydrate_branches` returns **full session transcripts**. The result unit is a branch deduped to a session; there is no score, snippet, or per-message locator in the output.

**Output** (`formatting.py`): `format_markdown_session` (89-131) prints a session header then dumps every `**User:**`/`**Assistant:**` message in full under `### Conversation`. `format_json_sessions` (134-144) returns `{sessions, total_sessions, total_messages, +extra}`. No KWIC, no score, no chunk locator exists today.

**Chunk boundaries already exist**: `build_exchange_pairs(messages)` (`summarizer.py:124-164`) pairs each user turn with its following assistant turns into `{"user", "assistant", "timestamp", "index"}`, stripping `[Tool: X]` markers. The `index` is a stable per-branch ordinal. This is the natural chunk unit and it is already implemented.

**Backfill/heal** (`hooks/backfill_embeddings.py`): `build_selection` marks a branch eligible when `embedding_version < EMBEDDING_VERSION OR embedding_model != ... OR summary_version_at_embed != ... OR NOT EXISTS (SELECT 1 FROM branch_vec WHERE branch_id = branches.id)`. So **bumping `EMBEDDING_VERSION` re-embeds the whole corpus**. `CONTENT_ERROR_VERSION = -1` sentinels un-embeddable rows.

### Key constraints
- **Hook hot path** is about the entry-point *import* cost (~440ms direct vs ~1800ms via the cyclopts app), not embedding time — but chunk embedding multiplies *inference* count on the write path, a separate budget to protect.
- **No lazy imports** — all the fastembed machinery is already top-level in `embeddings.py`; no change needed.
- **DB is a public contract** — must not drop `messages`/`branches`/`branch_messages` (synced history). Vectors are derived and regenerable; dropping/replacing them is the sanctioned re-embed path.
- **No migration-DML ladder** — additive `CREATE ... IF NOT EXISTS` plus a version bump that triggers re-embed.

## Feasibility Analysis

**Verdict: Feasible. Medium effort, low-to-moderate risk.** The architecture is unusually well-suited: embedding is already a single source of truth, exchange segmentation already exists, the vec-table + cascade-trigger + version-gated-backfill pattern generalizes cleanly from one-row-per-branch to many-rows-per-branch, and there is precedent (`_ensure_vec_schema` drop/heal) for treating vectors as disposable derived data.

The single most important feasibility insight: **steady-state write cost need not grow.** On a normal Stop-hook sync the active branch usually gains one new exchange, so an incremental write path embeds **one new chunk** — the same one inference per sync as today. The N× cost is a one-time backfill, which already runs opt-in/off-hot-path with a `--threads` knob.

### What would need to change

| Area | Files affected | Effort | Risk |
|------|---------------|--------|------|
| Chunk + chunk-vec schema, triggers, drop old `branch_vec` | `db.py`, `schema.py` | Medium | Med — vec0 multi-row delete semantics |
| Chunk write helpers (upsert many, version order invariant) | `db.py`, `session_ops.py` | Medium | Med — write-path latency |
| Incremental chunk embed-on-write | `session_ops.py` (`embed_branch`) | Medium | Med — hot path |
| Per-exchange chunking from existing segmentation | reuse `summarizer.build_exchange_pairs` | Low | Low |
| Backfill eligibility → chunk existence | `hooks/backfill_embeddings.py` | Low | Low |
| KNN over chunks + best-chunk rollup into RRF | `search_conversations.py` | Medium | Med — fusion contract |
| Session cards + KWIC (entrypoint A) | `formatting.py` | Medium | Low |
| Chunk results + locators (entrypoint B) | `formatting.py`, `cli/commands.py` | Medium | Low |
| Version bump + re-embed | `embeddings.py` (`EMBEDDING_VERSION`) | Low | Low |
| Tests pinning search/format contracts | `tests/test_search.py`, format tests | Medium | Low |

### What already supports this
- `build_exchange_pairs` (`summarizer.py:124-164`) already produces clean, tool-stripped, indexed exchange units — the chunk boundary is free.
- `embed_texts` (`embeddings.py:115-125`) already batches sequential embeds reusing one model load.
- The `branch_vec` + `branches_vec_ad` trigger + `upsert_branch_vec` DELETE+INSERT pattern is a direct template for a finer-grained `chunk_vec` + cascade + bulk-replace.
- The backfill version-gate already re-embeds the whole corpus on an `EMBEDDING_VERSION` bump — the migration mechanism exists.
- `messages` table carries `uuid` and `timestamp`; `build_exchange_pairs` carries `index` — both are candidate stable locators for entrypoint B.
- `_ensure_vec_schema`'s drop-and-heal establishes that replacing the vector table without touching history is in-bounds.

### What works against this
- **vec0 cannot `INSERT OR REPLACE`** and deleting "all rows for a branch" is awkward when the PK is the chunk rowid — needs a side metadata table to know which rowids belong to a branch (see Decision 2).
- **RRF is rank-based, not score-based** — chunk cosine scores don't slot directly into the current fusion; you aggregate to a branch rank before fusing (see Decision 3) or rewrite fusion.
- **Overfetch math breaks**: today `top_k = max(max_results * OVERFETCH_MULTIPLIER, OVERFETCH_FLOOR)` assumes one vector ≈ one branch. With many chunks per branch, the top-k chunks may collapse to far fewer branches; `k` must grow.
- **Write-path latency**: if the Stop-hook-spawned sync embeds every chunk of a long active branch on each sync, latency multiplies. Mitigated only if the write path is incremental.
- **Output contract change**: the `/ccr-recall` skill consumes full transcripts today; session cards/KWIC change that contract.

## The Three Deferred Decisions

### Decision 1 — Chunk unit: per-message vs per-exchange vs windowed

**Options**
- **Per-message**: finest; ~2× the vector count of exchanges; embeds bare user turns ("yes, do that") that carry no standalone semantics and splits a Q from its A, hurting retrieval precision. No existing segmentation produces this cleanly for embedding.
- **Per-exchange** (user + following assistant turns): already produced by `build_exchange_pairs`; a self-contained Q&A is the natural retrieval unit; tool noise already stripped; `index` is a ready-made locator. Vector count = `exchange_count` per branch. Fits comfortably under the 8192-token model limit for all but pathological exchanges (huge pasted files / tool dumps).
- **Windowed (token-budgeted)**: sliding window over a tokenizer; needs the jina tokenizer wired in, blurs the locator (a window straddles exchanges), adds tuning params. Buys nothing here because the model already accepts 8192 tokens — windowing solves a problem we don't have.

**Recommendation: Per-exchange**, reusing `build_exchange_pairs`. Embed `f"{ex['user']}\n\n{ex['assistant']}"` per exchange. It is the only option with zero new segmentation code, a stable locator, and a semantically coherent unit. Guard the rare over-long exchange with a head+tail cap (not the middle-dropping `truncate_mid`, which would discard embedding signal) so a 30k-token pasted file doesn't degrade the vector. Note the model was *trained* at 512 tokens; most exchanges sit well under that, which is the quality sweet spot.

### Decision 2 — Vec-table layout for many vectors per branch

The current PK (`branch_id INTEGER PRIMARY KEY`) structurally forbids multiple rows per branch. Two shapes:

- **Option 2a — chunk_vec keyed by chunk rowid + a plain `chunks` metadata table (recommended).**
  - `chunks(id INTEGER PRIMARY KEY, branch_id INTEGER REFERENCES branches(id), exchange_index INTEGER, first_message_uuid TEXT, timestamp DATETIME, snippet TEXT)` — the source of truth for which chunk rowids belong to a branch, and the carrier of the entrypoint-B locator.
  - `chunk_vec USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[512])`.
  - **Re-sync replace**: `SELECT id FROM chunks WHERE branch_id=?` → `DELETE FROM chunk_vec WHERE chunk_id IN (...)` → `DELETE FROM chunks WHERE branch_id=?` → reinsert. (vec0 DELETE-by-rowid is supported; the side table is what makes "delete a branch's chunks" expressible.)
  - **Cascade**: two triggers — `branches → chunks` (AFTER DELETE ON branches) and `chunks → chunk_vec` (AFTER DELETE ON chunks DELETE FROM chunk_vec WHERE chunk_id = OLD.id). Mirrors the existing `branches_vec_ad` at one extra level.
  - **KNN**: `SELECT chunk_id, distance FROM chunk_vec WHERE embedding MATCH ? AND k = ?` then JOIN `chunks → branches → sessions` for version filter + branch rollup + locator. This reuses the existing "filter post-KNN via SQL join" pattern verbatim.

- **Option 2b — chunk_vec with a vec0 partition/auxiliary `branch_id` column.** Lets KNN filter/delete by `branch_id` without a side table. More efficient KNN filtering, but relies on newer vec0 partition-key semantics (DELETE-by-partition behavior needs confirmation) and *still* needs a metadata table for the entrypoint-B locator (exchange_index, message uuid, snippet). So it adds a vec0-feature dependency without removing the side table.

**Recommendation: 2a.** It is the conservative generalization of the existing design, keeps all filtering in ordinary SQL joins (the pattern already tested), and the `chunks` table does double duty as the locator store entrypoint B needs anyway. Treat 2b's partition key as a later optimization if post-KNN filtering becomes a measured bottleneck. Drop the old `branch_vec` in `_ensure_vec_schema` the same way the dimension self-heal does — vectors are derived, so no history is lost.

### Decision 3 — Session-score aggregation and fusion composition

Current fusion combines two **rank** lists via RRF, then dedups to session. Chunk-level KNN now yields a chunk-rank list that must roll up to a branch (then session) before it can fuse.

**Chunk → branch score options**
- **Max (best chunk)**: branch ranked by its single best-matching chunk. Directly serves the #31 goal — "did *any* part of this session match?" Simple, recall-friendly. Risk: one coincidental chunk can float a whole session.
- **Mean**: dilutes — a 40-exchange session with one perfect chunk scores low. Actively counter to the recall goal. Reject.
- **Top-k mean**: average the best k chunks; rewards sessions relevant in several places while still firing on a single strong hit. More robust than max, one param (k).
- **Log-sum-exp / softmax**: smooth max favoring multiple good chunks; more params, marginal gain over top-k mean here.

**Composition options**
- **Rank-rollup (recommended)**: in `_get_vec_branch_ids`, KNN over `chunk_vec`, map each chunk→branch, keep the **first (best-distance) occurrence per branch**, emit the resulting branch-ordered list. This is exactly "max" expressed as ranks, and it drops into the existing `rrf([fts_ids, vec_ids])` → `_dedup_by_session` pipeline **unchanged**. Smallest diff; preserves the tested RRF contract. Carry the winning `chunk_id` alongside so formatting can render its KWIC snippet.
- **Score-based fusion rewrite**: normalize cosine + BM25 and weight them so top-k-mean can be used. Bigger change, breaks the pinned RRF tests, harder to reason about. Defer.

**Recommendation: max via best-chunk rank-rollup, composed with the existing RRF unchanged.** Start with max because it most directly closes the recall gap and ships with the least disturbance to tested fusion. Revisit top-k-mean only if max proves noisy in practice. **Bump the overfetch**: set the chunk KNN `k` to roughly `max_results * OVERFETCH_MULTIPLIER * (expected chunks/branch)` (or a generous floor) so the post-rollup branch count still fills `max_results`. For **entrypoint B**, skip the branch rollup entirely — return the chunk rank list directly (optionally deduped per session) with locators.

## Migration / Re-import Strategy

1. **Additive schema** in `_ensure_vec_schema` / `SCHEMA_CORE`: create `chunks` + `chunk_vec` + the two cascade triggers with `IF NOT EXISTS`; `DROP TABLE branch_vec` (and its trigger) in the self-heal block, exactly as the dimension drift is healed today.
2. **Bump `EMBEDDING_VERSION` 2 → 3** in `embeddings.py`. Update `build_selection` in `backfill_embeddings.py` so the "vector exists" clause becomes `NOT EXISTS (SELECT 1 FROM chunks WHERE branch_id = branches.id)`. The existing `embedding_version < ?` clause then makes the entire corpus eligible.
3. **Backfill re-embeds at chunk granularity**; embed-on-write switches to chunk embedding for new syncs. Old summary vectors vanish with the dropped table — **no synced history is lost** because messages/branches/branch_messages are untouched and vectors are regenerable derived data (the public-contract guarantee is satisfied).
4. Keep `summary_version_at_embed` tracking if chunk text still derives from summary-era truncation; otherwise it can track the chunk-builder version. (Open question below.)

## Storage & Embedding-Time Cost

- **Vector count**: 1 per active branch → `exchange_count` per active branch. The summary compressed up to 8 exchanges into one vector; chunk-level produces ~E vectors for an E-exchange session. Order of magnitude: a 40-exchange session goes from 1 → ~40 vectors. At 512×4 bytes ≈ 2 KB/vector, that's ~2 KB → ~80 KB/session. Expect **~10–50× vector-count and vector-storage growth**, bounded by total exchange count across the corpus (FTS index and message text dominate DB size today, so absolute growth is modest).
- **Embed time, backfill (one-time)**: ~E inferences per branch instead of 1. With `DEFAULT_EMBED_THREADS=1` this is the cost center, but backfill is opt-in, off the hot path, and has `--threads`. `embed_texts` amortizes model load.
- **Embed time, steady-state write**: **the load-bearing number.** If `embed_branch` re-embeds every chunk of the active branch on each Stop sync, latency scales with branch length. The mitigation is an **incremental write path**: embed only chunks whose text changed (normally the newly-appended last exchange) by diffing `chunks.exchange_index`/text against the live branch. Done right, steady-state stays at ~1 inference/sync — unchanged from today. This should be a design requirement, not an optimization.
- **Hot path**: the CLAUDE.md hot-path rule is about hook *import* cost and is unaffected (no new top-level imports in the hook entry points). The new risk is inference latency in the spawned sync; the incremental write path neutralizes it. Verify whether the Stop hook blocks on the sync (see Open Questions).

## Output / Formatting

- **Entrypoint A (session cards + best-chunk KWIC)**: add a card formatter in `formatting.py` that renders, per session, the header (project | time | session[:8] | branch), a **relevance score**, and a **KWIC snippet from the best-matching exchange** (window of chars around query terms in `chunks.snippet`) — instead of dumping the full transcript. JSON gains `score` and `best_chunk: {exchange_index, snippet, timestamp}`. This changes the `/ccr-recall` contract (today it reads full transcripts), so decide whether cards replace or augment the full-transcript mode.
- **Entrypoint B (direct chunk retrieval)**: a flag/command path that returns the chunk rank list as compact rows — `{session_uuid, project, exchange_index, timestamp, role, snippet, score}`. The **locator** is `(session_uuid, exchange_index)` plus `first_message_uuid`, which `/ccr-resume` could later use to jump to the exact point. This is purely additive to formatting + `cli/commands.py`.

## Concerns

### Technical risks
- **vec0 bulk-delete-per-branch**: relying on the `chunks` side table to enumerate rowids is correct but must be kept perfectly in sync with `chunk_vec`; a drift leaves orphan vectors. The chunks→chunk_vec trigger plus the order invariant (vectors deleted before/with chunks) must be tested.
- **Overfetch under-fill**: if `k` isn't raised, chunk collapse can return fewer than `max_results` sessions. Needs an explicit test with a many-chunk branch.
- **Write-path latency** if incrementality is missed — the difference between "ships invisibly" and "adds seconds to every Stop."

### Complexity risks
- A new table, a new vec table, two new triggers, and a rollup step add moving parts to a subsystem whose whole design philosophy is "contain the coupling." Keep the chunk-building knowledge in `summarizer`/`parsing` and the vec mechanics in `db.py`; don't let chunk concepts leak into the hook entry points.
- Two retrieval shapes (A rollup, B raw) means two output contracts and two sets of tests.

### Maintenance risks
- Re-embed on bump now costs ~E× the inferences — a future model swap is a heavier backfill. Acceptable (off hot path) but worth noting.
- The `/ccr-recall` skill and any consumers of the full-transcript JSON must be migrated together if cards replace transcripts (per house rule: migrate callers, delete legacy path in the same wave).

## Open Questions

- [ ] **vec0 semantics**: confirm DELETE-by-rowid on `chunk_vec` and whether a partition/auxiliary `branch_id` column (Option 2b) supports efficient KNN pre-filtering and partition delete in the pinned sqlite-vec version. (Searched: jina token limit confirmed; sqlite-vec multi-vector/partition delete *not* yet confirmed against the vendored version.)
- [ ] **Does the Stop hook block on `embed_branch`?** Trace whether `sync_current`/import runs synchronously before the hook returns `{"continue": true}`, to size the write-latency budget. (Code read covered the embed call but not the hook's spawn/await model.)
- [ ] **Average exchanges/session distribution** — query a real `~/.ccrecall/` DB (`SELECT avg(exchange_count), max(exchange_count) FROM branches WHERE is_active=1`) to turn the 10–50× estimate into a real cost number before committing.
- [ ] **Over-long exchange handling** — head+tail cap vs hard truncate vs sub-splitting a single giant exchange. Product/quality call.
- [ ] **Skill contract**: do session cards *replace* the full-transcript output of `/ccr-recall`, or add a mode? Replacing is cleaner (no parallel paths) but changes how the lenses consume results.
- [ ] **`summary_version_at_embed`** — does chunk text still derive from summary-era truncation, or does it get its own `CHUNK_VERSION`? Affects backfill eligibility wiring.

## Recommendation

Proceed with chunk-level embedding — the architecture supports it and the recall gap is real and structural (everything between exchange 2 and N−6 is invisible to vectors today). Adopt **per-exchange chunks** (reusing `build_exchange_pairs`), a **`chunks` + `chunk_vec` two-table layout with cascade triggers** (Option 2a), and **best-chunk max rank-rollup composed with the existing RRF** for entrypoint A, with entrypoint B returning the raw chunk list plus `(session_uuid, exchange_index, message_uuid)` locators. This is *Inferred*-tier confidence on the design fit (grounded in the code's existing patterns) and *Supported* on feasibility (the embedding-as-single-source, version-gated-backfill, and vec-table+trigger patterns are all present and directly generalizable).

The one thing that must be settled before committing code is the **incremental write path** — embed only changed chunks so steady-state Stop-sync cost stays at ~1 inference. If that proves infeasible (e.g., the sync can't cheaply diff chunk text), reassess, because a per-sync N× inference cost would violate the spirit of the hot-path constraint. Get a real exchanges/session number off a live DB first; it converts the cost estimate from order-of-magnitude to concrete.

### Lightweight alternative (the "do less" option)

**Embed the full branch transcript as one larger vector** (jina accepts 8192 tokens). Pros: stays 1 vector/branch, no schema change, no fusion change — and it *does* cover the middle, so it strictly beats today. Cons: a single 512-dim vector averaging a 40-exchange session is semantically muddy — a specific middle topic's signal is diluted by everything else, so precision is weak; it cannot deliver entrypoint B (no per-chunk locator); and long sessions exceed the model's 512-token training length (quality degrades toward the 8192 ceiling). Use this only if effort must be minimal and entrypoint B is dropped. A middle variant — **multi-vector summaries** (embed the structured first/last/gap sections as 2–3 vectors) — still misses the true middle and is inferior to per-exchange for the stated goal.

### Suggested next steps
1. Run `/mine-define` to turn this into a design doc, pinning the six open questions (especially incremental write path and vec0 delete semantics) as decisions.
2. Probe a live `~/.ccrecall/` DB for `avg/max(exchange_count)` to size cost concretely.
3. Confirm sqlite-vec multi-vector delete/partition semantics against the vendored version (quick spike).
4. Phase the build as verifiable units: **Phase 0** schema + triggers + drop branch_vec (pin tests) → **Phase 1** incremental chunk embed-on-write + backfill (verify steady-state cost) → **Phase 2** chunk KNN + best-chunk rollup into RRF + session cards/KWIC (entrypoint A) → **Phase 3** direct chunk retrieval + locators (entrypoint B).

## Sources
- [jinaai/jina-embeddings-v2-small-en · Hugging Face](https://huggingface.co/jinaai/jina-embeddings-v2-small-en)
- [Jina Embeddings 2: 8192-Token General-Purpose Text Embeddings for Long Documents (arXiv)](https://arxiv.org/html/2310.19923v4)
