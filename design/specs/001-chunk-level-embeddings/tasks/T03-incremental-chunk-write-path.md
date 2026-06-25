---
task_id: "T03"
title: "Replace embed_branch with incremental embed_branch_chunks"
status: "planned"
depends_on: ["T01", "T02"]
implements: ["FR#1", "FR#5", "FR#14", "AC#4", "AC#11"]
---

## Summary

Replace the per-branch summary embed (`embed_branch`) with `embed_branch_chunks`, the incremental
per-exchange write path. It embeds only new or content-changed exchanges (bounded per sync by
`MAX_WRITE_PATH_EMBEDS_PER_SYNC`), maintains the branch watermark with a clear-first/set-last
protocol, prunes chunks for vanished exchanges, and writes each chunk vector before its bookkeeping
(order invariant). Version-stale chunks are deliberately left to the background backfill so steady
state stays ≈ 1 inference/sync even right after the `EMBEDDING_VERSION` bump.

## Target Files

- modify: `src/ccrecall/session_ops.py`
- modify: `tests/test_session_ops.py`
- read: `src/ccrecall/db.py`
- read: `src/ccrecall/embeddings.py`
- read: `src/ccrecall/summarizer.py`
- read: `design/specs/001-chunk-level-embeddings/design.md`
- read: `design/specs/001-chunk-level-embeddings/tasks/context.md`

## Prompt

Implement `embed_branch_chunks` per design.md `## Architecture → (2) Write path` (steps 1–8, the
clear-first/set-last watermark protocol, and the raises-not-suppresses contract). Remove
`embed_branch` (design.md `## Replacement Targets`): the summary-vector write path is removed, not
kept alongside; its sole caller (`sync_branch`) migrates in this same change.

1. **Add `embed_branch_chunks(cursor, branch_db_id, branch_msgs, is_active, vec_writable)`** in
   `session_ops.py`:
   - **Step 1 — guard:** return unless `is_active and vec_writable and branch_msgs`.
   - **Step 2:** build exchanges via `build_exchange_pairs(branch_msgs)` (now carries
     `first_message_uuid`).
   - **Step 3:** for each exchange, `text, was_capped = cap_for_embedding(f"{user}\n\n{assistant}")`
     and `content_hash = sha256(text)`. Also compute the bounded display `user_text`/`assistant_text`
     using the **same head+tail logic per turn** so the shown excerpt aligns with the embedded
     region (challenge M14).
   - **Step 4:** load existing `chunks` rows for the branch into
     `exchange_index → (content_hash, embedding_version, model)`.
   - **Step 5 — diff (write-path eligibility):** embed an exchange iff **no chunk row exists** OR
     its **`content_hash` changed**. Do **not** re-embed merely version-stale / model-mismatched
     chunks — those are left to backfill (challenge H6). Cap the embed loop at
     `MAX_WRITE_PATH_EMBEDS_PER_SYNC` (define a small module constant, e.g. `8`); any remainder is
     left to backfill. If the diff finds nothing to embed and no prune is needed, set the watermark
     to `EMBEDDING_VERSION` **iff every existing chunk is also version-current** (idempotent repair
     of a prior failed step-8) and return.
   - **Step 5a — clear-first:** if step 5 found any needing-embed exchange, set
     `branches.embedding_version = 0` **now** (same transaction, before the embed loop).
   - **Step 6 — embed loop:** for each needing-embed exchange: upsert the `chunks` row
     (DELETE+INSERT on `(branch_id, exchange_index)`, storing `content_hash`, `was_capped`, locator
     fields `first_message_uuid`/`timestamp`, and the bounded `user_text`/`assistant_text`),
     `embed_text(text)`, then `write_chunk_embedding(cursor, chunk_id, vec, EMBEDDING_VERSION,
     EMBEDDING_MODEL)` (vector FIRST via `upsert_chunk_vec`, chunk bookkeeping LAST — the
     **order invariant**). Get the chunk's `id` from the upsert (`cursor.lastrowid` after the
     INSERT) to key the vector.
   - **Step 7 — prune:** delete `chunks` rows whose `exchange_index` no longer exists (the
     `chunks_vec_ad` cascade removes their vectors).
   - **Step 8:** after the loop, if every exchange now has a current-version chunk, set
     `branches.embedding_version = EMBEDDING_VERSION` and `embedding_model = EMBEDDING_MODEL`; else
     leave it stale (cleared at 5a).
   - **`embed_branch_chunks` RAISES on failure** — it does **not** swallow exceptions internally
     (this mirrors the old split where `embed_text`/`write_branch_embedding` raised and the wrapper
     suppressed). The backfill (T04) relies on this to reach its content-error handler.

2. **`sync_branch`** (`session_ops.py:395-441`): replace the `embed_branch(...)` call (line 441)
   with `embed_branch_chunks(...)` wrapped in `contextlib.suppress(Exception)` — a sync must never
   fail on a non-essential embed. `branch_msgs` is already in scope at line 408. A suppressed
   failure leaves the watermark cleared (5a) and/or a chunk vector missing; the backfill heal clause
   (T04) is the net.

3. **Remove `embed_branch`** and its now-unused import of `write_branch_embedding` from
   `session_ops.py` **only if** nothing else in the module uses it — note `write_branch_embedding`
   itself is NOT deleted yet (T06 owns that, after the backfill also migrates). Keep
   `branch_vec_queryable` import; `sync_session` still probes `vec_writable` via it.

4. **Tests (`tests/test_session_ops.py`)** — move/replace the `embed_branch` tests:
   - **Incremental diff (AC#4):** embed a branch, append one exchange, re-sync → exactly one new
     `chunk_vec` row written and no existing chunk re-embedded (assert by content_hash stability /
     embed call count).
   - **No-op on unchanged content (FR#5):** re-sync with no content change embeds nothing.
   - **Prune on shrink:** removing an exchange deletes its `chunks` + `chunk_vec` rows.
   - **Order invariant / caught-exception safety:** a swallowed embed error inside the loop leaves
     the branch watermark `< EMBEDDING_VERSION` (cleared at 5a), not stale-but-true.
   - **Version-bump bound (AC#11):** with many version-stale exchanges (simulate post-bump), the
     write path embeds at most `MAX_WRITE_PATH_EMBEDS_PER_SYNC` and leaves the rest version-stale.

## Focus

- `sync_branch` and `sync_session` are in `session_ops.py`; `vec_writable` is probed once in
  `sync_session` (`session_ops.py:555`) via `branch_vec_queryable(conn)`. **Keep the existing probe
  name `branch_vec_queryable` for the write path's guard in this task** — do NOT switch to
  `chunk_vec_queryable` here. `branch_vec` and `chunk_vec` are created together in
  `_ensure_vec_schema` (both present or both absent post-T01), so the existing probe is correct while
  `branch_vec` still exists. **T06 owns the rename of this exact probe to `chunk_vec_queryable`** (as
  a load-bearing step of the `branch_vec` teardown — once `branch_vec` is dropped, this probe MUST
  read `chunk_vec_queryable` or the write path silently stops embedding). Do not anticipate it here.
- `build_exchange_pairs` (T02) and `cap_for_embedding` (T02) and `upsert_chunk_vec` /
  `write_chunk_embedding` (T01) are your dependencies — import them at module top (no lazy imports).
- Use `hashlib.sha256(text.encode()).hexdigest()` for `content_hash`; import `hashlib` at top.
- The single sync commit happens at `sync_current.py:137` (write path) — `embed_branch_chunks`
  must not commit; it operates within the caller's transaction so a mid-loop failure commits the
  cleared watermark + whatever chunks succeeded together.
- Immutability rule: build new exchange/chunk dicts; do not mutate `branch_msgs` in place.

## Verify

- [ ] FR#1: Each exchange of an active-leaf branch is embedded as its own `chunk_vec` vector via
      `embed_branch_chunks` — verified by chunk/vector counts in `tests/test_session_ops.py`.
- [ ] FR#5: Steady-state embedding is bounded by new/content-changed exchanges — appending one
      exchange embeds exactly one new chunk; an unchanged re-sync embeds nothing (AC#4).
- [ ] FR#14: The write path embeds only new/content-changed exchanges and is bounded per sync at
      `MAX_WRITE_PATH_EMBEDS_PER_SYNC`; version-stale chunks are left for backfill.
- [ ] AC#4: After appending one exchange and re-syncing, exactly one new chunk vector is written and
      no existing chunk is re-embedded.
- [ ] AC#11: After simulating an `EMBEDDING_VERSION` bump, the next write-path sync of a long branch
      embeds at most `MAX_WRITE_PATH_EMBEDS_PER_SYNC` chunks and leaves the rest version-stale.
