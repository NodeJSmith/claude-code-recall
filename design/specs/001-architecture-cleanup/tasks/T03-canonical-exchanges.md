---
task_id: "T03"
title: "Add canonical exchange rows"
status: "planned"
depends_on: ["T01", "T02"]
implements: ["FR#7", "FR#8", "FR#9", "AC#6", "AC#7", "AC#8", "AC#12"]
---

## Summary

Make exchanges first-class conversation data. Add `exchanges`, link `chunks` to exchanges, promote existing chunk rows into exchanges, and update sync/import/search/backfill to use exchanges as the snippet/source-of-truth layer. Preserve existing conversation rows and derived vector self-healing.

## Target Files

- modify: `src/ccrecall/schema.py`
- modify: `src/ccrecall/database.py`
- modify: `src/ccrecall/session_ops.py`
- modify: `src/ccrecall/search_conversations.py`
- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- modify: `src/ccrecall/semantic_store.py`
- modify: `src/ccrecall/formatting.py`
- modify: `tests/test_db.py`
- modify: `tests/test_session_ops.py`
- modify: `tests/test_import_pipeline.py`
- modify: `tests/test_search.py`
- modify: `tests/test_backfill_embeddings.py`
- modify: `tests/test_integration.py`
- modify: `tests/test_formatting.py`
- modify: `tests/test_legacy_migration.py`
- read: `src/ccrecall/summarizer.py`
- read: `design/specs/001-architecture-cleanup/design.md`
- read: `design/specs/001-architecture-cleanup/tasks/context.md`

## Prompt

Implement the `Architecture -> Canonical Exchanges` and `Architecture -> Existing Data Upgrade` sections.

Add `exchanges` to the conversation schema with `branch_id`, `exchange_index`, `first_message_uuid`, `timestamp`, `user_text`, `assistant_text`, and `exchange_hash`. `exchange_hash` must be semantic-free: hash the deterministic user/assistant display payload, not embedding-capped text and not tokenizer/model output.

Add `chunks.exchange_id` and an index. Keep existing chunk locator/display columns as legacy redundancy for this pass. `chunks.content_hash` keeps the current embedding-specific meaning: exact embedded text after embedding-specific capping.

Create an idempotent upgrade path that promotes existing `chunks` rows into `exchanges`, computes `exchange_hash`, adds/populates `chunks.exchange_id`, and preserves row counts in `sessions`, `branches`, `messages`, `branch_messages`, and `import_log`.

Update sync/import so `build_exchange_pairs()` output upserts canonical exchanges even when semantic support is absent. Resync must prune exchanges whose `(branch_id, exchange_index)` no longer exists, and the prune must remove derived chunks and chunk vectors. Ensure cascade/cleanup works in base installs, not only when vec schema setup has run.

Update embedding/backfill/search so chunks derive from exchanges and Track B snippet hydration joins through `exchanges`. Existing search result shapes should remain stable except where snippets now come from `exchanges`.

## Focus

Current code stores exchange locator/display columns in `chunks` and tests assert directly on `chunks.exchange_index`, `user_text`, `assistant_text`, `was_capped`, and `first_message_uuid`. `tests/test_formatting.py` has snippet JSON/markdown expectations. `tests/test_search.py` seeds chunks directly in many helpers; update fixtures so they also seed exchanges and `chunks.exchange_id`. Be careful with SQLite FK changes: existing tables cannot gain `ON DELETE CASCADE` by altering old columns, so explicit cleanup/triggers may be needed.

## Verify

- [ ] FR#7: Sync/import creates canonical `exchanges` rows without semantic support.
- [ ] FR#8: Embedding chunks and `chunk_vec` rows derive from `exchanges` and hydrate snippets through `exchanges`.
- [ ] FR#9: Core conversation row counts are preserved during existing chunk promotion.
- [ ] AC#6: Tests show active branches get `exchanges` rows during sync/import even when semantic is absent.
- [ ] AC#7: Backfill/search snippet hydration reads through `exchanges` joined to `chunks`.
- [ ] AC#8: Existing `chunks` rows are promoted and linked through `chunks.exchange_id` without reducing core table counts.
- [ ] AC#12: Resync removing an exchange removes the exchange and derived chunk/vector rows.
