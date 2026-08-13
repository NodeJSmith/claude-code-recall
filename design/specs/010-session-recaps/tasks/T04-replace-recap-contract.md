---
task_id: "T04"
title: "Replace the recap contract and renderer"
status: "done"
depends_on: ["T02", "T03"]
implements: ["FR#1", "FR#2", "FR#6", "AC#1"]
---

## Summary

Replace the v1 continuation contract with a loose v2 Session Recap envelope and hash/version-aware rendering. Preserve deterministic fallback and additive search presentation without changing ranking.

## Target Files

- modify: `src/ccrecall/summary_enrichment.py`
- modify: `src/ccrecall/hooks/session_selection.py`
- modify: `src/ccrecall/hooks/context_rendering.py`
- modify: `src/ccrecall/search_hydrate.py`
- modify: `tests/test_summary_enrichment.py`
- modify: `tests/test_context_injection.py`
- modify: `tests/test_search.py`

## Prompt

Implement "Minimal recap contract." The model body requires bounded non-empty `summary`; optional bounded `title` and normalized `outcome` are dropped/normalized when defective. Ignore unknown fields and reject only unparseable/missing-unusable core output. Worker-owned metadata includes v2, model, generation time, attempt ID, input hash, input-contract version, and eligibility-policy version. Rendering requires successful v2 plus current/materialized hash and code-version matches, emits `### Session Recap`, and keeps deterministic context below. Search hydration may add title/summary preview only; do not alter deterministic topic or ranking.

## Focus

`summary_enrichment.py` is imported on the SessionStart hot path and must stay dependency-light. Release-time contract/policy changes must fail closed even before reimport. Preserve v1 bytes physically but never interpret them as v2. Remove citation/file-evidence/continuation validation rather than leaving dead parallel schema code.

## Verify

- [ ] FR#1: Unit tests accept useful minimal summaries, normalize/drop optional defects, and bound all display fields.
- [ ] FR#2: Prompt/contract tests prohibit handoff, advice, exhaustive chronology, and unsupported project-wide claims while retaining a recognizable work arc.
- [ ] FR#6: Rendering tests compare stored hashes/versions only and fall back deterministically for stale, malformed, failed, unsupported, or v1 data.
- [ ] AC#1: `uv run pytest tests/test_summary_enrichment.py` passes with v2 normalization/render/fallback coverage.
