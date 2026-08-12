---
task_id: "T01"
title: "Audit meaningful-session eligibility"
status: "done"
depends_on: []
implements: ["FR#14", "AC#8"]
---

## Summary

Run the private local labeling audit that must precede eligibility policy implementation. Commit only de-identified aggregate methodology and results, including selected policy version, thresholds, measures, and stable reason codes. Keep all transcript-derived private material outside the repository.

## Target Files

- create: `design/specs/010-session-recaps/eligibility-audit.md`
- create: `tools/check_recap_evidence.py`
- read: `design/specs/010-session-recaps/design.md`
- read: `src/ccrecall/config.py`
- read: `src/ccrecall/summary_enrichment.py`
- read: `src/ccrecall/summarizer.py`
- read: `tests/fixtures/llm_summary_evaluation/manifest.json`

## Prompt

Follow the design's "Eligibility, CLI, and status" and AC#8. Build a reproducible private sample across session length, prose/tool activity, files/commits, and planning/implementation-like strata. Label meaningful/not meaningful/uncertain locally, compare simple deterministic rules prioritizing useful-session recall, and write only aggregate counts, methodology, selected thresholds, `ELIGIBILITY_POLICY_VERSION`, and reason codes to the committed artifact. Do not include excerpts, paths, UUIDs, raw feature rows, or reviewer notes. Do not change application constants in this task.

## Focus

The current product uses `llm_summary_min_exchanges = 9`; do not silently retain it. Measures available in imported DB state include role prose, `tool_content`, tool counts, files, commits, exchange count, and timestamps. Repetitive tool calls must not qualify trivial work by themselves. This evidence output is a hard dependency of T07.

## Verify

- [ ] FR#14: `eligibility-audit.md` defines one versioned, explainable policy with stable measures/reason codes and no private content.
- [ ] AC#8: `uv run python tools/check_recap_evidence.py design/specs/010-session-recaps/eligibility-audit.md` rejects transcript excerpts, paths, UUID-shaped identifiers, and private label rows while requiring strata, rule comparison, and selected thresholds.
