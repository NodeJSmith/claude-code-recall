---
task_id: "T03"
title: "Maintain enrichment source fingerprints"
status: "done"
depends_on: ["T01", "T02"]
implements: ["FR#1", "FR#12", "AC#1"]
---

## Summary

Keep enrichment freshness accurate whenever deterministic branch inputs change.
Populate `summary_source_hash` during normal sync and deterministic backfill, and invalidate it when summary-producing inputs become unreliable or change.

## Target Files

- modify: `src/ccrecall/branch_ops.py`
- modify: `src/ccrecall/embed_ops.py`
- modify: `src/ccrecall/hooks/backfill_summaries.py`
- modify: `src/ccrecall/hooks/backfill_tool_content.py`
- modify: `tests/test_summarizer.py`
- modify: `tests/test_backfill_tool_content.py`
- read: `src/ccrecall/summarizer.py`

## Prompt

Implement the `summary_source_hash` maintenance rules from `Data model`. Reuse T02's canonical serializer rather than creating per-caller hash assembly. The fingerprint covers leaf UUID, deterministic summary version/JSON, aggregated-content hash, exchange metadata, files, commits, tools, and git branch; it excludes source paths and does not require rendering-path recomputation.

Update the active sync path in `branch_ops.py`/`embed_ops.py`, deterministic summary backfill, and tool-content backfill. Invalidate or clear the fingerprint before/alongside updates that change `aggregated_content`, linked messages, or deterministic summary state; write the new fingerprint only after deterministic summary recomputation succeeds. A summary failure must leave the hash NULL so an old enrichment becomes non-renderable. Do not call Claude here or alter existing deterministic output.

## Focus

`sync_branch()` recomputes metadata, links, aggregate content, then calls `write_branch_summary()`. `backfill_tool_content.py` currently rebuilds aggregate content and clears `summary_version`; it must also invalidate enrichment freshness. Keep transaction ownership where it is: sync/import calls remain atomic and deterministic backfill commits in its existing batch cadence. The result is an AC#1 safeguard: turning enrichment off or failing it cannot change deterministic summary behavior.

## Verify

- [ ] FR#1: Deterministic sync and backfill tests retain their existing summary values and versions when no enrichment exists.
- [ ] FR#12: Tests show hash creation after successful deterministic summary writes, invalidation on content/tool backfill changes, and NULL hash after deterministic summary failure.
- [ ] AC#1: Existing deterministic summary and tool-content regression tests pass with no enrichment-enabled change to stored summary fields or versions.
