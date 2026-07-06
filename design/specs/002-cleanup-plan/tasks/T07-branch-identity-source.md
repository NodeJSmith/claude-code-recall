---
task_id: "T07"
title: "Fix branch identity to session-keyed single row"
status: "done"
depends_on: ["T05"]
implements: ["FR#4", "FR#5", "FR#6", "FR#18"]
---

## Summary

Change `upsert_branch` from leaf_uuid-keyed identity (creates a new row on every incremental sync) to session-keyed single-row identity (one row per session, updated in place). Simplify `find_all_branches` to return only the active branch, deleting ~55 lines of abandoned-fork detection. Remove `enforce_single_active_branch`. Add a branch-count invariant check to `ccrecall stats`. Test updates are in T08.

## Target Files

- modify: `src/ccrecall/session_ops.py`
- modify: `src/ccrecall/parsing.py`
- modify: `src/ccrecall/hooks/import_conversations.py`
- modify: `src/ccrecall/cli/commands.py`
- read: `design/specs/002-cleanup-plan/design.md` (§ Architecture → Branch identity fix, Branch invariant check)

## Prompt

### session_ops.py changes (FR#4)

Rewrite `upsert_branch` to key on `session_id` instead of `leaf_uuid`. The current pattern (around line 737-738) builds an `existing_branches` dict keyed by `leaf_uuid`:
```python
existing = {row[1]: row[0] for row in cursor.execute("SELECT id, leaf_uuid FROM branches WHERE session_id = ?")}
```

Replace with a direct session_id lookup:
```python
cursor.execute("SELECT id FROM branches WHERE session_id = ? AND is_active = 1", (session_id,))
row = cursor.fetchone()
if row:
    branch_db_id = row[0]
    # UPDATE existing row
else:
    branch_db_id = cursor.execute("INSERT INTO branches ...").lastrowid
```

This eliminates:
- The `existing_branches: dict[str, int]` parameter from `upsert_branch`'s signature
- The dict-building query
- The `existing_branches` parameter from `sync_branch` (which threads it through)

Remove `enforce_single_active_branch` entirely. Remove `fork_point_uuid` from INSERT/UPDATE statements (always NULL). Continue writing `leaf_uuid` on each sync (useful diagnostic field).

**Preserve the embedding watermark protocol exactly** — the clear-first/set-last pattern in `embed_branch_chunks` must not change.

### parsing.py changes (FR#5)

Simplify `find_all_branches` to return only the active branch. Delete the abandoned-fork detection code at lines 167-220. **Preserve line 221** (the `return branches` statement). Remove the `MAX_BRANCH_DEPTH` constant.

### import_conversations.py changes

Remove the abandoned branch count logging from the import summary.

### Branch invariant check (FR#18)

Add to the stats output in `cli/commands.py` (where `cmd_stats` is defined): a query checking for sessions with multiple active branches:
```sql
SELECT session_id, COUNT(*) as cnt FROM branches WHERE is_active = 1 GROUP BY session_id HAVING cnt > 1
```
Log at WARNING level if any rows returned. Display the count in stats output.

### is_active filters (FR#6)

Do NOT remove any `is_active = 1` read filters. After all changes, verify with `grep -rn 'is_active' src/` that all filters remain.

Do NOT update test files — test changes are T08's scope. Source-only changes here.

## Focus

- The `session_ops.py` rewrite is the highest-risk edit. Read the full `upsert_branch` and `sync_branch` functions before changing. The embedding watermark protocol (clear-first/set-last in `embed_branch_chunks`) MUST be preserved.
- The `parsing.py` deletion must be precise — lines 167-220 are the fork detection code, but line 221 (`return branches`) is the function's return statement and must stay.
- T07 runs BEFORE T09 (schema versioning + migration). The old `UNIQUE(session_id, leaf_uuid)` constraint still works because session-keyed code produces at most one row per session_id, which trivially satisfies the wider constraint. T09 then tightens it to `UNIQUE(session_id)` safely.

## Verify

- [ ] FR#4: `upsert_branch` queries by `session_id`, not `leaf_uuid`; `existing_branches` dict parameter is gone
- [ ] FR#5: `find_all_branches` returns only the active branch; abandoned-fork detection code (lines 167-220) is deleted; `MAX_BRANCH_DEPTH` constant removed
- [ ] FR#6: All `is_active = 1` read filters remain in place (`grep -rn 'is_active' src/` shows filters in search, context, recent_chats, db.py)
- [ ] FR#18: `ccrecall stats` reports a WARNING if any session has multiple active branches
- [ ] FR#4: `enforce_single_active_branch` is deleted from session_ops.py
