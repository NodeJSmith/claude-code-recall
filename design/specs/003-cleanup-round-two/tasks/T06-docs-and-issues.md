---
task_id: "T06"
title: "Update documentation and close stale issues"
status: "done"
depends_on: ["T01", "T02", "T03", "T04", "T05"]
implements: ["FR#8", "AC#9"]
---

## Summary
Update `CLAUDE.md` and `__init__.py` to reflect the new module boundaries from the splits, then close GitHub issues #9 and #26 which were resolved by PR #54. Also update issue #22 to reflect which items this cleanup addresses. This task runs last since it documents the final state of all prior changes.

## Target Files
- modify: `CLAUDE.md`
- modify: `src/ccrecall/__init__.py`
- read: `src/ccrecall/import_log_ops.py` (created by T03 — verify exists)
- read: `src/ccrecall/message_ops.py` (created by T03)
- read: `src/ccrecall/branch_ops.py` (created by T03)
- read: `src/ccrecall/embed_ops.py` (created by T03)
- read: `src/ccrecall/hooks/context_alerts.py` (created by T04)
- read: `src/ccrecall/hooks/session_selection.py` (created by T04)
- read: `src/ccrecall/hooks/context_rendering.py` (created by T04)
- read: `src/ccrecall/hooks/backfill_query.py` (created by T05)
- read: `src/ccrecall/hooks/backfill_status.py` (created by T05)

## Prompt
### CLAUDE.md updates

In `CLAUDE.md`, update the Architecture section:

1. In the `session_ops` / sync description area, document the new module decomposition: `import_log_ops.py` (import-log skip check and upsert), `message_ops.py` (session/message row operations), `branch_ops.py` (branch CRUD, message diffing, per-branch sync), `embed_ops.py` (summary writing, watermark management, chunk embedding). Note that `session_ops.py` is now a slim orchestrator importing from these four.

2. In the hooks description area, document the `memory_context.py` decomposition: `context_alerts.py` (proactive health alert block), `session_selection.py` (session selection algorithm and DB queries), `context_rendering.py` (context block rendering and topic extraction). Note that `memory_context.py` stays as the hook entry point.

3. Document the `backfill_embeddings.py` decomposition: `backfill_query.py` (query construction, constants, PID cleanup), `backfill_status.py` (status counting, formatting, reporting).

4. Update "Four invariants to preserve" (or its current heading) to mention the new module boundaries where relevant — particularly that `embed_ops.py` now owns the embedding watermark protocol (invariant about hook hot path applies to the new module structure).

5. Update the schema version reference — `SCHEMA_VERSION` is now 2 (v2 drops `fork_point_uuid` and cleans orphan messages).

### __init__.py update

In `src/ccrecall/__init__.py`, update the submodule-listing docstring to include the new top-level modules: `import_log_ops`, `message_ops`, `branch_ops`, `embed_ops`.

### GitHub issues

Close issue #9 with comment: "Resolved — the token analytics subsystem was deleted entirely in PR #54."

Close issue #26 with comment: "Resolved — the legacy migration code was removed in PR #54."

Update issue #22 with comment documenting which items are now addressed:
- `memory_context.py` large function splits → addressed by the memory_context decomposition
- `backfill_embeddings.py` large function splits → addressed by the backfill decomposition
- `sanitize_fts_term` relocation (Pre-existing nits section) → addressed by FR#7
- Token-subsystem dedup items → moot (deleted in PR #54)
- `summarizer.py`'s `render_context_summary` (~97 lines) → remains unaddressed, out of scope

## Focus
- Read the current `CLAUDE.md` Architecture section carefully before editing — it was updated in PR #54 and documents `config.py`/`db.py` split, session-keyed branch identity, per-process logging, and search decomposition. Add the new module info alongside these existing descriptions without disturbing them.
- The "Four invariants to preserve" section is numbered — if adding a new item or updating existing ones, keep the numbering consistent.
- For issue comments, use `gh issue close <number> --comment "..."` to close with a comment in one command.
- `__init__.py` is only 13 lines — it's a small docstring update.

## Verify
- [ ] FR#8: Issues #9 and #26 are closed on GitHub with comments referencing PR #54
- [ ] AC#9: `gh issue view 9 --json state` shows `"state": "CLOSED"` and `gh issue view 26 --json state` shows `"state": "CLOSED"`
