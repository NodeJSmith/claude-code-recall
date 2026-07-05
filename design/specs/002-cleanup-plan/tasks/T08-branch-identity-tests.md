---
task_id: "T08"
title: "Update tests for session-keyed branch identity"
status: "planned"
depends_on: ["T06", "T07"]
implements: ["AC#6"]
---

## Summary

Update all test files for the session-keyed branch identity changes from T07. Rewrite branch upsert tests, remove multi-branch detection tests, remove `enforce_single_active_branch` tests, and add a characterization test proving 10 sequential syncs produce exactly 1 branch row.

## Target Files

- modify: `tests/test_session_ops.py`
- modify: `tests/test_parsing.py`
- modify: `tests/test_sync_hook.py`
- modify: `tests/test_import_pipeline.py`
- modify: `tests/test_integration.py`
- modify: `tests/test_backfill_embeddings.py`
- modify: `tests/test_context_injection.py`
- modify: `tests/test_summarizer.py`
- modify: `tests/test_recent_chats.py`
- read: `design/specs/002-cleanup-plan/design.md` (§ Architecture → Branch identity fix, § Test Strategy)

## Prompt

### test_session_ops.py

Rewrite branch upsert tests for session-keyed identity:
- Remove all references to `existing_branches` dict parameter in test calls
- Remove `enforce_single_active_branch` tests entirely
- Update any tests that assert multiple branch rows per session — they should now assert exactly 1
- Update function signatures in test calls to match the new `upsert_branch` (no `existing_branches` param)

### test_parsing.py

Structural rewrite of ~150/384 lines:
- Remove `TestFindAllBranchesProperties` Hypothesis class (lines 108-153)
- Remove synthetic multi-branch generators `_build_uuid_tree`/`uuid_trees` (lines 53-105)
- Update per-fixture branch-count expectations for rewind fixtures (lines 42-43) — all fixtures should now produce exactly 1 branch
- Simplify remaining tests to single-branch assertions

### test_sync_hook.py

Add a test for session-keyed branch identity — sync a session N times, verify exactly 1 branch row.

### Remaining test files

For each of `test_import_pipeline.py`, `test_integration.py`, `test_backfill_embeddings.py`, `test_context_injection.py`, `test_summarizer.py`, `test_recent_chats.py`:
- Update any references to inactive branch handling or `enforce_single_active_branch`
- Update any `existing_branches` dict parameter usage
- Verify `is_active` fixture data is consistent with the new single-branch model

### Characterization test (AC#6)

Add a test proving the fix works: simulate syncing a session 10 times incrementally (representing 10 Stop-hook fires against a growing JSONL file). Assert exactly 1 branch row with `is_active = 1` exists for that session after all syncs.

After all changes, run `uv run pytest` and `uvx prek run --all-files`.

## Focus

- In `test_parsing.py`, the rewind fixture tests (`TestFixtureBranches`) currently assert branch counts > 1 for some fixtures. After simplification, all fixtures should produce exactly 1 branch.
- The Hypothesis class `TestFindAllBranchesProperties` generates random UUID trees to test multi-branch detection — this entire class is dead after T07's simplification and should be deleted.
- `test_session_ops.py` is the most code-intensive update — read the full file to understand how `existing_branches` is threaded through test helper functions.
- The characterization test for AC#6 should use the real `upsert_branch`/`sync_branch` code path, not a simplified mock.

## Verify

- [ ] AC#6: Test proves 10 sequential syncs produce exactly 1 branch row with `is_active = 1`
- [ ] AC#6: `grep -rn 'enforce_single_active_branch' tests/` returns zero hits
- [ ] AC#6: `grep -rn 'existing_branches' tests/` returns zero hits
- [ ] AC#6: `uv run pytest` passes with zero failures
- [ ] AC#6: `uvx prek run --all-files` passes
