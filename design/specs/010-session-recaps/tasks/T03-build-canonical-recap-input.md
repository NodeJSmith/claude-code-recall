---
task_id: "T03"
title: "Build canonical DB recap input"
status: "done"
depends_on: ["T01", "T02"]
implements: ["FR#3", "FR#4", "FR#23", "AC#2", "AC#3", "AC#16"]
---

## Summary

Create the sole DB recap projection and hash boundary. Serialize the exact canonical projection from one SQLite snapshot, persist current input identity during import, atomically invalidate content-dependent jobs, and support controlled full reimport repair.

## Target Files

- create: `src/ccrecall/recap_input.py`
- modify: `src/ccrecall/branch_ops.py`
- modify: `src/ccrecall/session_ops.py`
- modify: `src/ccrecall/message_ops.py`
- modify: `src/ccrecall/hooks/backfill_tool_content.py`
- read: `src/ccrecall/summarizer.py`
- read: `src/ccrecall/summary_enrichment.py`
- create: `tests/test_recap_input.py`
- modify: `tests/test_import_pipeline.py`
- modify: `tests/test_session_ops.py`
- modify: `tests/test_backfill_tool_content.py`
- modify: `tests/test_summarizer.py`

## Prompt

Implement "Canonical DB recap input." Load active branch/session/project metadata, deterministic summary, and ordered active non-notification messages by `branch_messages.position`. Include role, timestamp, origin, UUID/parent identity available in imported schema, `content`, and `tool_content`. Canonicalize explicit nulls and decoded JSON with fixed UTF-8/sorted-key/fixed-separator serialization; hash exactly the object written to the owner-only packet. Use the policy version frozen in T01's audit artifact; do not invent a placeholder threshold or version. Import must persist current hash and current contract/policy versions after membership/order, metadata, and deterministic summary finalize. When identity changes, atomically reset only content-dependent terminal job states, preserving `platform_unsupported`. Add a controlled repair/full-reimport path for existing message corrections.

## Focus

Never read source files here. `summary_source_hash` remains deterministic-summary freshness and cannot substitute for recap input identity. Tool-only turns must survive. Packet bytes and hash bytes must derive from the same in-memory projection copied inside one transaction; provider execution occurs later. Preserve unchanged embedding/chunk behavior when sessions continue.

## Verify

- [ ] FR#3: Tests prove recap projection never resolves or reads transcript JSONL and excludes inactive, notification, external, and superseded messages.
- [ ] FR#4: Tests prove the exact canonical packet object produces the stored versioned input hash.
- [ ] FR#23: Continued/full-reimport tests stale recap identity, reset eligible job state, and preserve unchanged embeddings.
- [ ] AC#2: Snapshot tests vary content, tool content, order, metadata, and versions while source paths do not affect hash.
- [ ] AC#3: Tool-only and exclusion fixtures pass.
- [ ] AC#16: Continued-session integration coverage retains old payload and makes refresh eligible without vector churn.
