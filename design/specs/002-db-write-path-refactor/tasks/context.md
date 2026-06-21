# Context: DB-Write-Path Refactor (Part 2 of Issue #20)

## Problem & Motivation
Two high-blast-radius functions in ccrecall's DB-**write** path exceed the 50-line guideline and bundle several responsibilities each: `migrations.py`'s `migrate_columns` (~146L, runs on every DB open) and `migrate_db` (~61L), and `session_ops.py`'s `sync_session` (~339L, runs on every sync/import). Unlike part 1 (PR #21, the token read path), these mutate the SQLite file on disk — a botched split corrupts user data silently rather than producing a wrong chart. Part 1 deliberately deferred these because they need a DB-state characterization harness, not the output-snapshot harness part 1 used. A related pre-existing nit: `migrate_db`'s reconnect path sets WAL + busy_timeout but not `foreign_keys=ON`, diverging from the canonical `db.apply_base_pragmas`.

## Visual Artifacts
None.

## Key Decisions
1. **Behavior-preserving only — no observable change.** For identical inputs and starting DB state, the resulting schema, `user_version`, every row, and all return values are identical to current behavior. Adopting `apply_base_pragmas` in `migrate_db`'s recreate path adds `foreign_keys=ON`, but this is a **non-observable consistency change**: the recreate connection runs DDL only then closes, and `foreign_keys` is connection-scoped with no on-disk effect. It is verified by inspection (recreate routes through `apply_base_pragmas`), **not** a runtime assertion — there is no surface to observe it on (observability gap, named in design AC#3). Do NOT try to write a test asserting the recreate connection's `foreign_keys` state.
2. **Pin before move.** No structural change to a function ships before a characterization test pinning its DB effect is green on the *current* code. Migration pins already largely exist in `tests/test_db.py` — the work is gap-fill + relocate, not write-from-scratch.
3. **Pragma consolidation avoids an import cycle.** `migrations.py` must never import from `db.py` (`db.py` imports `migrate_columns`/`migrate_db` from it). So `apply_base_pragmas` relocates to the cycle-free `models.py` (already hosts `BUSY_TIMEOUT_MS`, `LOGGER_NAME`); both `db.py` and `migrations.py` import it from there. Chosen over the minimal one-line inline `foreign_keys=ON` because it consolidates the duplicated pragma triple into one source of truth.
4. **Functions over methods.** Both functions thread an explicit `sqlite3.Cursor`/`Connection` today; the split keeps that — module-level helpers taking `cursor`/`conn` + data as explicit args. No `Migrator`/`SyncSession` class.
5. **Uniform version-bump placement.** Today v1–v4 bump `user_version` inline in `migrate_columns` while v5/v6 bump inside their delegates. The split removes this asymmetry by moving every bump+commit *into* each `migrate_vN` helper (behavior-identical: still runs once, after the body, on the same gate condition).
6. **Don't over-fragment cohesive migration steps.** `_migrate_v5`/`_migrate_project_paths` (just over guideline) get only their per-row body lifted, leaving a loop-orchestrator skeleton. `_migrate_v6` (~55L) is left essentially as-is.

## Constraints & Anti-Patterns
- **No smuggled behavior changes.** If a split surfaces a latent bug, note + file separately; preserve current behavior including warts. The pins encode current behavior. The `foreign_keys=ON` that comes with adopting `apply_base_pragmas` is a non-observable consistency change (Key Decision 1), not an observable behavior delta.
- **v4 backup return is intentionally discarded** — `migrate_columns`' v4 gate calls `_backup_db_before_migration(db_path, "v4")` but ignores its bool; the DML runs even if backup failed (unlike `migrate_db`). Preserve this — do NOT add a guard.
- **Preserve every `commit()` boundary.** `migrate_columns` commits after each DDL group and version gate; `sync_session` NEVER commits (callers own the transaction). Helpers must not add/remove a `commit()` or move commit ownership into `sync_session`.
- **Preserve every `except` boundary wholesale.** The three summary/embed failure classifications in `sync_session` and the FTS-rebuild `contextlib.suppress(Exception)` in `_migrate_v5` are load-bearing — each `try/except` moves intact into the helper owning the guarded op; never widened/narrowed/dropped.
- **Preserve early-return guards adjacent to their query.** `_backfill_origin`/`_migrate_project_paths` guard on table existence and return early; do NOT hoist the guard into the orchestrator.
- **Hoisted probes stay hoisted.** `vec_writable = branch_vec_queryable(conn)` is probed once before the branch loop — keep it once-per-call, passed down as an arg.
- **Embed-on-write ordering is load-bearing.** vec0 upsert FIRST, version columns LAST, only for active leaves with a successful summary and a queryable vec table.
- **Naming:** newly extracted helpers drop the leading `_` (personal style: all methods public); existing `_`-prefixed names (`_backfill_origin`, `_migrate_v5`, `_migrate_v6`, `_migrate_project_paths`, `_backup_db_before_migration`, `_reaggregate_notification_branches`) stay as-is to avoid churn/test breakage (issue #20 says keep the convention). This non-uniformity is deliberate.
- **Out of scope (do NOT touch):** read/render-path splits (`summarizer.py`, `hooks/memory_context.py`, `hooks/backfill_embeddings.py` — part 3); the insights/findings/recommendations triple-representation (a behavior change); read-path helper relocations/renames (`sanitize_fts_term`, `_CONFIG_KEYS`).
- **Public import surface unchanged:** `migrate_db`, `migrate_columns` (imported by `db.py`, `hooks/memory_setup.py`), `sync_session` (imported by `hooks/sync_current.py`, `hooks/import_conversations.py`).

## Design Doc References
- `## Architecture` — the concrete split for `migrations.py` (DDL helpers, version dispatch, migrate_db split, pragma relocation) and `session_ops.py` (per-responsibility helpers).
- `## Edge Cases` — the guard/except/probe behaviors that must survive each split.
- `## Key Constraints` — pin-before-move, no-smuggled-changes, no-import-cycle, commit/except preservation.
- `## Test Strategy` — existing `test_db.py` migration suite is the starting pin; gap-fill, then relocate or augment.
- `## Impact → Behavioral Invariants` — what must stay byte-identical.

## Convention Examples
### Module-level helper threading an explicit cursor (the target shape)
**Source:** `src/ccrecall/migrations.py` (`_reaggregate_notification_branches` — already the shape the split should produce)
```python
def _reaggregate_notification_branches(cursor: sqlite3.Cursor) -> None:
    """Re-aggregate branches that contain notification messages."""
    cursor.execute(...)
    affected_branches = [row[0] for row in cursor.fetchall()]
    for bid in affected_branches:
        ...
```

### Early-return guard preserved verbatim in the extracted helper
**Source:** `src/ccrecall/migrations.py` (`_migrate_project_paths` guard)
```python
# Guard: projects and sessions tables may not exist in minimal test DBs
tables = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
if "projects" not in tables or "sessions" not in tables:
    return
```
**DO** keep the guard adjacent to its early return inside whichever helper owns the query. **DON'T** hoist the guard into the orchestrator and call the helper conditionally — that relocates behavior.

### Load-bearing exception classification (move wholesale, never reshape)
**Source:** `src/ccrecall/session_ops.py` (summary-write failure handling)
```python
try:
    summary_md, summary_json = compute_context_summary(cursor, branch_db_id)
    cursor.execute("UPDATE branches SET context_summary = ? ... WHERE id = ?", (...))
except (ValueError, TypeError, KeyError):
    summary_md = None            # content error — skip this branch's summary
except sqlite3.Error:
    logging.getLogger(LOGGER_NAME).exception("sync: summary write failed for branch %s", branch_db_id)
    summary_md = None            # infra error — log + skip
```

### Pragma helper consolidated in one place
**Source:** `src/ccrecall/db.py` (`apply_base_pragmas` — relocating to `models.py`)
```python
def apply_base_pragmas(conn: sqlite3.Connection) -> None:
    """Set WAL mode, busy_timeout, and foreign-key enforcement for concurrent-safe access."""
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
```
