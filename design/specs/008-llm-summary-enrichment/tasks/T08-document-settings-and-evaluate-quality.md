---
task_id: "T08"
title: "Document enrichment and evaluate quality"
status: "planned"
depends_on: ["T06", "T07"]
implements: ["FR#11", "AC#9", "AC#11"]
---

## Summary

Document the explicit opt-in, Claude-auth data boundary, settings, canonical command, and budget semantics.
Add a durable local evaluation procedure for factual quality and citation entailment before prompt/schema changes are accepted.

## Target Files

- modify: `README.md`
- create: `design/specs/008-llm-summary-enrichment/evaluation.md`
- create: `design/specs/008-llm-summary-enrichment/evaluation-results.md`
- create: `tests/fixtures/llm_summary_evaluation/bug-investigation.jsonl`
- create: `tests/fixtures/llm_summary_evaluation/implementation-refactor.jsonl`
- create: `tests/fixtures/llm_summary_evaluation/planning-discovery.jsonl`
- create: `tests/fixtures/llm_summary_evaluation/manifest.json`
- create: `tests/test_llm_summary_evaluation.py`
- read: `design/specs/008-llm-summary-enrichment/design.md`

## Prompt

Update README configuration and entry-point guidance for LLM enrichment. Document `llm_summaries_enabled`, `llm_summary_model` (default `sonnet`), effort, timeout, `$1.00` budget threshold, and minimum exchanges. Explain that enabling enrichment sends selected branch-scoped transcript content, branch/session metadata, and source-path provenance through the installed Claude CLI using the user's Claude auth. State the canonical `ccrecall backfill llm-summaries` and `--check-capability` flow, deterministic fallback, no-session-persistence gate, and that the budget is an upstream threshold rather than a guaranteed maximum cost.

Create three de-identified, synthetic long-branch transcript fixtures for bug investigation, implementation/refactor, and planning/discovery, plus a manifest containing each scenario's gold latest-state, causal-history, decision/rationale, attempted-path, unresolved-work, and handoff facts with UUID locators. The completed worker builds packets from these transcript fixtures. Add a local test that validates fixture/manifest shape and coverage, so the corpus cannot silently lose a required scenario or gold-fact category.

Create `evaluation.md` as the execution-time manual quality gate. It must direct the evaluator to run the completed worker against all three fixtures, record the rendered brief and stored-envelope result without copying transcript text, then compare each output against its manifest. Require explicit coverage, unsupported-claim, UUID-membership, and citation-entailment verdicts for every applicable gold fact. Create `evaluation-results.md` by completing that review for the three fixtures with de-identified pass/fail verdicts and remediation notes, not raw transcript or model output. Record the observed Sonnet/Haiku comparison as motivation, not as a release benchmark or user data.

## Focus

README currently promises that no data leaves the machine. The new documentation must qualify that statement precisely for this explicit opt-in feature without weakening the local deterministic baseline. Do not publish sample transcript content, raw LLM output, local paths, session UUIDs, or credentials in the repository. This is a manual acceptance procedure, not a flaky live-provider CI test.

## Verify

- [ ] FR#11: README explicitly states that opt-in sends selected local transcript content, branch/session metadata, and source-path provenance through Claude Code auth, with the capability prerequisite, deterministic fallback, and canonical manual command.
- [ ] AC#9: Three de-identified long-branch fixtures plus manifest cover bug, implementation/refactor, and planning/discovery; committed `evaluation-results.md` records completed rendered/stored review verdicts for coverage, unsupported claims, UUID membership, and citation entailment without copying real transcript/model text.
- [ ] AC#9: `evaluation.md` instructs the evaluator to run the worker on every fixture, compare each rendered/stored result to the manifest, and record all required verdicts in `evaluation-results.md`.
- [ ] AC#11: README documents the configurable `sonnet` default and `$1.00` threshold as non-guaranteed spend control.
