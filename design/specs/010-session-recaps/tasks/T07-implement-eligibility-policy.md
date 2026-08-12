---
task_id: "T07"
title: "Implement audited recap eligibility"
status: "planned"
depends_on: ["T01", "T02"]
implements: ["FR#14", "AC#8"]
---

## Summary

Implement the shared versioned meaningful-session evaluator from the completed audit artifact. Provide one query/evaluation boundary for automatic queueing, manual selection, and read-only status.

## Target Files

- create: `src/ccrecall/recap_eligibility.py`
- modify: `src/ccrecall/config.py`
- read: `design/specs/010-session-recaps/eligibility-audit.md`
- create: `tests/test_recap_eligibility.py`
- modify: `tests/test_backfill_llm_summaries.py`
- modify: `tests/test_status.py`

## Prompt

Translate the exact selected thresholds, `ELIGIBILITY_POLICY_VERSION`, measures, and reason codes from `eligibility-audit.md` into a lightweight deterministic evaluator. Absolute prerequisites are active session branch, current deterministic summary, and non-empty eligible imported messages. Return measures and stable reasons usable by worker/status; do not infer completion or satisfaction. Ensure worker selection and status aggregation call the same evaluator/query boundary.

## Focus

Do not choose or tune thresholds independently; T01 is authoritative. Repetitive tool activity must follow the audit decision. Keep the module free of provider, vec, fastembed, and JSONL source imports. Policy-version changes participate in recap input freshness and render fail-closed behavior.

## Verify

- [ ] FR#14: Automatic/manual/status callers receive the same versioned decision, measures, and reason codes for identical DB state.
- [ ] AC#8: Synthetic tests cover every audited policy branch, including short useful and long repetitive sessions, and match the aggregate artifact.
