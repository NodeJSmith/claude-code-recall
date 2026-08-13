---
task_id: "T06"
title: "Harden recap provider execution"
status: "done"
depends_on: ["T03", "T04", "T05"]
implements: ["FR#16", "FR#17", "FR#18", "FR#24", "FR#26", "AC#10", "AC#11", "AC#19", "AC#21"]
---

## Summary

Replace JSONL packet/capability machinery with canonical DB-packet invocation and a crash-safe POSIX process-group lifecycle. Commit packet/process ownership before use, quarantine cleanup uncertainty, and gate unsupported platforms and quarantine capacity.

## Target Files

- modify: `src/ccrecall/llm_summarizer.py`
- read: `src/ccrecall/recap_input.py`
- read: `src/ccrecall/recap_state.py`
- modify: `tests/test_llm_summarizer.py`
- create: `tests/fixtures/process_tree_child.py`

## Prompt

Implement "Provider boundary and health" and FR#24 launch ownership. Remove source resolution, JSONL packet assembly, capability sidecar/smoke test, citations, and file-evidence checks. Consume only a pre-reserved attempt and canonical DB projection. Create packet at its deterministic owner path, launch Claude as POSIX process-group leader, persist leader PID/PGID and OS start identity immediately, then mark running. Timeout performs TERM, grace, KILL, wait/reap before ordinary timeout/deletion. Ambiguous launch identity or teardown/deletion failure becomes `cleanup_failed`, blocks replacement, and quarantines owner-protected packet metadata. Enforce quarantine count/byte admission limits and unsupported-platform blocking.

## Focus

The crash between spawn and process-identity persistence must never be treated as never launched. A reservation can be cancelled only when non-launch is proven. Recovery needs exact PID/PGID/start identity to avoid PID reuse. No force purge exists while liveness is uncertain. Keep DB closed during Claude execution and never log/persist content or raw output.

## Verify

- [ ] FR#16: Real POSIX fixture proves TERM/grace/KILL/wait leaves no child or grandchild.
- [ ] FR#17: Teardown/deletion uncertainty records cleanup failure, blocks replacement, and retains protected quarantine metadata.
- [ ] FR#18: Unsupported-platform tests make no provider call and return a stable platform block reason.
- [ ] FR#24: Packet exists only after committed reservation; ambiguous spawn crashes cannot overlap a replacement process group.
- [ ] FR#26: Count/byte ceilings pause admission and oldest age remains warning-only.
- [ ] AC#10: Real process-tree and injected cleanup-failure tests pass locally on POSIX.
- [ ] AC#11: Platform-gating tests preserve deterministic operation.
- [ ] AC#19: Crash-window tests prove attributable packets and no concurrent provider groups.
- [ ] AC#21: Quarantine status metadata and safe maintenance preconditions are exercised.
