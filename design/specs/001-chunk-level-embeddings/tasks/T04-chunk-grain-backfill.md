---
task_id: "T04"
title: "Re-target backfill to chunk-grain eligibility and embedding"
status: "done"
depends_on: ["T01", "T02", "T03"]
implements: ["FR#6", "FR#15", "AC#5", "AC#7", "AC#12", "AC#15"]
---

## Summary

Re-target the opt-in embedding backfill from branch-summary embedding to chunk-grain embedding via
the same `embed_branch_chunks` code path. Widen the universe to drop the summary requirement
(summary-failed branches still have embeddable exchange text), make eligibility
"watermark-stale OR has a chunk row with no vector" (the heal clause that catches crash victims and
post-drop orphans), fetch each branch's messages (now including `m.uuid`) to feed
`embed_branch_chunks`, distinguish content errors from batch-abort infra errors, and report
progress/ETA in **inferences** (one per exchange) rather than branches.

## Target Files

- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- modify: `tests/test_backfill_embeddings.py`
- read: `src/ccrecall/db.py`
- read: `src/ccrecall/session_ops.py`
- read: `src/ccrecall/embeddings.py`
- read: `design/specs/001-chunk-level-embeddings/design.md`
- read: `design/specs/001-chunk-level-embeddings/tasks/context.md`

## Prompt

Implement per design.md `## Architecture → (3) Backfill` and `## Replacement Targets`
(`build_selection` replaced by chunk eligibility). Preserve the batch loop, two-level failure model,
`--threads`/`--days`/`--limit`, nice-level, no-progress abort, and content-error sentinel.

1. **Universe — drop the summary requirement.** Chunk embedding reads raw exchange text, not the
   summary, so define a `CHUNK_EMBEDDABLE_BRANCH_FILTER` = active leaf with at least one message
   (`is_active = 1 AND EXISTS(SELECT 1 FROM branch_messages WHERE branch_id = branches.id)`), NOT
   the inherited `EMBEDDABLE_BRANCH_FILTER` (which requires a non-empty `context_summary`). Put the
   new constant alongside the old one (the old filter stays for summary-dependent callers — do not
   remove it). Use `CHUNK_EMBEDDABLE_BRANCH_FILTER` in `build_selection` and `count_status`.

2. **`build_selection` — chunk eligibility** = **watermark-stale OR heal clause**, excluding the
   content-error sentinel:
   - watermark-stale: `embedding_version IS NULL OR embedding_version < ? OR embedding_model IS NOT ?`
     (this is what makes the whole corpus eligible after the version bump — it includes the
     version-stale chunks the write path deliberately skips). **Drop `summary_version_at_embed` from
     the predicate** — it is vestigial for the chunk path (chunk staleness is `content_hash` +
     `EMBEDDING_VERSION`).
   - heal clause (the generalization of today's `OR NOT EXISTS (… branch_vec …)`):
     `OR EXISTS (SELECT 1 FROM chunks c WHERE c.branch_id = branches.id AND NOT EXISTS (SELECT 1 FROM chunk_vec WHERE chunk_id = c.id))`
   - keep `embedding_version IS NOT {CONTENT_ERROR_VERSION}` exclusion and the `--days` recency bound.
   - params become `[EMBEDDING_VERSION, EMBEDDING_MODEL]` (no `SUMMARY_VERSION`).

3. **Per-branch message fetch** (challenge T1 + M10). The current loop selects
   `(id, context_summary)` and embeds the summary. The new loop selects branch ids, then for each
   calls `fetch_branch_messages(cursor, branch_id, include_notifications=False)` (now returning
   `m.uuid`) to supply `branch_msgs` to `embed_branch_chunks`. A `sqlite3.Error` during this fetch
   is a **batch-abort** failure (let it propagate to the outer `except Exception` → EXIT_ABORT),
   **NOT** a content-error sentinel — the two must not be conflated.

4. **Per-branch embed** calls the **same** `embed_branch_chunks` (one code path) inside the existing
   per-branch SAVEPOINT. Because it **raises**, catch `(ValueError, OverflowError, UnicodeError)` in
   the SAVEPOINT handler to mark `CONTENT_ERROR_VERSION` on the branch watermark (marked once,
   skipped thereafter), exactly as the summary path did. Pass `is_active=True` (the universe is
   active leaves) and `vec_writable=True` (guarded by the existing `branch_vec_queryable`/now
   `chunk_vec_queryable` abort check at the top of `run`).

5. **Status/ETA counts inferences, not branches** (challenge M21). Each branch contributes N
   inferences (one per exchange). Add a `total_inferences` counter alongside `total_updated`
   (branches) and report both (e.g. "N exchanges embedded across M/total branches"). `count_status`
   reports chunk coverage (current-version chunks / total chunks) rather than branch coverage —
   keep the `done`/`eligible`/`errored` partition but compute over chunk readiness where the design
   specifies it. Keep `--status [--json]` working and abort paths exiting non-zero.

6. **Tests (`tests/test_backfill_embeddings.py`)** — adapt `build_selection`/status tests to chunk
   eligibility and add:
   - **Version-bump eligibility (AC#5):** bumping `EMBEDDING_VERSION` makes all active-leaf branches
     eligible; after a backfill run, every active-leaf exchange has a current-version chunk vector.
   - **Heal clause / crash victim (AC#12):** seed a `chunks` row with **no** `chunk_vec` while the
     branch watermark reads `EMBEDDING_VERSION` → the heal clause re-selects that branch.
   - **Content-error vs batch-abort (challenge T1):** a branch whose embed raises a content error is
     marked `CONTENT_ERROR_VERSION` once and skipped next pass (not looped to the no-progress abort);
     a `sqlite3.Error` during the per-branch message fetch aborts the batch (EXIT_ABORT) instead of
     marking the sentinel.
   - **Backfill locator (challenge M10):** backfilled chunks get a non-NULL `first_message_uuid`
     (because `fetch_branch_messages` now selects `m.uuid`).
   - **Summary-failed branch (AC#15):** a branch with `context_summary = NULL` is selected by
     `CHUNK_EMBEDDABLE_BRANCH_FILTER`, chunk-embedded, and its chunks are searchable.
   - **History preservation (AC#7):** row counts/contents of `messages`/`branches`/`branch_messages`
     are unchanged across a version bump + backfill (integration-style, seeded DB).
   - Keep the existing two-level failure model and no-progress-abort tests.

## Focus

- `build_selection` is at `backfill_embeddings.py:65-92`; the heal clause it generalizes is the
  `OR NOT EXISTS (... branch_vec ...)` at `:84-85`. `count_status` is at `:95-141`; the batch loop
  at `:277-352` selects `(id, context_summary)` at `:285` — this is what changes to a per-branch
  message fetch + `embed_branch_chunks`.
- The two-level failure model (`:308-333`): per-row content errors marked & continued; infra/session
  failure aborts. The new fetch-error path is **batch-abort** — make sure it lands in the outer
  `except Exception` (or re-raise), not the inner `(ValueError, OverflowError, UnicodeError)` catch.
- The design **rejected** comparing a chunk *count* to `branches.exchange_count` — they are computed
  over different inputs (`compute_branch_metadata` over raw JSONL vs `build_exchange_pairs` over
  fetched rows) and not guaranteed equal. Use the `NOT EXISTS` heal clause, not a count test.
- `branch_vec_queryable` abort guard at `:249` — switch this to `chunk_vec_queryable` (the chunk
  path is what this backfill now writes). Both exist post-T01; the chunk-accurate name is correct
  here. The `run_status` guard at `:165` likewise.
- The backfill is opt-in (`ccrecall backfill embeddings`), runs nice-leveled and off the hot path;
  the one-time corpus re-embed is now ≈ exchanges-per-session × larger — that is expected and
  acceptable (design.md `## Impact → Blast Radius`).

## Verify

- [ ] FR#6: Bumping `EMBEDDING_VERSION` makes the entire active-leaf corpus eligible; a backfill run
      re-embeds it at chunk granularity (AC#5).
- [ ] FR#15: The backfill re-selects any branch with a `chunks` row lacking a `chunk_vec` (crash
      victims, post-drop orphans) independent of the watermark (AC#12).
- [ ] AC#5: After an `EMBEDDING_VERSION` bump, all active-leaf branches are eligible; post-backfill,
      every active-leaf exchange has a current-version chunk vector.
- [ ] AC#7: `messages`/`branches`/`branch_messages` row counts and contents are unchanged across a
      version bump + backfill.
- [ ] AC#12: A branch with a `chunks` row whose `chunk_vec` is missing is re-selected by backfill
      even when its watermark reads `EMBEDDING_VERSION`.
- [ ] AC#15: A branch with `context_summary = NULL` is chunk-embedded (via
      `CHUNK_EMBEDDABLE_BRANCH_FILTER`) and searchable.
