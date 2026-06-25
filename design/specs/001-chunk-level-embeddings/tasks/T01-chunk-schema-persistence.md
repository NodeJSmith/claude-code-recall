---
task_id: "T01"
title: "Add chunk schema and persistence helpers (additive)"
status: "done"
depends_on: []
implements: ["FR#7", "FR#8", "AC#6"]
---

## Summary

Lay the persistence foundation for chunk-level embeddings, **additively** — the existing
`branch_vec` table and its helpers stay intact so every later task's checkpoint is green; the
`branch_vec` teardown happens in T06 once all callers have migrated. Add the `chunks` metadata
table to `SCHEMA_CORE`, the `chunk_vec` vec0 table plus the two-level cascade triggers in
`_ensure_vec_schema`, the chunk write/probe helpers, the watermark-reset-on-`chunk_vec`-drop
defense, and extend `fetch_branch_messages` to carry `m.uuid` for the Track B locator.

## Target Files

- modify: `src/ccrecall/schema.py`
- modify: `src/ccrecall/db.py`
- modify: `tests/test_db.py`
- read: `design/specs/001-chunk-level-embeddings/design.md`
- read: `design/specs/001-chunk-level-embeddings/tasks/context.md`

## Prompt

Implement the schema + persistence layer described in design.md `## Architecture → (1) Schema`
and the `## Convention Examples` (order-invariant write, cascade trigger). Do **not** drop
`branch_vec`, do **not** delete `upsert_branch_vec`/`write_branch_embedding`, and do **not** bump
`EMBEDDING_VERSION` here — those are owned by T06 and T02 respectively. This task is purely
additive.

1. **`schema.py`** — add the `chunks` table and its two indexes to `SCHEMA_CORE` (so the
   metadata table exists even on connections without sqlite-vec). Use the exact DDL from design.md
   `## Architecture → (1) Schema`:
   - columns: `id` (INTEGER PRIMARY KEY), `branch_id` (NOT NULL REFERENCES branches(id)),
     `exchange_index` (NOT NULL), `content_hash` (TEXT NOT NULL), `first_message_uuid` (TEXT),
     `timestamp` (TEXT), `user_text` (TEXT), `assistant_text` (TEXT),
     `was_capped` (INTEGER NOT NULL DEFAULT 0), `embedding_version` (INTEGER NOT NULL DEFAULT 0),
     `embedding_model` (TEXT), `UNIQUE(branch_id, exchange_index)`.
   - indexes: `idx_chunks_branch` on `chunks(branch_id)`, `idx_chunks_version` on
     `chunks(embedding_version)`. All `CREATE ... IF NOT EXISTS`.

2. **`db.py` `_ensure_vec_schema`** — in addition to the existing `branch_vec` creation (keep it),
   create `chunk_vec` and the two cascade triggers (use the DDL from design.md):
   - `CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{EMBEDDING_DIM}])`
   - `branches_chunks_ad` AFTER DELETE ON branches → `DELETE FROM chunks WHERE branch_id = OLD.id`
   - `chunks_vec_ad` AFTER DELETE ON chunks → `DELETE FROM chunk_vec WHERE chunk_id = OLD.id`
   - **Watermark reset on `chunk_vec` drop:** generalize the existing dimension self-heal so that
     whenever `chunk_vec` is dropped-and-recreated (stale `float[N]` ≠ current `EMBEDDING_DIM`), it
     **also** runs `UPDATE branches SET embedding_version = 0` to force re-population (design.md
     `## Architecture → (1) Schema`, "`chunk_vec` drop resets watermarks"). Apply the same
     dimension self-heal pattern to `chunk_vec` that `branch_vec` already has, plus the watermark
     reset. Drop the `chunks_vec_ad` trigger before dropping `chunk_vec` (SQLite does not
     cascade-drop triggers when their target virtual table is dropped — mirror the existing
     `branch_vec` self-heal that drops `branches_vec_ad` first).

3. **`db.py` helpers** (mirror the order-invariant `branch_vec` helpers at the chunk grain):
   - `chunk_vec_queryable(conn) -> bool` — sibling of `branch_vec_queryable`, probes
     `SELECT 1 FROM chunk_vec LIMIT 1`, scoped to `sqlite3.Error`.
   - `upsert_chunk_vec(cursor, chunk_id, embedding)` — DELETE-then-INSERT on `chunk_vec` keyed by
     `chunk_id` (vec0 rejects `INSERT OR REPLACE`).
   - `write_chunk_embedding(cursor, chunk_id, embedding, embedding_version, embedding_model)` —
     vector upsert FIRST, then update the **chunk row's** `embedding_version`/`embedding_model`
     LAST (order invariant). The chunk row is created by the caller (T03/T04) before this is
     called; this helper only writes the vector + bookkeeping.

4. **`db.py` `fetch_branch_messages`** — add `m.uuid` to the SELECT and include `"uuid"` in each
   returned dict. This is additive: existing consumers (`recent_chats.py`, `search_conversations.py`
   `_hydrate_branches`) read by key and are unaffected.

5. **Tests (`tests/test_db.py`)** — add coverage:
   - `chunks` + `chunk_vec` tables exist after `_ensure_vec_schema` (and `chunks` exists via the
     plain `SCHEMA_CORE` path too); `branch_vec` still exists (additive, not yet dropped).
   - two-level cascade: deleting a `branches` row deletes its `chunks` rows AND their `chunk_vec`
     rows — verified by count (this is **AC#6**).
   - `upsert_chunk_vec` replaces (DELETE+INSERT) rather than erroring on a repeat.
   - `chunk_vec_queryable` returns True with vec loaded, False without.
   - `fetch_branch_messages` returns the new `uuid` field.

## Focus

- `_ensure_vec_schema` is at `db.py:173-204`; the existing dimension self-heal is at
  `db.py:189-195` and the `branch_vec` trigger creation at `db.py:200-204`. Follow that exact
  shape for `chunk_vec`.
- `EMBEDDING_DIM` (512) is imported from `ccrecall.embeddings` at `db.py:15`. Keep using it; do
  NOT change the dimension.
- The `chunks` table is the source of truth for which chunk rowids belong to a branch and carries
  the Track B locator (`first_message_uuid`, `timestamp`) + bounded display text
  (`user_text`/`assistant_text`).
- `get_db_connection` (`db.py:261-267`) calls `_ensure_vec_schema` then commits — your additions
  ride that existing transaction; do not add a separate commit.
- `tests/conftest.py` `memory_db` fixture executes `SCHEMA` (= `SCHEMA_CORE + SCHEMA_FTS5`); the
  `chunks` table will appear there automatically once added to `SCHEMA_CORE`. The vec tables need a
  vec-loaded connection (see `tests/test_db.py` existing `branch_vec` tests around lines 283-323
  for the load-extension fixture pattern).
- Blast radius: `schema.py` has no ccrecall deps (stdlib only) — keep it that way. `db.py` is
  imported widely; the new helpers are additive.

## Verify

- [ ] FR#7: Deleting a `branches` row removes all its `chunks` rows and all their `chunk_vec` rows
      via the two-level cascade triggers (`branches_chunks_ad`, `chunks_vec_ad`).
- [ ] FR#8: The schema additions are additive (`CREATE ... IF NOT EXISTS`) and touch no row in
      `messages`/`branches`/`branch_messages`; `chunks`/`chunk_vec` are the only new tables and are
      derived data. `branch_vec` is retained (not dropped) in this task.
- [ ] AC#6: Deleting a branch row deletes all its `chunks` rows and all their `chunk_vec` rows,
      verified by count in `tests/test_db.py`.
