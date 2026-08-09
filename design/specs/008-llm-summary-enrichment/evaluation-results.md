# LLM Summary Enrichment Evaluation Results

**Corpus:** `tests/fixtures/llm_summary_evaluation/`

**Scope:** de-identified manual review of rendered Branch Resume Briefs and stored enrichment envelopes generated from the synthetic bug-investigation, implementation-refactor, and planning-discovery scenarios.

**Important:** This is a local acceptance record for synthetic fixtures. It is not a release benchmark, provider SLA, or user-data report.

## Overall verdict

- **Sonnet synthetic-corpus gate:** pass
- **Blocked release issue found during this review:** none
- **Follow-up required on future prompt/schema changes:** rerun the full procedure in [`evaluation.md`](./evaluation.md)

## Scenario verdicts

### bug-investigation

| Category | Coverage | Unsupported claims | UUID membership | Citation entailment | Notes |
|---|---|---|---|---|---|
| Latest state | Pass | Pass | Pass | Pass | Rendered brief preserved the current fix status and the missing crash-recovery check. |
| Causal history | Pass | Pass | Pass | Pass | The brief kept the duplicate-row cause instead of collapsing directly to the final state. |
| Decision / rationale | Pass | Pass | Pass | Pass | The chosen checkpoint approach stayed paired with its rationale. |
| Attempted path | Pass | Pass | Pass | Pass | The abandoned savepoint path remained explicit. |
| Unresolved work | Pass | Pass | Pass | Pass | Remaining regression coverage was surfaced as unfinished work. |
| Handoff | Pass | Pass | Pass | Pass | The next-step handoff stayed concrete and non-generic. |

### implementation-refactor

| Category | Coverage | Unsupported claims | UUID membership | Citation entailment | Notes |
|---|---|---|---|---|---|
| Latest state | Pass | Pass | Pass | Pass | The brief reflected the extracted packet-builder state and the remaining integration gap. |
| Causal history | Pass | Pass | Pass | Pass | The path from the monolithic worker to the extraction landed clearly. |
| Decision / rationale | Pass | Pass | Pass | Pass | Shared source resolution remained tied to the rationale about drift prevention. |
| Attempted path | Pass | Pass | Pass | Pass | The partial in-place patch was preserved as an abandoned approach. |
| Unresolved work | Pass | Pass | Pass | Pass | Startup/import-guard work remained visible. |
| Handoff | Pass | Pass | Pass | Pass | The handoff pointed at the next validation step instead of generic cleanup. |

### planning-discovery

| Category | Coverage | Unsupported claims | UUID membership | Citation entailment | Notes |
|---|---|---|---|---|---|
| Latest state | Pass | Pass | Pass | Pass | The brief preserved the current evaluation-plan state and its remaining script gap. |
| Causal history | Pass | Pass | Pass | Pass | The rationale for synthetic local evaluation remained connected to the earlier privacy concern. |
| Decision / rationale | Pass | Pass | Pass | Pass | The manifest-first evaluation design stayed evidence-backed. |
| Attempted path | Pass | Pass | Pass | Pass | The dropped live-provider CI path remained explicit. |
| Unresolved work | Pass | Pass | Pass | Pass | The manual-results refresh requirement stayed visible. |
| Handoff | Pass | Pass | Pass | Pass | The handoff kept the rerun requirement for future prompt/schema changes. |

## Cross-scenario notes

- The stored envelopes remained the source-of-truth for UUID provenance; the rendered briefs were useful because the factual sections they showed all traced back to valid stored citations.
- No reviewed output introduced a factual claim that was unsupported by the manifest.
- The synthetic corpus was sufficient to exercise latest-state, causal-history, decision/rationale, attempted-path, unresolved-work, and handoff coverage in all three scenarios.

## Sonnet vs. Haiku motivation note

During the optional local synthetic-corpus comparison rerun documented in [`evaluation.md`](./evaluation.md), Sonnet produced more reliable decision/rationale retention and handoff specificity than Haiku, especially on the planning/discovery fixture. That observation is why `sonnet` remains the default. This note is design motivation only; it is not a benchmark and does not use user transcript data.
