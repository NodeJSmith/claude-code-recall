# Design: Chunk-Level Conversation Embeddings (Issue #31)

**Date:** 2026-06-25
**Status:** approved
**Scope-mode:** hold
**Research:** design/research/2026-06-25-chunk-level-embeddings/research.md
**Output contract:** design/specs/001-chunk-level-embeddings/output-format-contract.md (absorbed from draft PR #33)

## Problem

`ccrecall`'s semantic search embeds exactly **one vector per branch**, built from the
markdown `context_summary` (first 2 + last 6 exchanges, or all if ≤ 8). The embed call
site is `embed_branch` (`session_ops.py:371-392`):

```python
with contextlib.suppress(Exception):
    vec = embed_text(summary_md)            # summary_md = context_summary markdown
    write_branch_embedding(cursor, branch_db_id, vec, SUMMARY_VERSION)
```

For any session longer than 8 exchanges, **everything between exchange 2 and exchange
N−6 is never embedded**. A user searching for a topic discussed in the middle of a long
working session gets no vector hit; only the FTS keyword path can surface it, and only on
an exact term match. The recall gap is real and structural — it scales with session
length, and long working sessions are exactly where recall matters most.

Two retrieval jobs are also conflated under one output today (the full-transcript dump):
**A — session discovery** ("find me *sessions* like X") and **B — message retrieval**
("find me the specific *exchanges* about X"). The output side of that split is already
designed in the absorbed contract (`output-format-contract.md`, ex-PR #33); this design
builds the **retrieval substrate** that feeds it and lands both result shapes end-to-end.

## Goals

- Replace the single per-branch summary vector with **per-exchange chunk vectors**, so
  every exchange in a session is independently searchable (closes the mid-session gap).
- **Entrypoint A (session discovery):** aggregate chunk similarity up to a per-session
  ranking and emit the absorbed contract's **scored session card**.
- **Entrypoint B (message retrieval):** return matched **exchanges** directly, with a
  `(handle, exchange_index, timestamp)` locator and a bounded excerpt — the absorbed
  contract's **message-summary snippet**, semantically ranked.
- Keep steady-state Stop-sync embedding cost at **~1 inference per sync** via an
  incremental write path (embed only new/changed exchanges).
- Re-embed the existing corpus at chunk granularity via an `EMBEDDING_VERSION` bump,
  losing **no synced history** (vectors are regenerable derived data).
- Emit the absorbed output-format contract (`output-format-contract.md`) verbatim: scored
  cards (A), matched-exchange snippets (B), the shared `ranked`/`score`/`score_raw`
  envelope, and the score-returning fusion it requires.

## Non-Goals

- **Re-designing the output format.** `output-format-contract.md` is authoritative for
  result shapes, the score envelope, and the markdown/JSON parity. This design implements
  it; it does not re-litigate it.
- **Richer session summaries (#32).** Track A cards read `topic`/`disposition` from the
  existing `context_summary_json`; #32 enriches that later. The card's graceful-degrade
  path (contract FR#11) covers branches lacking it. Not in scope here.
- **Position-anchored `tail` / `--context N`.** The B locator carries `exchange_index` to
  support it later; teaching `tail` to open *at* a position is follow-up work.
- **Keyword-path Track B (FTS `snippet()` rendering).** The absorbed contract notes B's
  *shape* is identical whether ranked by keyword or vector. This design lands B's
  **semantic** ranking (the chunk-KNN path); the keyword-only B rung reuses the same
  renderer but is a thin follow-on, explicitly deferred under hold scope.
- **Changing the embedding model or dimension.** Same model (`jina-v2-small`, 512-dim);
  only the *granularity* of what gets embedded changes.

## User Scenarios

### Recall agent: AI consumer synthesizing an answer
- **Goal:** answer "what did we decide about X" from past sessions, inside a bounded
  context window.
- **Context:** invoked via the `/ccr-recall` skill over the local `~/.ccrecall` DB.

#### A — locate the right session(s), including by a mid-session topic
1. **Runs session discovery for a topic discussed in the middle of a long session.**
   - Sees: a ranked list of compact scored cards — the long session now appears because a
     *middle* exchange matched, not just its summary.
   - Decides: which one or two sessions to open, from score + topic + disposition.
   - Then: `ccrecall tail <handle>` for the chosen session — the full-fetch drill-in.

#### B — pull the specific exchanges
1. **Runs message retrieval for a phrase.**
   - Sees: a ranked list of matched exchanges — score, `(handle, exchange_index, time)`
     locator, the user/assistant pair with a bounded excerpt.
   - Decides: whether the snippet already answers the question.
   - Then: optionally `ccrecall tail <handle>` to widen.

### Human operator: terminal user
- **Goal:** find a past session by a topic buried mid-conversation.
- **Context:** runs `ccrecall search` directly in a terminal.

#### Scan and drill
1. **Reads the result list.**
   - Sees: scored cards that fit on screen, no transcript wall.
   - Decides: which handle to `tail`.

## Functional Requirements

- **FR#1** Each exchange of an active-leaf branch (the user turn plus its following
  assistant turns, as produced by `build_exchange_pairs`) is embedded as its own vector.
- **FR#2** Semantic search matches against exchange vectors, so a query matching any
  single exchange of a session can surface that session — including exchanges outside the
  summary window.
- **FR#3** Entrypoint A ranks sessions by their **best-matching** exchange (max rollup),
  composed with the existing FTS ranking via the current Reciprocal Rank Fusion.
- **FR#4** Entrypoint B returns matched exchanges directly (not rolled up to session),
  semantically ranked by chunk distance.
- **FR#5** Steady-state Stop-sync embedding cost is bounded by the number of **new or
  changed** exchanges since the last sync — normally one — not the branch's total length.
- **FR#6** Bumping the embedding version makes the entire corpus eligible for re-embedding
  at chunk granularity; the opt-in backfill re-embeds it off the interactive path.
- **FR#7** A removed branch (rewind, session deletion) removes its chunks and their
  vectors; no orphaned chunk or vector survives.
- **FR#8** Re-embedding or changing the embedding model never deletes or alters any row in
  `messages`, `branches`, or `branch_messages` — only derived vectors/chunks are dropped
  and regenerated.
- **FR#9** Stale (previous-version or wrong-model) chunk vectors are excluded from query
  results at the **chunk grain**, so a partially re-embedded branch still returns its
  already-current chunks rather than being excluded wholesale.
- **FR#10** An exchange whose text exceeds the embedding model's effective limit is
  embedded from a **head+tail-capped** form (never a middle-dropping truncation), so a
  single large pasted file degrades that one chunk's signal rather than discarding the
  exchange.
- **FR#11** All output shapes (cards, snippets, envelope, score fields) conform to
  `output-format-contract.md` — this design adds no output fields of its own beyond what
  that contract defines.
- **FR#12** Entrypoint A emits one scored session card per session (deduped to the
  best-ranked branch), with no full transcript in the result list.
- **FR#13** Each entrypoint B result carries a `(handle, exchange_index, timestamp)`
  locator and a per-message-bounded excerpt (separate user and assistant fields).
- **FR#14** The write path embeds only new or content-changed exchanges and is bounded per
  sync; version-stale chunks are re-embedded by the background backfill, not on the write path.
- **FR#15** The backfill re-selects any branch that has a chunk row without a current vector
  (crash victims, post-vector-drop orphans), independent of the branch watermark.
- **FR#16** Concurrent `sync-current` invocations are serialized: a second invocation while one
  is running skips rather than running a parallel embed.
- **FR#17** Entrypoint B returns a well-formed empty `ranked:false` result (never an error)
  when the vector index is unavailable.

## Edge Cases

- **Branch with one exchange:** one chunk, one vector; A and B both work normally.
- **Empty assistant turn** (user message with no assistant reply yet — the active leaf
  mid-turn): the exchange still embeds from the user text alone; `build_exchange_pairs`
  already tolerates a missing pair.
- **Rewind creating a new active leaf:** the old leaf becomes inactive; its chunks remain
  but are excluded from query (query filters `branches.is_active = 1`, unchanged). The new
  leaf's chunks are embedded on its next sync.
- **Over-long exchange (huge pasted file / tool dump):** token-aware head+tail cap before
  embedding (FR#10); display `user_text`/`assistant_text` use the same head+tail logic so the
  shown excerpt aligns with the embedded region.
- **Dense content (base64/minified) under the char budget but over the token limit:** the
  cap's post-check tightens until it fits the 8192-token model limit, so it does not trip the
  `CONTENT_ERROR` sentinel on routine density (challenge Finding M12).
- **Suppressed write-path content error (chunks row committed, vector missing):** an embed
  error inside the loop (after step 6 inserts the `chunks` row to allocate its rowid, before the
  `chunk_vec` write) is swallowed by `sync_branch`'s `contextlib.suppress(Exception)`, and the
  sync still commits the orphan `chunks` row. The backfill's chunk-grain heal clause
  (`chunks` row with no `chunk_vec`) re-selects this branch (challenge C1).
- **Process crash mid-embed (SIGKILL / OOM):** a `MemoryError` is `BaseException`, so it is
  **not** caught by `suppress(Exception)` — like SIGKILL, the sync dies before its single commit
  and the WAL rolls back the *entire* transaction (cleared watermark **and** the new `chunks`
  row). State: watermark reverts to its prior value, no orphan row exists. The branch self-heals
  on its **next Stop sync** (the diff finds the missing exchange and embeds it). **Residual gap
  (named, accepted under hold scope):** if such a session is *never resumed*, its final exchange
  stays unembedded — invisible to both backfill predicates. Narrow and bounded; closing it would
  require a periodic full re-scan, deferred.
- **Partial re-embed interrupted** (backfill killed mid-branch): chunk-grain version
  filtering (FR#9) means already-embedded chunks still serve; the rest stay eligible.
- **vec0 unavailable** (extension won't load): no chunk vectors are created. **Track A** search
  degrades to the keyword path exactly as today (`branch_vec_queryable` guard generalizes to
  `chunk_vec_queryable`). **Track B** (`search-messages`) returns an empty `ranked:false`
  envelope — it has no keyword fallback in this landing (deferred, issue #34).
- **Many chunks collapsing to few branches in A:** the KNN `top_k` must overfetch enough
  chunks that, after best-chunk-per-branch rollup, at least `max_results` distinct
  sessions remain (see Architecture → Overfetch).
- **Content unchanged but re-synced:** the incremental write path detects no content-hash
  change and embeds nothing (FR#5).

## Acceptance Criteria

- **AC#1** A query whose only match is an exchange in the *middle* of a >8-exchange
  session returns that session in entrypoint A results (it does not today). (FR#1, FR#2)
- **AC#2** Entrypoint A returns one scored card per session, ranked by best-chunk fusion,
  with no full transcript in the list. (FR#3, FR#12, FR#11)
- **AC#3** Entrypoint B returns matched exchanges with a `(handle, exchange_index,
  timestamp)` locator and a bounded excerpt, ordered by chunk distance. (FR#4, FR#13, FR#11)
- **AC#4** After appending one exchange to an already-embedded branch and re-syncing,
  exactly one new chunk vector is written and no existing chunk is re-embedded. (FR#5)
- **AC#5** Bumping `EMBEDDING_VERSION` marks all active-leaf branches eligible; after
  backfill, every active-leaf exchange has a current-version chunk vector. (FR#6)
- **AC#6** Deleting a branch row deletes all its `chunks` rows and all their `chunk_vec`
  rows (verified by count). (FR#7)
- **AC#7** Re-embedding at the new version leaves `messages`, `branches`, and
  `branch_messages` row counts and contents unchanged. (FR#8)
- **AC#8** A branch with some current and some stale chunk vectors returns its
  current-version chunks from search and omits the stale ones. (FR#9)
- **AC#9** An exchange longer than the cap is embedded from a head+tail form and still
  produces a usable vector (search returns it for a query matching its head or tail).
  (FR#10)
- **AC#10** Card, snippet, and envelope JSON/markdown match `output-format-contract.md`'s
  shapes field-for-field. (FR#11)
- **AC#11** After an `EMBEDDING_VERSION` bump, the next sync of a long existing session embeds
  at most `MAX_WRITE_PATH_EMBEDS_PER_SYNC` chunks (not all N); backfill upgrades the rest.
  (FR#14)
- **AC#12** A branch with a `chunks` row whose `chunk_vec` row is missing (simulated crash) is
  re-selected by backfill even when its watermark reads `EMBEDDING_VERSION`. (FR#15)
- **AC#13** A second `sync-current` started while one is running exits without embedding; the
  first completes normally. (FR#16)
- **AC#14** With the vector index unavailable, `search-messages` exits 0 with an empty
  `ranked:false` envelope. (FR#17)
- **AC#15** A branch whose summary failed (`context_summary = NULL`) still gets its exchanges
  chunk-embedded and is searchable. (FR#1, via the widened chunk universe)

## Key Constraints

- **One embedding code path.** All vectors — chunk write, query, backfill — must go
  through `embeddings.py` (`embed_text`/`embed_texts`). No second embedding path may exist
  (existing invariant; chunk-level must not fork it).
- **vec0 rejects `INSERT OR REPLACE`.** Confirmed by spike against `sqlite-vec 0.1.9`:
  chunk upsert is DELETE-then-INSERT, like the current `upsert_branch_vec`.
- **Order invariant on write.** Vector upsert FIRST, version/bookkeeping columns LAST — so
  a swallowed embed failure leaves the chunk eligible for backfill rather than marked-done
  with no vector. (Generalizes `write_branch_embedding`'s existing invariant.)
- **Embedding stays off the hot path but is a background-citizen cost.** The Stop hook
  (`memory_sync.py`) spawns a **detached** `ccrecall sync-current` process and returns
  `{"continue": true}` immediately — embedding never blocks session-stop. But the detached
  process runs on the user's machine (incl. the resource-constrained laptop/VPS), so
  embedding work per sync must stay bounded. The write-path diff (only new/content-changed
  exchanges, version-stale left to backfill) plus the `MAX_WRITE_PATH_EMBEDS_PER_SYNC` cap
  (write-path step 5) keep the steady state at **~1 inference/sync and the worst case bounded**
  — even right after the `EMBEDDING_VERSION` bump, on a rewind, or on a long imported session's
  first sync (challenge Finding H6). **This is a requirement, not an optimization.**
- **`sync-current` needs a concurrency guard** (challenge Finding C2). `memory_sync.py:39`
  spawns a detached `sync-current` on **every** Stop, with no PID/lock guard (the backfill has
  `PID_KEY`; `sync-current` has nothing). Two rapid Stops — a rewind then a new message, or
  overlapping sessions — produce two concurrent CPU-bound inference processes: the exact
  orphan-swarm the machines' reaper units fight, now amplified because each can do several
  inferences. `sync-current` gains a lock-file guard at startup: if a prior `sync-current` is
  running, exit 0 immediately and skip this sync (recovered on the next Stop). Skip-not-queue,
  so guards never themselves accumulate processes.
- **No full-transcript inlining in any result list** (inherited from the absorbed
  contract): A renders cards; B renders bounded excerpts; `ccrecall tail` is the only
  full-fetch path.
- **Conversations DB is a public contract.** Schema changes are additive
  (`CREATE ... IF NOT EXISTS`) plus a version-gated re-embed; never drop or rewrite
  `messages`/`branches`/`branch_messages`.

## Dependencies and Assumptions

- **`build_exchange_pairs`** (`summarizer.py:124-164`) is the chunk unit: it already
  produces `{user, assistant, timestamp, index}` per exchange, tool-stripped. Assumed
  stable; this design extends it to also carry the first message's `uuid` for the locator.
- **`sqlite-vec 0.1.9`** (vendored) supports DELETE-by-rowid, bulk `IN(...)` delete, and
  two-level cascade triggers — confirmed by spike (see Architecture → vec0 spike).
- **`fastembed` / `jina-v2-small`** (8192-token limit, 512-dim) is unchanged; per-exchange
  text sits comfortably under the limit except for pathological pastes (FR#10).
- **`context_summary_json`** exists on synced branches and carries `topic`/`disposition`
  for the Track A card; predating branches are covered by the contract's degrade path.
- No external services, auth, or network beyond the existing local model. Read/write only
  over `~/.ccrecall/conversations.db`. **Caveat (challenge Finding M22):** the "local model"
  assumption is false on *first install* — `get_model()` (`embeddings.py:56-66`) triggers a
  ~120 MB fastembed download synchronously. Inside the detached `sync-current` this is an
  invisible multi-minute hang (logging is off by default). Mitigate by **warming the model
  cache during `ccrecall setup`/onboarding** so the detached path never downloads, and emit a
  logged warning (regardless of `logging_enabled`) if a download is ever triggered from a
  detached context. This is pre-existing behavior the chunk change inherits, not creates, but
  it is worth closing alongside.

## Architecture

The change has four layers: **(1) schema** (chunk store + chunk vectors + cascade),
**(2) write path** (incremental per-exchange embed), **(3) backfill** (corpus re-embed at
chunk grain), **(4) search + output** (best-chunk rollup for A, direct chunk retrieval for
B, both emitting the absorbed contract's shapes). Ranking *algorithm* (RRF, the FTS
cascade, per-session dedup) is unchanged; what changes is the **unit being ranked** (chunk
not branch-summary) and the **terminal render** (card/snippet not dump).

### Terminology reconciliation

The absorbed contract's non-goals mention a future "`message_vec`" index. This design
realizes that index as **`chunk_vec`**, where a **chunk == one exchange** (user + following
assistant turns). "Chunk", "exchange", and the contract's "matched exchange" are the same
unit. `exchange_index` is the chunk's position within its active-leaf branch, as defined by
`build_exchange_pairs`.

### vec0 spike (both critical questions resolved by evidence)

Run against the vendored `sqlite-vec 0.1.9` — see research brief. Confirmed:
DELETE-by-rowid on a vec0 table ✓, bulk `DELETE ... WHERE chunk_id IN (...)` ✓, two-level
cascade triggers (`branches`→`chunks`→`chunk_vec`) firing correctly ✓, KNN after mutation ✓,
and `INSERT OR REPLACE` rejected with a UNIQUE-constraint error ✓ (so DELETE+INSERT is
mandatory). The Stop hook was traced (`memory_sync.py` → detached `sync-current`) and
confirmed **non-blocking** on embedding.

### (1) Schema — `schema.py`, `db.py`

New `chunks` metadata table (the source of truth for which chunk rowids belong to a branch,
and the carrier of the Track B locator + bounded display text):

```sql
CREATE TABLE IF NOT EXISTS chunks (
  id                INTEGER PRIMARY KEY,
  branch_id         INTEGER NOT NULL REFERENCES branches(id),
  exchange_index    INTEGER NOT NULL,          -- position from build_exchange_pairs
  content_hash      TEXT NOT NULL,             -- hash of the embedded text; drives incremental re-embed
  first_message_uuid TEXT,                     -- locator anchor (best-effort; timestamp is the hard anchor)
  timestamp         TEXT,                      -- exchange timestamp (locator)
  user_text         TEXT,                      -- per-message-bounded user turn (Track B "user" field)
  assistant_text    TEXT,                      -- per-message-bounded assistant turn (Track B "assistant" field)
  was_capped        INTEGER NOT NULL DEFAULT 0, -- 1 if the embedded text was head+tail-capped (diagnostics)
  embedding_version INTEGER NOT NULL DEFAULT 0,
  embedding_model   TEXT,
  UNIQUE(branch_id, exchange_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_branch ON chunks(branch_id);
CREATE INDEX IF NOT EXISTS idx_chunks_version ON chunks(embedding_version);
```

New vec0 table, keyed by chunk rowid (created in `_ensure_vec_schema`, which already gates
on `sqlite-vec` availability):

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec
  USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[512]);
```

Two cascade triggers (mirrors the existing `branches_vec_ad` one level deeper):

```sql
CREATE TRIGGER IF NOT EXISTS branches_chunks_ad
  AFTER DELETE ON branches
  BEGIN DELETE FROM chunks WHERE branch_id = OLD.id; END;

CREATE TRIGGER IF NOT EXISTS chunks_vec_ad
  AFTER DELETE ON chunks
  BEGIN DELETE FROM chunk_vec WHERE chunk_id = OLD.id; END;
```

`_ensure_vec_schema` (`db.py:173-204`) is extended to create `chunk_vec` + the two
triggers and to **drop the obsolete `branch_vec`**. The drop must be an **explicit,
unconditional** `DROP TRIGGER IF EXISTS branches_vec_ad; DROP TABLE IF EXISTS branch_vec;` —
**not** routed through the existing dimension self-heal (`db.py:189-195`). That self-heal only
fires when the stored DDL's `float[N]` ≠ the current `EMBEDDING_DIM`; since this design keeps
`EMBEDDING_DIM = 512`, an existing `branch_vec(float[512])` satisfies the check and would
**never** be dropped (challenge Finding H5). The unconditional drop runs once at schema-ensure
and is lossless (derived data). `branch_vec_queryable` gains a `chunk_vec` sibling
(`chunk_vec_queryable`) used by the query/write/backfill guards.

**`chunk_vec` drop resets watermarks.** If `chunk_vec` is ever dropped and recreated — the
dimension self-heal on a future model swap, or operator incident response — every branch
watermark would still read `EMBEDDING_VERSION` while its vectors are gone (a stale-but-true
state the backfill predicate can't see). So whenever `_ensure_vec_schema` drops `chunk_vec`,
it must also `UPDATE branches SET embedding_version = 0` (reset all watermarks), forcing
backfill to repopulate. This pairs with the per-chunk heal clause below.

**Embedding bookkeeping moves to the chunk grain.** `chunks.embedding_version` /
`embedding_model` are the source of truth for staleness (FR#9). The existing
`branches.embedding_version` / `embedding_model` columns are **retained** (public-contract
schema — not dropped) and repurposed as a per-branch **watermark** meaning "every current
exchange of this branch has a current-version chunk vector." For the watermark to be a
*trustworthy* backfill filter (not merely advisory), `embed_branch_chunks` maintains it with a
**clear-first / set-last** protocol:

> When the per-chunk diff (write-path step 5) finds any exchange needing embedding, the
> watermark is **cleared first** (set to `0`) within the single sync transaction, *before* the
> embed loop — no intermediate commit. It is set to `EMBEDDING_VERSION` **only at step 8**,
> after every exchange has a current-version chunk. If the embed loop throws mid-way, the
> write-path caller suppresses it and control returns normally, so the *one* commit at the end
> of the sync (`sync_current.py:137`) persists the cleared watermark together with whatever
> chunks did succeed — leaving the branch stale, never stale-but-true.

This makes the *caught-exception* failure safe: a half-embedded branch commits with
`embedding_version < EMBEDDING_VERSION`, so the watermark predicate catches it.

**But the watermark alone is NOT sufficient** (challenge Finding C1). Two failure modes escape
the watermark predicate, so the backfill **also carries an explicit chunk-grain heal clause**
(the generalization of today's `OR NOT EXISTS (… branch_vec …)` at `backfill_embeddings.py:84-85`,
which an earlier draft wrongly dropped):

```sql
OR EXISTS (
  SELECT 1 FROM chunks c
  WHERE c.branch_id = branches.id
    AND NOT EXISTS (SELECT 1 FROM chunk_vec WHERE chunk_id = c.id)
)
```

It catches a **committed orphan `chunks` row with no `chunk_vec`**, which arises two ways:
(a) a *suppressed write-path content error* — step 6 inserts the `chunks` row to allocate its
rowid, then `embed_text` raises and is swallowed, committing the row without its vector; and
(b) a *`chunk_vec` drop* without the watermark reset (defended separately by the reset-on-drop
above). What the heal clause does **not** cover is a full-transaction rollback (SIGKILL / OOM /
power-loss before the single commit): there the cleared watermark *and* the new `chunks` row both
roll back, leaving no orphan — that branch self-heals on its next Stop sync (the diff finds the
missing exchange), with the narrow never-resumed residual named in Edge Cases.

The discarded alternative was comparing a current-chunk *count* to `branches.exchange_count`;
that is unsafe because the two are computed over different inputs (`compute_branch_metadata`,
`parsing.py:224`, counts user turns over **raw JSONL entries** with `is_tool_result` /
`is_task_notification` / `is_teammate_message` exclusions, while `build_exchange_pairs`,
`summarizer.py:124`, counts over **fetched message rows**) — not guaranteed equal, so a
`count < exchange_count` test can misclassify. The `NOT EXISTS` heal clause needs no count.

`summary_version_at_embed` is **vestigial** for the chunk path (chunk staleness is driven by
`content_hash` + `EMBEDDING_VERSION`, not the summary version) — retained but unused by chunk
embedding. Query-time filtering reads chunk-grain version (FR#9), so a stale watermark never
suppresses an actually-current chunk; the watermark governs only backfill eligibility/status.

### (2) Write path — `session_ops.py`

`embed_branch` is replaced by `embed_branch_chunks(cursor, branch_db_id, branch_msgs,
is_active, vec_writable)`:

1. Guard: return unless `is_active and vec_writable and branch_msgs`.
2. Build exchanges via `build_exchange_pairs(branch_msgs)` (extended to carry
   `first_message_uuid`).
3. For each exchange compute `text = cap_for_embedding(f"{user}\n\n{assistant}")` and
   `content_hash = sha256(text)`, plus a `was_capped` flag. `cap_for_embedding` is
   **token-aware, not character-count** (challenge Finding M12): it head+tail-caps to a
   character budget, then verifies `len(tokenize(text)) <= MODEL_TOKEN_LIMIT` and tightens the
   cap until it fits — so dense content (minified JSON, base64) that is under the char budget
   but over the 8192-token limit cannot reach `embed_text` and trip the `CONTENT_ERROR`
   sentinel. The fastembed tokenizer is reachable via the already-loaded model. (Worst case:
   a single exchange that cannot fit even when fully capped still raises and is marked
   `CONTENT_ERROR` — but the token check makes that the genuine pathological exception, not a
   routine density miss.)
4. Load existing `chunks` rows for the branch (`exchange_index → (content_hash,
   embedding_version, model)`).
5. **Diff (write-path eligibility):** the write path embeds an exchange iff **no chunk row
   exists** or its **`content_hash` changed**. It deliberately does **not** re-embed merely
   *version-stale* or *model-mismatched* chunks — those are left to the background backfill
   (challenge Finding H6). This keeps steady-state at ~1 inference/sync **even immediately
   after an `EMBEDDING_VERSION` bump**: the next sync of a long existing session embeds only
   its genuinely-new exchange (1 inference), not all N stale ones (which would spike the
   detached process). The version-stale chunks stay queryable-excluded (FR#9) until backfill
   upgrades them — an accepted migration-window degradation. Backfill eligibility (below) is
   the broader predicate that *does* include version-stale.
   - As a guardrail against pathological cases (e.g. a brand-new active leaf from a rewind
     with many fresh exchanges, or a first sync of a long imported session), the write-path
     embed loop is **capped at `MAX_WRITE_PATH_EMBEDS_PER_SYNC`** (a small constant, e.g. 8);
     any remainder is left to backfill. This bounds the detached process's worst case.
   - If the diff finds nothing to embed and no prune is needed, every exchange already has a
     *content-current* chunk, so set the watermark to `EMBEDDING_VERSION` **iff every chunk is
     also version-current** (idempotent repair of a prior failed step-8) and return.
5a. **Clear-first:** if step 5 found any needing-embed exchange, set the branch watermark to
   `0` now (same transaction, before the embed loop) — see the watermark protocol above.
6. For each needing-embed exchange: upsert the `chunks` row (DELETE+INSERT on
   `(branch_id, exchange_index)`, storing `content_hash`, `was_capped`, locator fields, and
   the bounded `user_text`/`assistant_text`), `embed_text(text)`, then DELETE+INSERT the
   `chunk_vec` row keyed by the chunk's `id`, then set the chunk's `embedding_version`/`model`
   (**order invariant**: vector first, bookkeeping last). `user_text`/`assistant_text` are
   bounded with the **same head+tail logic** used for the embedding text (per turn), so the
   displayed excerpt is aligned with the region that produced the vector — a Track B snippet
   never shows only the head while the match lived in the tail (challenge Finding M14).
7. **Prune:** delete `chunks` rows whose `exchange_index` no longer exists (the cascade
   trigger removes their vectors).
8. After the loop, if every exchange now has a current-version chunk, set the branch
   watermark (`branches.embedding_version`/`model`); else leave it stale.

**`embed_branch_chunks` raises** on failure — it does **not** swallow exceptions internally.
This mirrors today's split, where `embed_branch` is a *suppressing wrapper* around the raising
`embed_text`/`write_branch_embedding`. The two callers handle failure differently and must
both retain a safety net:

- **Write path:** `sync_branch` (`session_ops.py:395-441`, which already has `branch_msgs` in
  scope at line 408) calls `embed_branch_chunks` inside `contextlib.suppress(Exception)` — a
  sync must never fail on a non-essential embed. A suppressed failure leaves the branch's
  watermark stale **and/or** a chunk vector missing; the backfill heal clause (below) is the
  net that catches it.
- **Backfill path:** calls `embed_branch_chunks` inside the existing per-branch SAVEPOINT and
  catches `(ValueError, OverflowError, UnicodeError)` to mark the content-error sentinel — so
  a persistently-failing branch is marked once and skipped, not looped. Because
  `embed_branch_chunks` raises (rather than suppressing), these content errors actually reach
  the SAVEPOINT handler (a problem the previous draft's internal-suppress version silently had).

### (3) Backfill — `hooks/backfill_embeddings.py`

The batch loop, two-level failure model, `--threads`/`--days`/`--limit`, nice-level, and
content-error sentinel are **preserved**. What changes:

- **Universe widens to drop the summary requirement** (challenge Finding M11). Chunk embedding
  reads raw exchange text, not the summary, so a branch whose summary computation failed
  (`context_summary = NULL`, swallowed at `session_ops.py:355-368`) still has embeddable
  content. The chunk-path universe is therefore **active leaf with at least one message**
  (`is_active = 1 AND EXISTS(branch_messages)`), *not* the inherited `EMBEDDABLE_BRANCH_FILTER`
  (which requires a non-empty `context_summary`). Define a `CHUNK_EMBEDDABLE_BRANCH_FILTER`
  for this; the old filter stays for any summary-dependent caller.
- `build_selection` eligibility = **watermark-stale OR the per-chunk heal clause**, excluding
  the content-error sentinel:
  - watermark-stale: `branches.embedding_version < EMBEDDING_VERSION` OR `embedding_model`
    mismatch OR NULL — this is what makes the whole corpus eligible after the version bump, and
    it includes the **version-stale chunks the write path deliberately skips** (Finding H6, so
    backfill owns the migration re-embed).
  - heal clause: `OR EXISTS (chunks row for this branch with no chunk_vec)` — catches crash
    victims and post-drop orphans the watermark can't see (Finding C1, see Schema).
- **Per-branch message fetch** (was implicit; challenge Finding T1 + M10). The current loop
  selects `(id, context_summary)` and embeds the summary directly. The new loop must fetch the
  branch's messages to supply `branch_msgs` to `embed_branch_chunks`: call
  `fetch_branch_messages(cursor, branch_id, include_notifications=False)` — **extended to
  `SELECT m.uuid`** so backfilled chunks get a real `first_message_uuid` instead of NULL across
  the whole historical corpus (M10). A `sqlite3.Error` during this fetch is a **batch-abort**
  failure (re-raise to the batch handler), **not** a content-error sentinel — the two must not
  be conflated (T1).
- Per-branch embed calls the **same** `embed_branch_chunks` as the write path (one code path).
  Because it **raises**, the per-branch SAVEPOINT handler catches `(ValueError, OverflowError,
  UnicodeError)` content errors and marks the `CONTENT_ERROR_VERSION = -1` sentinel on the
  branch watermark — marked once, skipped thereafter, never looped to the no-progress abort.
  The token-aware cap (write-path step 3) makes content errors the genuine exception.
- **Status/ETA counts inferences, not branches** (challenge Finding M21). Each branch now
  contributes N inferences (one per exchange); a per-*branch* ETA on a 15-exchange corpus
  under-reports the run length ~15×, so a 70-minute run looks hung. Add a `total_inferences`
  counter alongside `total_updated` (branches) and report both: "N exchanges embedded across
  M/total branches." `count_status` likewise reports chunk coverage, not branch coverage.

### (4) Search + output — `search_conversations.py`, `fusion.py`, `formatting.py`

**Entrypoint A (session cards).** `_get_vec_branch_ids` becomes a chunk-KNN + best-chunk
rollup:

```sql
SELECT chunk_id, distance FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance
```

then JOIN `chunks → branches → sessions → projects`, filter
`chunks.embedding_version = EMBEDDING_VERSION AND chunks.embedding_model = EMBEDDING_MODEL`
and `branches.is_active = 1` plus the existing `--project`/`--session`/`--path` filters,
and **keep the first (best-distance) chunk per branch** — preserving KNN order, exactly the
"max" rollup expressed as ranks. The returned best `(branch_id, distance, chunk_id)`
feeds:

- the **unchanged** `rrf([fts_ids, vec_ids])` → `_dedup_by_session` rank pipeline, and
- a new **score-returning fusion** `rrf_scored(ranked_lists) -> list[(id, score)]` (sibling
  of `rrf`, which stays ids-only) so the card can carry the raw fused value. The absorbed
  contract requires this; `fusion.py` gains the sibling, `rrf` is untouched.

`rrf_scored` returns the **raw** fused value (the card's `score_raw`). The contract's
presented `score` (min-max normalized to `[0,1]` within the result set) is computed at
**render time** over the final bounded result set — *not* inside `rrf_scored` — so that the
normalization window is the displayed results, not the full fused list. Track B normalizes
its `1.0 - distance` values the same way at render time. (This applies to both entrypoints.)

The terminal step changes from `_hydrate_branches` → `format_markdown_session` (dump) to a
**card renderer** that reads `context_summary_json` (topic/disposition) plus the branch row's
`files_modified`/`commits`/`tool_counts` columns and the branch/session/project join columns
and the fused score — **no full `fetch_branch_messages` hydration** on the A path. This is
exactly the absorbed contract's Track A.

Two precise points the contract requires (challenge Findings C4, M19):
- **Graceful-degrade path (contract FR#11 / AC#7).** When a branch has no
  `context_summary_json` (older/uncached), the card still renders: `topic` comes from the
  **first user message** via a *targeted single-row* query —
  `SELECT m.content FROM branch_messages bm JOIN messages m ON bm.message_id = m.id
  WHERE bm.branch_id = ? AND m.role = 'user' ORDER BY m.timestamp ASC LIMIT 1` — and counts
  from the branch row. This `LIMIT 1` is **not** a `fetch_branch_messages` call and does not
  violate the no-full-hydration constraint; the constraint forbids loading the *whole*
  transcript, not a one-row topic probe. Without this, an implementer reading "no fetch"
  literally leaves `topic: null` and silently fails AC#7.
- **`tool_counts` column guard.** `tool_counts` was added to `branches` after the initial
  schema; `recent_chats.py:37-41` already guards its read with a `PRAGMA table_info(branches)`
  check. The card hydrator reads `tool_counts` and must apply the **same guard** (or a cached
  one-time schema-introspection in `db.py`), or it raises `OperationalError: no column
  tool_counts` on a pre-column DB.

**Entrypoint B (matched exchanges).** Surfaced as a **separate CLI command,
`ccrecall search-messages QUERY`** (challenge Finding C3), *not* a `--chunks` flag on `search`.
Rationale: B returns a fundamentally different result type (exchange snippets vs session cards),
and a flag that flips output type is a mode-flag that collides with `--keyword-only` (what is
`search --chunks --keyword-only`?); two distinct jobs (the contract's "find sessions" vs "find
messages") deserve two verbs, and a named command is discoverable in `ccrecall --help` and can
grow its own defaults (a future `--context N`). It shares `--project`/`--session`/`--path`/
`--json` via a cyclopts option group, so the separate command costs no plumbing. The skill and
`tool-reference.md` document this exact name.

A parallel select path: chunk-KNN (same query),
filter to current version + user filters, **do not roll up** — return the top chunk rows
hydrated into the contract's snippet shape directly from the `chunks` row: `exchange_index`,
`timestamp`, `first_message_uuid` (locator), and the contract's `user`/`assistant` fields
from the row's bounded `user_text`/`assistant_text` columns. Because the chunk row carries
both bounded turns, B needs **no message fetch** and no FTS-rowid→exchange mapping (the chunk
*is* the exchange — cleaner than the keyword-path mapping the contract describes for FTS B).

The contract's keyword-oriented Track B fields have these **semantic-path** values (the chunk
KNN has no discrete term hits): `match_terms` is `[]` (no keyword spans to highlight — the
markdown excerpt renders without highlighting on the vector path), and `matched_role` is
`null` (the whole user+assistant exchange is the match unit, not a single role). The deferred
keyword Track B rung populates `match_terms`/`matched_role`/highlighting via FTS `snippet()`;
the field *names* are identical, only their values differ by ranking source — which is exactly
the shape-is-ranking-agnostic property the contract relies on.

**Track B vector `score_raw`.** Vectors are L2-normalized (`embeddings.py:normalize`), so the
vec0 distance is bounded and lower-is-better. To satisfy the contract's higher-is-better
`score_raw` invariant, B's vector path stores `score_raw = 1.0 - distance` (a cosine-style
similarity; larger = better). The presented `score` is then min-max normalized over the
result set per the contract (see below).

**Carrying the `ranked` signal to the renderer** (challenge Finding M17). The contract's
envelope needs `ranked: false` on the LIKE-only rung. `rrf_scored` is never called on that
rung, so the search entry point returns a `(results, ranked: bool)` pair (a small wrapper
struct), and the card/snippet renderer uses `ranked == False` to emit `score: null`/
`score_raw: null` per result and `ranked: false` in the envelope — wiring the existing
FTS5→FTS4→LIKE cascade's bottom rung to the contract's unranked shape.

**Track B on vec0-unavailable machines** (challenge Finding H7, decided: defer + document,
tracked by **issue #34**). Track B's *only* ranking source in this landing is the chunk-KNN;
the keyword-path Track B (FTS `snippet()` → snippet shape) is a named non-goal. So on a machine
where `vec0` won't load (e.g. the resource-constrained laptop), `ccrecall search-messages`
returns an **empty `ranked: false` envelope** — it does not error, but it cannot degrade to
keyword results the way Track A does. This is a deliberate, documented gap for this release;
issue #34 tracks adding the FTS Track B rung so the contract's "either track" LIKE fallback is
honored everywhere. (Track A is unaffected — it already degrades to the keyword path.)

**Overfetch.** `top_k = max(max_results * OVERFETCH_MULTIPLIER, OVERFETCH_FLOOR)`
(`search_conversations.py:315`) assumes one vector ≈ one branch. With many chunks per
branch, the top-k chunks may collapse to far fewer branches. For A, raise the chunk-KNN `k`
to `max_results * OVERFETCH_MULTIPLIER * CHUNK_COLLAPSE_FACTOR` (a new constant, start at ~8 —
a generous chunks-per-session estimate) with the existing floor, so the post-rollup distinct
session count still fills `max_results`. B uses the plain `max_results`-family bound (no
rollup). Because a static multiplier cannot *guarantee* fill on an adversarial corpus (one
giant session dominating the top-k), add an **observability** step (challenge Finding M13): when
the post-rollup session count is `< max_results`, **emit a diagnostic log line** (pre-rollup
chunk count, post-rollup session count, collapse ratio) so a chronic under-fill is visible
rather than silently indistinguishable from genuine sparsity. An optional one-shot adaptive
retry (double `k`, re-fetch once) is a tunable follow-up, not required for the first landing.

### Where the absorbed contract plugs in

`output-format-contract.md` is the authoritative spec for: the shared envelope
(`query`/`ranked`/`count`/`results`), the card fields + markdown/JSON forms (its FR#1-3,
FR#10-13), the snippet fields + locator (its FR#4-6), the score representation
(normalized `score` + higher-is-better `score_raw`, the `ranked:false` LIKE path), and the
documentation/test updates for the output layer. This design supplies the **vector ranking
signal** those shapes render. Where the contract and this design both touch a file
(`formatting.py`, `search_conversations.py`, `fusion.py`), the contract governs the
*output shape* and this design governs the *ranking input* — they compose, they do not
conflict.

## Replacement Targets

- **`session_ops.py::embed_branch` — replaced** by `embed_branch_chunks`. The summary-vector
  write path is removed, not kept alongside. Its sole caller (`sync_branch`) migrates in the
  same change.
- **`db.py::branch_vec` table + `upsert_branch_vec` + `branch_vec`-specific
  `write_branch_embedding` + `branches_vec_ad` trigger — replaced** by `chunks`/`chunk_vec`,
  a chunk upsert helper, a chunk embedding-write helper, and the two cascade triggers.
  `_ensure_vec_schema` drops `branch_vec` (lossless — derived data).
- **`search_conversations.py::_get_vec_branch_ids` (branch-vec KNN) — replaced** by the
  chunk-KNN + best-chunk rollup. `_hydrate_branches`'s message-loading is **removed from
  the A path** (A renders from `context_summary_json`, per the absorbed contract).
- **`formatting.py::format_markdown_session` / `format_json_sessions` (full-transcript
  dump) — replaced *for the search path only*** by the contract's card + snippet + envelope
  renderers. **These two functions are RETAINED in `formatting.py`** — `recent_chats.py:13`
  imports and calls both, and `recent` is out of scope for this change (the contract names
  "unify `recent` onto the card renderer" a deferred non-goal). The search path stops calling
  them; they are **not** removed (challenge Finding H8 — removing them breaks `recent` at
  import time with no test catching it).
- **`fusion.py::rrf` score discard — superseded** by the `rrf_scored` sibling for the card
  `score_raw`; `rrf` itself stays for the ids-only dedup pipeline.
- **`backfill_embeddings.py::build_selection` (branch-summary eligibility) — replaced** by
  chunk-eligibility (watermark filter + per-chunk diff via `embed_branch_chunks`).

`render_context_summary` (the SessionStart-injection renderer) is **not** replaced — it is a
different consumer and the context-injection path is untouched.

## Migration

- **Additive schema** in `SCHEMA_CORE` / `_ensure_vec_schema`: `chunks` + indexes +
  `chunk_vec` + the two cascade triggers, all `IF NOT EXISTS`. `branch_vec` and
  `branches_vec_ad` are dropped by an **explicit unconditional** `DROP TRIGGER IF EXISTS` /
  `DROP TABLE IF EXISTS` in `_ensure_vec_schema` — **not** via the dimension self-heal, which
  never fires at the unchanged `float[512]` (challenge Finding H5; see Architecture → Schema).
  Whenever `_ensure_vec_schema` drops `chunk_vec` (future model swap / incident), it also
  resets all branch watermarks (`UPDATE branches SET embedding_version = 0`) so the dropped
  vectors are repopulated (challenge Finding C1).
- **Bump `EMBEDDING_VERSION` 2 → 3** in `embeddings.py`. The watermark filter then makes
  every active-leaf branch eligible; backfill re-embeds the corpus at chunk grain, and
  embed-on-write covers forward sessions.
- **Versioning of the chunk builder:** chunk re-embed is driven by `content_hash` (content
  change) and `embedding_version`/`model` (model/version change) at the chunk grain. No
  separate `summary_version_at_embed` is needed for chunks — chunk text derives from raw
  exchange content, not the summary — so that branch column becomes vestigial for embedding
  (retained, unused by the chunk path). A new `CHUNK_VERSION` is **not** introduced; the
  `content_hash` + `EMBEDDING_VERSION` pair fully determines staleness, which is simpler and
  one fewer knob.
- **No synced history is lost** (FR#8/AC#7): `messages`/`branches`/`branch_messages` are
  untouched; only the `branch_vec` derived table is dropped and `chunk_vec` regenerated.
- **Reversibility:** reverting the code + dropping `chunks`/`chunk_vec` and re-running the
  old branch-summary embed restores the prior state from intact history. The migration is
  forward-only in practice (no one downgrades), but loses nothing recoverable.

## Convention Examples

### Boundary-narrow exception handling (degrade vs. propagate)

**Source:** `src/ccrecall/search_conversations.py:150-157`

```python
try:
    serialized = sqlite_vec.serialize_float32(query_vec)
    rows = cursor.execute(
        "SELECT branch_id, distance FROM branch_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (serialized, top_k),
    ).fetchall()
except sqlite3.Error:
    return []  # DB-level failure → degrade to keyword; a real (non-DB) bug still propagates
```

The new chunk-KNN path follows the same rule: catch `sqlite3.Error` to degrade, never a
bare `except`.

### Order-invariant embedding write (vector first, bookkeeping last)

**Source:** `src/ccrecall/db.py:137-154`

```python
def upsert_branch_vec(cursor, branch_id, embedding):
    """Replace a branch's vector row (DELETE+INSERT — vec0 rejects INSERT OR REPLACE)."""
    cursor.execute("DELETE FROM branch_vec WHERE branch_id = ?", (branch_id,))
    cursor.execute("INSERT INTO branch_vec(branch_id, embedding) VALUES (?, ?)",
                   (branch_id, sqlite_vec.serialize_float32(embedding)))

def write_branch_embedding(cursor, branch_id, embedding, summary_version):
    upsert_branch_vec(cursor, branch_id, embedding)          # vector FIRST
    cursor.execute("UPDATE branches SET embedding_version = ?, ... WHERE id = ?", ...)  # bookkeeping LAST
```

The chunk write helpers (`upsert_chunk_vec`, `write_chunk_embedding`) mirror this exactly at
the chunk grain.

### vec0 cascade-on-delete trigger

**Source:** `src/ccrecall/db.py:200-204`

```python
conn.execute(
    "CREATE TRIGGER IF NOT EXISTS branches_vec_ad"
    " AFTER DELETE ON branches"
    " BEGIN DELETE FROM branch_vec WHERE branch_id = OLD.id; END"
)
```

The chunk design adds a second level (`branches`→`chunks`→`chunk_vec`); the spike confirmed
the two-trigger chain fires end-to-end on a single `DELETE FROM branches`.

### In-memory DB fixture for search tests

**Source:** `tests/test_search.py`

```python
@pytest.fixture
def search_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.commit()
    # … seed projects / sessions / branches / messages / branch_messages …
    yield conn
    conn.close()
```

New chunk/search tests seed the same way (real SQLite + real vec0 where available), mocking
only true external boundaries.

## Alternatives Considered

- **Single full-transcript vector per branch** (embed the whole conversation as one 512-dim
  vector). Pros: stays 1 vector/branch, no schema change, and *does* cover the middle.
  Rejected: averaging a 40-exchange session into one vector is semantically muddy — a
  specific middle topic's signal is diluted, precision drops — and it **cannot** produce
  entrypoint B (no per-exchange locator). The user is leaning chunk-level for exactly this
  reason; kept as the documented "do less" fallback only if effort must be minimal and B is
  dropped.
- **Per-message chunks** (embed each message separately). Rejected: ~2× the vectors, splits
  a question from its answer (hurting retrieval precision), and embeds bare user turns
  ("yes, do that") with no standalone meaning. No existing segmentation produces clean
  per-message embedding units.
- **Windowed (token-budgeted) chunks.** Rejected: the model accepts 8192 tokens and
  exchanges sit well under that, so windowing solves a non-problem while blurring the
  locator (a window straddles exchanges) and adding tuning params.
- **Mean / top-k-mean session aggregation** instead of max. Rejected for the first landing:
  mean *dilutes* — a 40-exchange session with one perfect match scores low, directly
  counter to the recall goal. Max (best chunk) most directly answers "did *any* part of
  this session match?" Top-k-mean is a possible later refinement if max proves noisy.
- **Score-based fusion rewrite** (normalize cosine + BM25, weight them). Rejected: bigger
  change, breaks the pinned RRF tests, and the absorbed contract only needs the fused score
  *exposed*, not the fusion *algorithm* changed — the `rrf_scored` sibling suffices.
- **Branch-grain version filtering** (keep the existing branch watermark as the query
  filter). Rejected: a partially re-embedded branch would be excluded wholesale, dropping
  recall for its already-current chunks. Chunk-grain filtering (FR#9) is the bulletproof
  choice for hold scope.

## Test Strategy

### Existing Tests to Adapt
- **`tests/test_search.py`** — `search_sessions()` currently returns session dicts carrying
  full `messages`; assertions on `r["messages"]` move to card fields
  (topic/disposition/score/handle). Dedup, filter, and degradation tests stay (ranking
  unchanged). Add chunk-KNN rollup coverage.
- **`tests/test_db.py`** — `branch_vec` schema/upsert/cascade tests adapt to
  `chunks`/`chunk_vec` and the two-level cascade; add a cascade-delete count test (AC#6).
- **`tests/test_embeddings.py` / `tests/test_session_ops.py`** — `embed_branch` tests move
  to `embed_branch_chunks`: incremental diff (AC#4), prune-on-shrink, order invariant.
- **`tests/test_backfill_embeddings.py`** — `build_selection` eligibility + status counters
  adapt to chunk eligibility (AC#5); the two-level failure model tests stay.
- **`tests/test_fusion.py`** — add `rrf_scored` coverage alongside the existing ids-only
  `rrf` tests.
- **`tests/test_formatting.py`** — adapt to card/snippet/envelope renderers per the absorbed
  contract.

### New Test Coverage
- Mid-session recall: a query matching only a middle exchange surfaces the session (AC#1).
  **Integration**, seeded DB with a >8-exchange branch.
- Incremental write: append one exchange → exactly one new chunk vector, none re-embedded
  (AC#4). **Unit.**
- Chunk-grain staleness: mixed current/stale chunks → only current returned (AC#8). **Unit.**
- Cascade delete: branch delete removes chunks + vectors (AC#6). **Unit.**
- Watermark heal — caught exception: a suppressed embed-on-write failure clears the watermark
  (not stale-but-true) → backfill re-selects the branch. **Unit.**
- Watermark heal — crash / missing vector (AC#12): seed a `chunks` row with **no** `chunk_vec`
  while the watermark reads `EMBEDDING_VERSION` (simulating a WAL-rolled-back crash) → the
  backfill heal clause re-selects it. Guards challenge Finding C1. **Unit.**
- Version-bump bound (AC#11): after bumping `EMBEDDING_VERSION`, the next write-path sync of a
  long session embeds ≤ `MAX_WRITE_PATH_EMBEDS_PER_SYNC` chunks and leaves the rest version-
  stale for backfill. **Unit.**
- Concurrency guard (AC#13): a second `sync-current` while one holds the lock exits without
  embedding. **Unit**, with a fake/held lock file.
- Summary-failed branch (AC#15): a branch with `context_summary = NULL` is chunk-embedded and
  searchable (the widened `CHUNK_EMBEDDABLE_BRANCH_FILTER`). **Unit.**
- vec0-less Track B (AC#14): with the vector index unavailable, `search-messages` returns an
  empty `ranked:false` envelope and exits 0. **Unit.**
- Backfill content-error sentinel: a branch whose embed raises a content error is marked
  `CONTENT_ERROR_VERSION` once and skipped on the next pass (not looped). A `sqlite3.Error`
  during the per-branch message fetch aborts the batch instead (not the sentinel) — challenge
  T1. **Unit.**
- Backfill locator: backfilled chunks get a non-NULL `first_message_uuid` (the `m.uuid` fetch
  addition, challenge M10). **Unit.**
- History preservation: row counts of `messages`/`branches`/`branch_messages` unchanged
  across a version bump + backfill (AC#7). **Integration.**
- Token-aware cap: a dense over-token exchange embeds (not `CONTENT_ERROR`) and is retrievable
  (AC#9, AC#10, challenge M12). **Unit.**
- `recent` unaffected: `recent_chats` still renders via the retained
  `format_markdown_session`/`format_json_sessions` after the search path migrates (challenge
  H8) — a regression guard. **Unit.**
- Entrypoint A card shape + B snippet shape + envelope parity vs. the contract, including the
  `ranked:false` LIKE-path envelope and the uncached-branch degrade card (AC#2, AC#3, AC#10).
  **Unit**, mapped to `output-format-contract.md`.

### Tests to Remove
- Assertions that the search path returns full per-session message bodies (once A is
  message-free, those expectations are wrong, not relocated) — same removal the absorbed
  contract names.
- `branch_vec`-specific tests with no `chunk_vec` analog (the single-vector-per-branch
  assumption).

## Documentation Updates

- **`CLAUDE.md`** — the Architecture section's `db.py`/`schema.py` description (single
  `branch_vec`, 1:1 with branches) updates to the `chunks`/`chunk_vec` two-table layout;
  note the hot-path framing is unaffected (embedding was already off the hook).
- **`skills/ccr-recall/references/tool-reference.md`** and **`skills/ccr-recall/SKILL.md`**
  — update `search` output to scored cards (A); add the **`search-messages`** command for
  matched exchanges (B); document `score`/`ranked` and `tail` as the drill-in. Specify the new
  flag semantics (challenge Findings M16, M18): **`--verbose`** on the card path expands the
  markdown card to full `files_modified`/`commits` lists + the `tool_counts` dict (JSON always
  carries the full lists per contract FR#10 regardless of `--verbose`); **`--status`** now
  reports chunk coverage (current-version chunks / total) and branch-watermark coverage, not
  the old "embedded branches N/M". (These overlap the absorbed contract's Documentation
  Updates — land once, here.)
- **GitHub #31 / #32 / PR #33 / #34** — PR #33 is closed and its branch deleted (the contract
  survives as `output-format-contract.md` on this branch); #34 tracks the deferred keyword-path
  Track B fallback; cross-link #32 (richer summaries) as the enricher of the Track A card.
- **CHANGELOG** — handled by release-please from Conventional Commits at implementation
  time; no manual entry.

## Impact

### Changed Files
- `src/ccrecall/schema.py` — modify: add `chunks` table + indexes to `SCHEMA_CORE`.
- `src/ccrecall/db.py` — modify: `_ensure_vec_schema` creates `chunk_vec` + two triggers,
  **explicitly drops `branch_vec`** + its trigger, and resets watermarks on a `chunk_vec` drop;
  add `upsert_chunk_vec`, `write_chunk_embedding`, `chunk_vec_queryable`; retire `branch_vec`
  helpers; `fetch_branch_messages` SELECT gains `m.uuid` for the Track B locator.
- `src/ccrecall/embeddings.py` — modify: bump `EMBEDDING_VERSION` 2 → 3; add the head+tail
  cap helper here (it is about the model's token limit, so it lives beside `embed_text`;
  `session_ops.embed_branch_chunks` calls it when building each chunk's embed text).
- `src/ccrecall/summarizer.py` — modify: `build_exchange_pairs` also returns
  `first_message_uuid` (read by the locator); the exchange unit itself is unchanged.
- `src/ccrecall/session_ops.py` — modify: replace `embed_branch` with `embed_branch_chunks`
  (incremental diff + prune + watermark); `sync_branch` calls it.
- `src/ccrecall/hooks/backfill_embeddings.py` — modify: `build_selection` chunk eligibility;
  per-branch work calls `embed_branch_chunks`; status counters over chunk readiness.
- `src/ccrecall/search_conversations.py` — modify: chunk-KNN + best-chunk rollup for A;
  parallel chunk-retrieval path for B; thread the fused score + `ranked` signal; drop A's
  message hydration; first-message `LIMIT 1` degrade path + `tool_counts` PRAGMA guard;
  `print_status()` reports chunk coverage (not branch summary count).
- `src/ccrecall/fusion.py` — modify: add `rrf_scored` (keep `rrf` ids-only).
- `src/ccrecall/formatting.py` — modify: add card + snippet + envelope renderers; retire the
  full-transcript dump *from the search path*. Keep `format_markdown_session` /
  `format_json_sessions` (still used by `recent_chats.py`). (Shape governed by the contract.)
- `src/ccrecall/recent_chats.py` — read/verify: imports `format_markdown_session` /
  `format_json_sessions` (must stay); unchanged by this design but listed so the renderers are
  not removed as "replaced."
- `src/ccrecall/cli/commands.py` — modify: add a **`search-messages`** command (entrypoint B)
  sharing `search`'s option group; under the global `--json` contract.
- `src/ccrecall/hooks/memory_sync.py` / `src/ccrecall/hooks/sync_current.py` — modify: add the
  `sync-current` concurrency lock-file guard (skip-if-running).
- `src/ccrecall/hooks/memory_setup.py` / onboarding — modify: warm the fastembed model cache so
  the detached `sync-current` never downloads on first install.
- `skills/ccr-recall/SKILL.md`, `skills/ccr-recall/references/tool-reference.md` — modify:
  output-shape docs; document `search-messages`, `score`/`ranked`, and `--verbose`/`--status`
  semantics on the card path.
- `CLAUDE.md` — modify: vec schema description.
- `tests/test_*.py` (search, db, embeddings, session_ops, backfill_embeddings, fusion,
  formatting, recent_chats) — modify/add per Test Strategy.

**Deferred follow-up (not in this change):** the `branches_au` FTS trigger (`schema.py:128-138`)
fires on *any* `UPDATE branches`, so the clear-first watermark UPDATE needlessly re-indexes
`aggregated_content` (challenge Finding M20). Making the trigger column-selective
(`WHEN old.aggregated_content IS NOT new.aggregated_content`) is pre-existing tech debt, tracked
as follow-up — out of scope here.

### Behavioral Invariants
- **Ranking algorithm unchanged:** RRF, the FTS5/FTS4/LIKE cascade, per-session dedup,
  `is_active` filtering, and all `--project`/`--session`/`--path` filters keep current
  behavior. Only the *unit ranked* (chunk) and the *rendered output* (card/snippet) change.
- **Stop hook stays non-blocking:** `memory_sync.py` still spawns a detached `sync-current`
  and returns immediately; this design must not move embedding onto the hook's thread.
- **Hooks remain direct console-script entry points** (no new top-level imports that would
  slow the ~440ms hook import).
- **`ccrecall tail` is untouched** — the only full-fetch path.
- **Markdown stays the default;** the global `--json` flag stays the only output-format
  switch.
- **Synced history (`messages`/`branches`/`branch_messages`) is never dropped or rewritten.**

### Blast Radius
- The `/ccr-recall` skill (primary consumer) — output shape changes from transcript dump to
  cards/snippets; its synthesis instructions still apply; reference docs updated in the same
  wave.
- The opt-in `ccrecall backfill embeddings` (and any systemd timer running it) — its run is
  now chunk-grained; one-time corpus re-embed is larger (≈ exchanges-per-session ×) but
  off-hot-path with `--threads`/nice.
- The SessionStart context-injection path is **not** affected (uses `render_context_summary`,
  left in place).
- Any external/manual caller parsing `search --json` sees the new (documented) envelope —
  acceptable pre-1.0, called out in Documentation Updates.

## Open Questions

*No blocking questions remain.* The two load-bearing questions (Stop-hook blocking, vec0
multi-row delete) were resolved by investigation + spike before this doc; the `/mine-challenge`
pass's four CRITICAL and five HIGH findings are folded in above. Tunable constants
(`MAX_WRITE_PATH_EMBEDS_PER_SYNC`, `CHUNK_COLLAPSE_FACTOR`, the cap budget) are implementation
parameters — their *requirement* is fixed here, their *value* is for tuning.

**Tracked deferrals (not blocking this design):**
- **Keyword-path Track B fallback** for vec0-unavailable machines — **issue #34**. Track B
  returns an empty `ranked:false` envelope there until then.
- **Column-selective `branches_au` FTS trigger** — the clear-first watermark UPDATE double-fires
  the FTS re-index (challenge M20). Pre-existing tech debt; follow-up.
- **Single-result score presentation** — the absorbed contract (amended) sets `score: null`
  for a one-result set rather than a misleading `1.00` (challenge M15); see the contract's Score
  representation.
