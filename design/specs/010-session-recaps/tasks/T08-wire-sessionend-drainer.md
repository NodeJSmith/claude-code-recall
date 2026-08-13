---
task_id: "T08"
title: "Wire SessionEnd and serialized drainer"
status: "done"
depends_on: ["T03", "T04", "T05", "T06", "T07"]
implements: ["FR#5", "FR#7", "FR#8", "FR#9", "FR#10", "FR#11", "FR#13", "AC#2", "AC#4", "AC#5", "AC#7", "AC#18", "AC#19"]
---

## Summary

Move automatic recap generation from Stop to a lightweight SessionEnd coordinator and serialized detached drainer. Implement per-session fallback replay journaling, final sync, fenced orchestration, recovery, and guarded materialization while preserving hook contracts.

## Target Files

- create: `src/ccrecall/hooks/session_end.py`
- create: `src/ccrecall/hooks/drain_session_recaps.py`
- modify: `src/ccrecall/hooks/sync_current.py`
- modify: `src/ccrecall/hooks/memory_setup.py`
- modify: `src/ccrecall/hooks/clear_handoff.py`
- modify: `hooks/hooks.json`
- modify: `pyproject.toml`
- modify: `tests/test_clear_handoff_contract.py`
- modify: `tests/test_sync_hook.py`
- create: `tests/test_session_recap_drainer.py`
- modify: `tests/test_db.py`

## Prompt

Implement "Durable finalization and serialization" and "Recovery and convergence." SessionEnd writes clear handoff when applicable, then uses the no-migrate bounded upsert; on absent, outdated, partial, or busy DB/schema it atomically merges one versioned per-session fallback journal entry and fsyncs where supported. Intent precedes best-effort detached spawn and stdout is always `{}`. Every drainer first replays a bounded journal batch DB-upsert-before-delete, obtains global lease, coordinates/factors final sync without hook stdout, evaluates eligibility/current/cooldown, captures input, admits provider, and token/hash/version-guards materialization. Stop no longer spawns recaps. Recovery proves process cleanup before reclaim and handles overdue state idempotently.

## Focus

Hooks must not import the provider boundary, migrate, wait, or load embedding-heavy modules beyond existing Stop behavior. Malformed fallback entries are quarantined; markers are replay journal only, never ordinary ownership. Preserve `/clear` handoff ordering. On unsupported platforms, record stable blocked intent and suppress provider drainer detachment. Keep DB closed around provider execution.

## Verify

- [ ] FR#5: Drainer integration tests independently change active branch identity, claim token, recap contract, and recomputed input hash and prove every stale result is discarded without replacing the prior recap.
- [ ] FR#7: Sonnet remains default/opt-in and only SessionEnd initiates automatic recap work.
- [ ] FR#8: DB-ready input commits a job; absent, outdated, partial, or busy DB/schema writes a per-session fallback entry; invalid session writes neither; spawn failure retains committed intent; every path emits exactly `{}` without waiting.
- [ ] FR#9: Per-session DB/fallback coalescing survives concurrent events and failed spawn.
- [ ] FR#10: Drainer heartbeats, materialization, terminalization, and cleanup acknowledgements are token-fenced.
- [ ] FR#11: Automatic, recovery, and later manual work share one serialized runtime lease.
- [ ] FR#13: Explicit/drainer recovery replays journals and reconciles overdue cleanup idempotently.
- [ ] AC#2: The assembled capture/invoke/write flow proves packet/hash snapshot equivalence and rejects materialization after any guarded input changes.
- [ ] AC#4: Hook contract tests prove no Stop inference, no SessionEnd migration/provider import, and exact stdout.
- [ ] AC#5: Concurrent drainer tests yield one provider admission and reject late claimant writes.
- [ ] AC#7: Recovery tests cover expired claims, malformed/replayed markers, and exact guidance state.
- [ ] AC#18: Full hook/import invariant tests and lint checks pass for touched hook modules.
- [ ] AC#19: Assembled drainer recovery never overlaps an unverified old provider group.
