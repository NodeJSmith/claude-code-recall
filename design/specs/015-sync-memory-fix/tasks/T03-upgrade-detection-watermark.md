---
task_id: "T03"
title: "Add upgrade detection, watermark, and backfill selection"
status: "planned"
depends_on: ["T02"]
implements: ["FR#6", "FR#14", "FR#15", "AC#3", "AC#9", "AC#10"]
---

## Summary

Make backfill actually reach draft-quality chunks: modify `_diff_exchanges` to flag chunks needing upgrade based on `cap_tokens`, modify `_should_stamp_watermark` to withhold the watermark when draft-quality chunks exist, and add a `cap_tokens`-based clause to `build_selection` in `backfill_query.py`. Together these ensure the sync→backfill→sync lifecycle works correctly.

## Target Files

- modify: `src/ccrecall/embed_ops.py` — update `_diff_exchanges` (line 102) to accept `target_cap` and flag `cap_tokens < target_cap`; update `_should_stamp_watermark` (line 168) to check `cap_tokens` in both existing-chunks loop and freshly-embedded shortcut; update `existing_chunks` SELECT (line 226-232) to include `cap_tokens`; update `embed_branch_chunks` (line 190) to thread `cap_limit` to `_diff_exchanges` as `target_cap`
- modify: `src/ccrecall/hooks/backfill_query.py` — add `cap_tokens < MODEL_TOKEN_LIMIT` clause to `build_selection` (line 34)
- modify: `tests/test_embed_ops.py` — add tests for `_diff_exchanges` cap-tokens upgrade detection, `_should_stamp_watermark` positive/negative cases
- modify: `tests/test_backfill_embeddings.py` — add test that `build_selection` includes branches with draft-quality chunks
- read: `src/ccrecall/embed_ops.py` — `_diff_exchanges` (102-115), `_should_stamp_watermark` (168-187), `embed_branch_chunks` (190-263), `_write_embedded_chunks` (118-152)
- read: `src/ccrecall/hooks/backfill_query.py` — `build_selection` (34-70)

## Prompt

Implement the upgrade detection, watermark withholding, and backfill selection changes.

**_diff_exchanges (embed_ops.py:102):** Add `target_cap: int = MODEL_TOKEN_LIMIT` parameter. In the list comprehension (line 112), add a second condition: a chunk also needs re-embedding if its `cap_tokens` (from `existing_chunks`) is non-NULL and `effective_cap_tokens(existing["cap_tokens"]) < target_cap`. Use the `effective_cap_tokens` helper from T01. Import it at the top of the file. The existing hash-mismatch condition stays — a chunk is flagged by either hash mismatch OR cap-tokens upgrade.

**existing_chunks SELECT (embed_ops.py:226-232):** Add `cap_tokens` to the SELECT columns. Update the dict comprehension to include `"cap_tokens": row[4]` (adjust index after adding the column).

**_should_stamp_watermark (embed_ops.py:168):** Two changes:
1. The `existing_chunks` dict now has `"cap_tokens"`. In the existing loop (line 176-186), add a check: if `effective_cap_tokens(existing.get("cap_tokens"))` < `MODEL_TOKEN_LIMIT`, return `False`.
2. The freshly-embedded shortcut (line 178-179: `if idx in embedded_indices: continue`) must also check the per-exchange `cap_tokens` from `exchange_data`. Add `cap_tokens` to the `exchange_data` dict access: if `ed.get("cap_tokens") is not None and ed["cap_tokens"] < MODEL_TOKEN_LIMIT`, return `False` instead of continuing. This uses the per-exchange value (`cap_limit if was_capped else None`) — NOT the raw caller cap limit.

**embed_branch_chunks (embed_ops.py:190):** Thread `cap_limit` to `_diff_exchanges` as `target_cap`: `_diff_exchanges(exchange_data, existing_chunks, target_cap=cap_limit)`.

**build_selection (backfill_query.py:34):** Add a fourth clause to the `AND (...)` disjunction (after the heal clause, line 60-63):
```sql
OR EXISTS (
  SELECT 1 FROM chunks c
  WHERE c.branch_id = branches.id
    AND c.cap_tokens IS NOT NULL
    AND c.cap_tokens < ?
)
```
Add `MODEL_TOKEN_LIMIT` to `params`. Import `MODEL_TOKEN_LIMIT` from `ccrecall.embeddings` (or pass as a parameter — check if `backfill_query.py` already imports from `embeddings`).

## Focus

- `backfill_query.py` already imports `EMBEDDING_VERSION` and `EMBEDDING_MODEL` from `ccrecall.embeddings` (line 7-8), so adding `MODEL_TOKEN_LIMIT` to the import is safe.
- The `_should_stamp_watermark` freshly-embedded shortcut is critical. The design explicitly warns: use the per-exchange `cap_tokens` value (from `_prepare_exchange_data`), NOT the raw caller cap limit. Using the caller cap would make the watermark never stamp for any sync-path branch, even when all exchanges are short. AC#10 tests this positive case.
- `_diff_exchanges` returns `(needing_embed, indices_to_prune)`. The cap-tokens upgrade trigger adds to `needing_embed` but does NOT add to `indices_to_prune` — pruning is only for exchange indices that no longer exist, not for cap upgrades.
- `backfill_status.py:81` also calls `build_selection` — the new clause automatically includes draft-quality branches in its eligible count, which is correct behavior. No additional changes needed there.

## Verify

- [ ] FR#6: `_diff_exchanges` flags a chunk with `cap_tokens=4096` for re-embedding when `target_cap=MODEL_TOKEN_LIMIT`, even with matching `content_hash`
- [ ] FR#14: `_should_stamp_watermark` returns `False` when any chunk (existing or freshly-embedded) has `cap_tokens` non-NULL and `< MODEL_TOKEN_LIMIT`
- [ ] FR#15: `build_selection` SQL includes branches with chunks where `cap_tokens < MODEL_TOKEN_LIMIT`
- [ ] AC#3: Chunk at cap_tokens=4096, backfill-context `_diff_exchanges` (target_cap=8192) flags it; after re-embed at 8192, sync-context `_diff_exchanges` (target_cap=4096) does NOT flag it
- [ ] AC#9: Branch with cap_tokens=4096 chunks → watermark NOT stamped, branch selected by `build_selection`; after backfill re-embeds at 8192 → watermark stamped, branch no longer selected
- [ ] AC#10: Branch with freshly-embedded chunks all having `was_capped=False` (cap_tokens=NULL) → watermark IS stamped
