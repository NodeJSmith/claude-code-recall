---
task_id: "T09"
title: "Build recap CLI status and lifecycle"
status: "planned"
depends_on: ["T05", "T06", "T07", "T08"]
implements: ["FR#12", "FR#13", "FR#15", "FR#18", "FR#19", "FR#20", "FR#22", "FR#25", "FR#26", "AC#6", "AC#7", "AC#9", "AC#11", "AC#12", "AC#13", "AC#15", "AC#20", "AC#21"]
---

## Summary

Replace the old direct backfill/capability CLI with queue-backed Session Recap selection, targeted retry, recovery, provider-health reset, maintenance, read-only status, and reconcilable output. Validate the assembled lifecycle over realistic repeated runs.

## Target Files

- modify: `src/ccrecall/hooks/backfill_llm_summaries.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `src/ccrecall/status.py`
- modify: `src/ccrecall/config.py`
- modify: `tests/test_backfill_llm_summaries.py`
- modify: `tests/test_llm_summary_cli.py`
- modify: `tests/test_status.py`
- modify: `tests/test_integration.py`

## Prompt

Implement "Eligibility, CLI, and status," accounting, recovery, and retention UX. Keep `backfill llm-summaries` compatibility while using Session Recap terminology. Remove `--check-capability` and broad `--force`; add targeted failure retry and one-run model/budget/timeout selectors plus session/day/attempt limit. Add `recap recover`, `reset-health`, and `maintain` (dry-run by default) thin commands. Manual runs create immutable run membership and enqueue into the shared drainer. Status introspects schema before recap queries and reports platform, policy/contracts, Sonnet defaults, cooldown, jobs/overdue, current/stale/legacy, attempts, quarantine limits/age, and exact commands without writing or importing provider/embedding-model boundaries.

## Focus

Global abort makes the run incomplete and finalizes unresolved membership as deferred. `--limit` caps run-owned started attempts, not discovery. Targeted retry cannot bypass cooldown/platform/input/cleanup safety and never persists overrides. Unsupported platform jobs are blocked, not runnable pending. Maintenance cannot purge quarantine while liveness is uncertain. Use deterministic fakes for provider lifecycle tests.

## Verify

- [ ] FR#12: CLI/status expose shared cooldown and no transcript branch failure for global abort.
- [ ] FR#13: Recovery command and read-only overdue guidance reconcile without daemon claims.
- [ ] FR#15: Removed flags are absent; retry/overrides validate and leave config unchanged.
- [ ] FR#18: Human/JSON output explains unsupported-platform blocks and excludes them from runnable counts.
- [ ] FR#19: Population/final dispositions/attempt outcomes reconcile under limits, coalescing, stale result, abort, and zero-work rerun.
- [ ] FR#20: Status reports unavailable/outdated/partial schema before running recap SQL.
- [ ] FR#22: Maintenance dry-run/prune and status latest queries remain bounded and lineage-safe.
- [ ] FR#25: Blocked retry exhaustion and exact targeted reset commands are visible and effective.
- [ ] FR#26: Quarantine count/bytes/oldest age and admission pause appear in human/JSON output.
- [ ] AC#6: Repeated global failure records the current attempt as `global_abort`, leaves remaining jobs pending, stamps no branch failure, blocks calls before `retry_after`, admits one probe afterward, and resets health on success.
- [ ] AC#7: Recovery/status idempotence and guidance tests pass.
- [ ] AC#9: Cyclopts help/validation and settings immutability tests pass.
- [ ] AC#11: Unsupported-platform output/provider suppression tests pass.
- [ ] AC#12: Mixed realistic population accounting reconciles across repeated runs.
- [ ] AC#13: Pre-recap/partial/current read-only status fixtures pass.
- [ ] AC#15: Large-history maintenance/query tests pass.
- [ ] AC#20: Assembled runs allow one timeout retry, zero automatic retries for unusable output/budget exceeded/cleanup failed, then expose targeted/new-input reset and converge with zero unchanged calls.
- [ ] AC#21: Quarantine capacity and safe maintenance scenarios pass.
