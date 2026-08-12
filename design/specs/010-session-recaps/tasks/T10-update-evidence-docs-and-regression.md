---
task_id: "T10"
title: "Update recap evidence docs and regressions"
status: "planned"
depends_on: ["T04", "T08", "T09"]
implements: ["FR#2", "FR#3", "FR#7", "AC#17", "AC#18"]
---

## Summary

Commit de-identified model/input evaluation evidence, replace public Branch Resume Brief documentation with Session Recaps, update bundled skills and v1 supersession notes, and run the complete regression suite.

## Target Files

- create: `design/specs/010-session-recaps/evaluation.md`
- modify: `README.md`
- modify: `CHANGELOG.md`
- modify: `skills/ccr-recall/SKILL.md`
- modify: `skills/ccr-resume/SKILL.md`
- modify: `design/specs/008-llm-summary-enrichment/design.md`
- modify: `design/specs/008-llm-summary-enrichment/evaluation.md`
- modify: `design/specs/008-llm-summary-enrichment/evaluation-results.md`
- modify: `tests/test_llm_summary_evaluation.py`
- modify: `tools/check_recap_evidence.py`
- read: `design/specs/010-session-recaps/eligibility-audit.md`
- read: `design/specs/010-session-recaps/design.md`

## Prompt

Write only aggregate private evaluation methodology/results: DB versus JSONL input size/quality, Haiku/Sonnet completion and scored quality, selected DB-only authority, Sonnet default, and provisional timeout rationale. Include no transcripts, recap text, paths, UUIDs, prompts, raw outputs, private mappings, or reviewer notes. Update README command/config/data-flow/status/platform/privacy sections, bundled recall/resume guidance, changelog terminology, and v1 design/evaluation with a historical superseded pointer rather than rewriting history. Remove public instructions for capability checks, force, Stop spawning, JSONL recap packets, citations, and Branch Resume Brief rendering.

## Focus

Search found Branch Resume Brief and old flag references outside the design's original Impact list. Preserve the repository/package naming mismatch and existing release history. Session Recap is orientation, not authoritative continuation evidence; tail/pending-question workflows remain authoritative. Confirm documentation describes budget as an upstream stop threshold, not guaranteed maximum spend.

## Verify

- [ ] FR#2: Docs and skills consistently describe recognition recap rather than handoff/advice/continuation.
- [ ] FR#3: Public data-flow docs state DB-only recap input and contain no JSONL generation/source-readiness instructions.
- [ ] FR#7: Docs state opt-in SessionEnd generation and Sonnet default, with no Stop-generation claim.
- [ ] AC#17: `uv run python tools/check_recap_evidence.py design/specs/010-session-recaps/evaluation.md` accepts required aggregate results and rejects private paths, UUIDs, transcript/recap excerpts, prompts, and raw outputs.
- [ ] AC#18: `uv run pytest` and `uvx prek run --all-files` pass after all documentation, hook, DB, CLI, and regression changes.
