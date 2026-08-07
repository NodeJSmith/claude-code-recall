---
task_id: "T02"
title: "Build enrichment contract and renderer"
status: "done"
depends_on: []
implements: ["FR#1", "FR#3", "FR#4", "FR#15", "FR#16", "FR#17", "AC#2", "AC#3", "AC#10"]
---

## Summary

Create the lightweight enrichment domain module: canonical source hashing, strict Claude response-body validation, worker-owned stored-envelope construction, freshness checks, and bounded Branch Resume Brief rendering.
This module is the only enrichment code imported by SessionStart rendering and must remain dependency-light.

## Target Files

- create: `src/ccrecall/summary_enrichment.py`
- create: `tests/test_summary_enrichment.py`
- read: `src/ccrecall/summarizer.py`
- read: `src/ccrecall/serialization.py`

## Prompt

Implement `Stored enrichment envelope`, `Claude response body schema`, `Rendering`, and the hash rules in the design. Define `SUMMARY_ENRICHMENT_VERSION`, status constants, `CLAUDE_RESPONSE_SCHEMA`, and a separate stored-envelope constructor. The response schema must require only factual brief fields and reject worker-owned `version`, `model`, and `generated_at`; the worker constructor supplies those fields after validation.

Validate field caps, exact object shapes, non-empty active-branch message UUID citations, attempted-path outcomes, allowed confidence values, and valid file references. Expose a single canonical SHA-256 source-hash serializer that normalizes decoded JSON, sorts object keys, preserves meaningful list order, and is shared by deterministic writers and the worker. Do not import DB, subprocess, hook entries, sqlite-vec, or embedding code.

Implement `valid_current_enrichment()` and rendering that emits a `### Branch Resume Brief` only for an `ok`, current, source-hash-matched envelope. Apply the 2,400-character primary and 800-character supplementary budgets without splitting an item into misleading prose. Keep deterministic markdown unchanged below the block, prioritize current state and evidenced continuation hints, and leave UUIDs as stored validation provenance rather than rendered prose.

## Focus

The new module is a SessionStart dependency. Test its import in a clean subprocess, as existing dependency-isolation tests do. Treat model output as untrusted despite `--json-schema`; the real trial showed model-produced timestamps cannot be trusted. The renderer receives `is_primary_session` from `context_rendering.py`; it must not query the DB or recompute large source fields. Test citation membership here; semantic citation entailment belongs to the manual evaluation in T08.

## Verify

- [ ] FR#1: Invalid, missing, failed, or stale envelopes return the deterministic markdown byte-for-byte.
- [ ] FR#1: A clean subprocess imports `summary_enrichment.py` without loading DB, subprocess, hook-entry, sqlite-vec, fastembed, or onnxruntime modules.
- [ ] FR#3: Valid response bodies are accepted and malformed, oversized, unknown-field, uncited, or worker-metadata-bearing bodies are rejected.
- [ ] FR#4: Only current-version `ok` envelopes with equal stored/current source hashes render enrichment.
- [ ] FR#4: Canonical hash tests prove decoded JSON key-order normalization is stable, meaningful list order changes alter the hash, and path-only provenance is excluded.
- [ ] FR#15: Renderer preserves latest state, causal history, decision rationale, attempted paths, and evidenced continuation hints when present.
- [ ] FR#16: Every stored factual field validates against active-branch UUID membership.
- [ ] FR#17: Primary and supplementary render budgets are enforced while deterministic context remains below the brief.
- [ ] AC#2: Unit tests cover valid enrichment composition and all deterministic-only fallback paths.
- [ ] AC#3: Unit tests cover the complete strict response schema and worker-owned envelope metadata.
- [ ] AC#10: Render tests prove the primary/supplementary budgets and required continuation-hint priority.
