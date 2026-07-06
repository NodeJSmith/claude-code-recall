# Context: Productionization Cleanup Round Two

## Problem & Motivation
PR #54 restructured ccrecall's largest pain points but explicitly deferred three oversized modules: `session_ops.py` (744 lines), `memory_context.py` (699 lines), and `backfill_embeddings.py` (491 lines). These modules mix multiple responsibilities and bias new code toward tangled patterns. Additionally, a dead `fork_point_uuid` column pollutes the schema, orphan message rows waste storage, `ccrecall tail` mispicks sessions after reboots due to mtime-based sorting, and `sanitize_fts_term` lives in the wrong module. Issues #9 and #26 were resolved by PR #54 but never closed.

## Visual Artifacts
None.

## Key Decisions
1. Split `session_ops.py` into 5 modules: `import_log_ops.py`, `message_ops.py`, `branch_ops.py`, `embed_ops.py`, and a slim `session_ops.py` orchestrator. Rationale: 5 distinct concerns, only 3 public symbols consumed externally.
2. Split `memory_context.py` into 4 modules: `context_alerts.py`, `session_selection.py`, `context_rendering.py`, and a slim `memory_context.py` entry point. Rationale: no production importers, 4 distinct responsibility clusters.
3. Split `backfill_embeddings.py` into 3 modules: `backfill_query.py`, `backfill_status.py`, and a slim `backfill_embeddings.py`. Rationale: in-file extraction was rejected because it wouldn't reduce total line count below 400.
4. Keep `fork_point_uuid` in both `SCHEMA_CORE` and `_migrate_to_v1` DDL. Only the v2 migration's table rebuild drops it. Removing it from either would break fresh installs where v1 runs first.
5. `sanitize_fts_term` moves to `search_query.py` (its only consumer), not to `db.py`.
6. `list_transcripts()` switches from mtime to JSONL timestamp-based sorting, with mtime fallback.
7. Boundary types between extracted modules stay plain Python types (int, tuple, dict) — no new dataclasses cross module boundaries.

## Constraints & Anti-Patterns
- The embedding watermark protocol (clear-first/set-last in `embed_branch_chunks`) must be preserved exactly — the transaction boundary must not change.
- Hook stdout contract varies by hook: `memory_setup.py` emits `{"continue": true}`, `memory_context.py` emits `{}` or `{"hookSpecificOutput": {...}}` (no "continue" key). Preserve whichever pattern the hook currently uses.
- `PRAGMA foreign_keys = ON` at steady state; `_apply_migrations` temporarily sets OFF for table rebuilds.
- The `is_active = 1` read filters on `branches` are permanent guards — do not remove.
- No lazy imports — module splits use structural reorganization only.
- Do NOT edit `SCHEMA_CORE` or `_migrate_to_v1` DDL — both must keep `fork_point_uuid` for fresh-install compatibility.
- Do NOT create new dataclasses or custom types to shuttle data between split modules.
- Mock.patch targets must be updated to the new module paths — patches against the old `ccrecall.session_ops.embed_text` path will silently fail to intercept after the split.

## Design Doc References
- `## Architecture` — detailed module decomposition tables, line estimates, function assignments
- `## Migration` — v2 migration steps, SCHEMA_CORE/v1 compatibility rationale
- `## Test Strategy` — existing tests to adapt (with specific import/patch changes), new test coverage needed
- `## Convention Examples` — migration pattern, hook error handling (two patterns), module decomposition boundary types
- `## Key Constraints` — embedding watermark, hook stdout, FK toggle, is_active filters
- `## Edge Cases` — migration crash atomicity, JSONL without timestamps, zero orphans

## Convention Examples

### Migration pattern — table rebuild with FK-safe deletes

**Source:** `src/ccrecall/db.py:238-338`

```python
def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    # Step 1: FK-safe delete order
    cursor.execute(
        "DELETE FROM branch_messages WHERE branch_id IN "
        "(SELECT id FROM branches WHERE is_active = 0)"
    )
    cursor.execute(
        "DELETE FROM chunks WHERE branch_id IN "
        "(SELECT id FROM branches WHERE is_active = 0)"
    )
    cursor.execute("DELETE FROM branches WHERE is_active = 0")

    # Step 2: Table rebuild for constraint change
    cursor.execute("CREATE TABLE branches_new (...)")
    cursor.execute("INSERT INTO branches_new SELECT ... FROM branches")
    cursor.execute("DROP TABLE branches")
    cursor.execute("ALTER TABLE branches_new RENAME TO branches")

    # Step 3: Re-create indexes and FTS triggers
    cursor.execute("CREATE INDEX IF NOT EXISTS ...")
    # ... FTS triggers ...
```

DO: Delete dependent rows first (branch_messages, chunks), then parent rows (branches). Re-create triggers after DROP TABLE (which auto-drops them).
DON'T: Drop the parent table before cleaning dependents — FK violations crash the migration.

### Hook error handling — two patterns

**Source (Stop/SessionEnd hooks):** `src/ccrecall/hooks/memory_setup.py:126-168`

```python
def main():
    additional_context: str | None = None
    try:
        # ... all hook logic ...
    except Exception:
        log_hook_exception("setup")

    # OUTSIDE the try -- always runs
    output: dict = {"continue": True}
    if additional_context is not None:
        output["hookSpecificOutput"] = { ... }
    print(json.dumps(output))
```

**Source (SessionStart context hook):** `src/ccrecall/hooks/memory_context.py:555-699`

```python
def main():
    try:
        # ... selection, rendering, assembly ...
        print(json.dumps(output))  # success path: {"hookSpecificOutput": {...}}
        return
    except Exception:
        log_hook_exception("context")
    _emit_empty()  # error path: prints {}
```

DO: Ensure every exit path (success, early return, exception) prints valid JSON to stdout.
DON'T: Assume all hooks use the same pattern — preserve whichever pattern the hook currently uses.

### Module decomposition — plain boundary types

**Source:** round one's search decomposition (`search_query.py`, `search_vector.py`, `search_hydrate.py`)

Boundary types between extracted modules are plain Python types: `int` (branch IDs), `tuple[int, float]` (score tuples), `dict` (result cards). No custom classes cross module boundaries.

DO: Pass plain types (IDs, tuples, dicts) between extracted modules.
DON'T: Create new dataclasses or custom types just to shuttle data between the split modules.
