---
task_id: "T05"
title: "Build durable recap lifecycle state"
status: "done"
depends_on: ["T02", "T03"]
implements: ["FR#9", "FR#10", "FR#11", "FR#12", "FR#19", "FR#22", "FR#25", "AC#5", "AC#6", "AC#12", "AC#15", "AC#20"]
---

## Summary

Implement SQLite jobs, fenced claims, attempts, global runtime lease, provider cooldown, durable run membership/accounting, bounded retries, and retention queries. This is the state authority used later by hooks, drainer, CLI, and status.

## Target Files

- create: `src/ccrecall/recap_state.py`
- modify: `src/ccrecall/config.py`
- create: `tests/test_recap_state.py`
- modify: `tests/test_backfill_llm_summaries.py`

## Prompt

Implement the design's "Persistent state," "Job and attempt transitions," "Global drainer and cooldown," "Accounting," and "Retention" APIs. Use monotonic tokens and conditional updates for every heartbeat/transition/terminal/cleanup acknowledgement. Separate job claim from provider admission; support `reserved`, `running`, terminal attempt states, and content-dependent versus stable blocks. Implement singleton runtime lease, provider cooldown with capped exponential backoff/retry-after, immutable run-candidate snapshots and run-owned attempt limits, one automatic timeout retry per unchanged input, targeted retry lineage, indexed latest-state queries, and bounded lineage-preserving pruning selection.

## Focus

Do not invoke Claude or create packet files in this module. One attempt may belong to at most one run even when one coalesced job satisfies multiple snapshots. Global failures must not stamp branches. Lease expiry cannot itself authorize replacement launch; the process boundary supplies cleanup proof later. Diagnostics are capped and content-free. Keep boundaries lightweight and embedding-free.

## Verify

- [ ] FR#9: Repeated requests coalesce into one session job without losing newer generation identity.
- [ ] FR#10: Old-token heartbeat/write/terminal/cleanup mutations are rejected after reclaim.
- [ ] FR#11: Runtime lease tests admit one global owner and leave contended work durable.
- [ ] FR#12: Cooldown tests defer all jobs, admit one post-expiry probe, reset on success, and avoid branch failure state.
- [ ] FR#19: Run snapshots, final dispositions, limits, coalescing, and abort deferral reconcile exactly.
- [ ] FR#22: Indexed latest queries and pruning preserve required current/latest/retry lineage.
- [ ] FR#25: Repeated-run tests allow one timeout retry, give unchanged-input `unusable_output`, `budget_exceeded`, and `cleanup_failed` zero automatic retries, block each exhausted outcome, and reset only on new input or targeted retry.
- [ ] AC#5: Concurrency tests prove one claim/invocation admission and fencing of every late state transition.
- [ ] AC#6: Repeated cooldown/global-failure scenarios converge without calls before retry time.
- [ ] AC#12: Mixed-population accounting has immutable denominators and no double ownership.
- [ ] AC#15: Large synthetic history uses declared access paths and bounded pruning.
- [ ] AC#20: Repeated unchanged timeout stops after one retry; unusable output, budget exceeded, and cleanup failed stop after their first attempt; targeted/new-input resets create new lineage.
