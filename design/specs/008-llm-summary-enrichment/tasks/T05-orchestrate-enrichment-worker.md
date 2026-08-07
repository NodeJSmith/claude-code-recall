---
task_id: "T05"
title: "Orchestrate the enrichment worker"
status: "planned"
depends_on: ["T01", "T02", "T03", "T04"]
implements: ["FR#2", "FR#9", "FR#10", "FR#12", "AC#4"]
---

## Summary

Add the detached LLM summary worker that selects eligible branches, coordinates source/Claude work, and writes only validated current enrichment results.
It owns PID exclusion, status transitions, optimistic source-hash writes, and transaction boundaries.

## Target Files

- create: `src/ccrecall/hooks/backfill_llm_summaries.py`
- create: `tests/test_backfill_llm_summaries.py`
- read: `src/ccrecall/hooks/backfill_summaries.py`
- read: `src/ccrecall/hooks/backfill_embeddings.py`
- read: `src/ccrecall/llm_summary_db.py`
- read: `src/ccrecall/llm_summarizer.py`
- read: `src/ccrecall/summary_enrichment.py`

## Prompt

Implement `Worker placement`, `Eligibility`, status policy, compare-and-swap persistence, and stale packet reaping. Add a synchronous `run()` entry that loads settings/logging under `backfill-llm-summary`, acquires its own PID key, selects active deterministic-current branches, and commits per branch or small batch.

Read branch metadata/messages/source provenance and establish the expected `summary_source_hash` before creating a packet. For a migrated row with a current deterministic summary but `summary_source_hash IS NULL`, compute the canonical T02 hash from the current branch inputs, persist it before packet construction, then re-read/capture that value as the expected compare-and-swap guard. If a current hash cannot be produced because deterministic summary state is invalid, leave it NULL and do not invoke Claude. Close the read/write transaction before invoking Claude. After a valid response, reopen a lightweight DB connection and write the worker-owned stored envelope only with `WHERE id = ? AND summary_source_hash = ?`; discard responses when this affects zero rows. Apply the same compare-and-swap condition to status writes where appropriate so an older invocation cannot overwrite newer state.

Implement all documented eligibility and failure statuses. Do not auto-retry `invalid_output` or branch-level `budget_exceeded`; only `--force` may reselect them. Capability-sidecar failures follow their separate rerun-check path. The worker must be usable by both the manual CLI and the detached sync-current spawn without importing heavy embedding dependencies.

## Focus

Follow the `backfill_summaries.py` batch/cleanup shape and `backfill_embeddings.py` distinction between per-row and run-level failures, but do not copy embedding imports or model checks. The worker is a correctness boundary: no open write transaction may remain during a Claude call, no stale result may replace a newer fingerprint, and deterministic summary fields must never be mutated on any LLM failure.

## Verify

- [ ] FR#2: Worker persists only separate enrichment columns/envelopes and never rewrites deterministic summary fields.
- [ ] FR#9: Mocked Claude calls occur after the read transaction closes; DB writes reopen afterward.
- [ ] FR#10: Failure/status tests preserve deterministic data and distinguish retryable, force-only, and terminal selection states.
- [ ] FR#10: PID tests prove a live worker skips rather than queues, stale markers are reaped, and cleanup runs on worker success and failure.
- [ ] FR#12: A source-hash change while Claude runs makes the final write a no-op and keeps stale enrichment hidden.
- [ ] FR#12: A migrated deterministic-current row with NULL `summary_source_hash` receives the canonical persisted hash before packet construction; invalid deterministic state remains NULL and skips Claude invocation.
- [ ] FR#12: A source-hash change while Claude runs also prevents an older success or failure status write from overwriting the newer branch state.
- [ ] AC#4: Worker-level mocked subprocess tests preserve concise diagnostics and cover all classified exits.
