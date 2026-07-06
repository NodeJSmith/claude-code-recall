# Design: Productionization Cleanup Round Two

**Date:** 2026-07-06
**Status:** archived
**Scope-mode:** hold

## Problem

PR #54 restructured ccrecall's largest pain points (dead subsystems, monolithic db.py, 879-line search module, branch identity bug) but explicitly deferred three oversized modules that mix multiple responsibilities: `session_ops.py` (744 lines, 3 functions over 50 lines), `memory_context.py` (699 lines, 3 functions over 50 lines including a 141-line `main`), and `backfill_embeddings.py` (491 lines, a 234-line `run`). These modules bias new code toward the same tangled patterns.

Additionally, a vestigial `fork_point_uuid` column (8 references, always NULL since abandoned-fork tracking was removed) pollutes the schema and confuses readers; orphan rows in `messages` left behind by the v1 migration waste storage; `ccrecall tail` sorts sessions by filesystem mtime instead of actual event timestamps, causing it to pick the wrong prior session after reboots (issue #45); and `sanitize_fts_term` lives in `content.py` despite being an FTS query sanitizer used only by `search_query.py`. Issues #9 and #26 were resolved by PR #54 but never closed.

## Goals

- Every module touched by this work adheres to single-responsibility: one concern per module, functions decomposed along natural seams
- No source file in the affected set exceeds 400 lines
- The `fork_point_uuid` column is removed from the schema via a versioned migration
- Orphan `messages` rows (unreferenced by any `branch_messages`) are cleaned in the same migration
- `ccrecall tail` orders sessions by in-file event timestamps, not filesystem mtime
- `sanitize_fts_term` lives with its consumer
- Issues #9 and #26 are closed
- All tests pass and `uvx prek run --all-files` is clean

## User Scenarios

### Developer: ccrecall maintainer
- **Goal:** Make changes to ccrecall without fighting oversized modules
- **Context:** Working in session_ops, memory_context, or backfill_embeddings code

#### Adding embedding logic
1. **Opens the embedding-related code**
   - Sees: a focused `embed_ops.py` (~200 lines) with only embedding concerns
   - Decides: where to make the change based on the module name
   - Then: edits without needing to understand branch operations, message insertion, or import-log bookkeeping

#### Debugging context injection
1. **Investigates why context injection selected the wrong sessions**
   - Sees: `session_selection.py` with only selection logic, separate from alert building and context rendering
   - Decides: which module to inspect based on the symptom (selection? rendering? alerts?)
   - Then: traces the issue in a focused module instead of a 699-line file

### User: ccrecall consumer
- **Goal:** Resume a Claude Code session reliably after a reboot
- **Context:** Machine rebooted, starting a new Claude Code session

#### Post-reboot session resumption
1. **Starts a new session and the ccr-resume skill fires**
   - Sees: correct prior session surfaced (not a stale one whose mtime was flattened by the reboot)
   - Decides: nothing — the correct session appears automatically
   - Then: continues work from where they left off

## Functional Requirements

- **FR#1** `session_ops.py` is decomposed into focused modules where each module owns a single concern (import-log operations, message operations, branch operations, embedding operations, top-level orchestration)
- **FR#2** `hooks/memory_context.py` is decomposed into focused modules where each module owns a single concern (proactive alerts, session selection, context rendering, hook entry point orchestration)
- **FR#3** `hooks/backfill_embeddings.py` is decomposed into focused modules where query construction/constants and status reporting are separated from the `run()` orchestrator
- **FR#4** The `fork_point_uuid` column is removed from the `branches` table via a schema migration (user_version bump from 1 to 2)
- **FR#5** Orphan rows in `messages` (rows not referenced by any `branch_messages` entry) are deleted in the same v2 migration, in FK-safe order
- **FR#6** `list_transcripts()` in `session_tail.py` sorts transcript files by the last event's `timestamp` field from the JSONL content, falling back to filesystem mtime only when no parseable timestamp exists
- **FR#7** `sanitize_fts_term` is relocated from `content.py` to `search_query.py`
- **FR#8** GitHub issues #9 and #26 are closed with references to the resolving PR (#54)

## Edge Cases

- **v2 migration crash**: The entire migration runs inside a single `BEGIN IMMEDIATE ... COMMIT` transaction. If the process crashes before COMMIT, the transaction rolls back atomically — no partial state persists. The next connection restarts the full v2 migration from scratch since `user_version` was not yet bumped
- **JSONL files with no timestamp field**: `list_transcripts()` falls back to mtime for any file where no parseable `timestamp` entry exists (e.g., truncated or corrupt transcripts)
- **Empty content.py after sanitize_fts_term removal**: `content.py` still contains `extract_text_content`, `parse_origin`, `extract_plain_text`, and other content-extraction helpers — it does not become empty
- **Concurrent migration + sync**: Same mitigation as v1 — `BEGIN IMMEDIATE` serializes writers, both converge on the same correct state. The orphan DELETE is idempotent.
- **Zero orphan messages**: On a database where all messages are referenced, the DELETE is a no-op and the migration proceeds normally

## Acceptance Criteria

- **AC#1** No source file created or modified by this work exceeds 400 lines (FR#1, FR#2, FR#3)
- **AC#2** `uv run pytest` passes with zero failures (FR#1-FR#7)
- **AC#3** `uvx prek run --all-files` passes (FR#1-FR#7)
- **AC#4** After running the v2 migration, `PRAGMA user_version` returns 2 (FR#4, FR#5)
- **AC#5** After the v2 migration, `SELECT COUNT(*) FROM messages m LEFT JOIN branch_messages bm ON bm.message_id = m.id WHERE bm.message_id IS NULL` returns 0 (FR#5)
- **AC#6** After the v2 migration, `PRAGMA table_info(branches)` does not include a `fork_point_uuid` column (FR#4)
- **AC#7** `ccrecall tail --list` orders sessions by actual activity time, not filesystem mtime, verified by a test that writes JSONL entries with explicit timestamps out of mtime order (FR#6)
- **AC#8** `sanitize_fts_term` is importable from `ccrecall.search_query` and not importable from `ccrecall.content` (FR#7)
- **AC#9** Issues #9 and #26 are closed on GitHub (FR#8)

## Key Constraints

- The embedding watermark protocol (clear-first/set-last in `embed_branch_chunks`) must be preserved exactly across the session_ops split — the transaction boundary must not change
- Hook stdout contract: every hook prints valid JSON on every exit path. The specific envelope varies by hook type: `memory_setup.py` emits `{"continue": true}` (with optional `hookSpecificOutput`), while `memory_context.py` emits `{}` or `{"hookSpecificOutput": {...}}` (no `"continue"` key). The memory_context split must preserve its existing `_emit_empty()`/`_emit_with_proactive()` pattern
- `PRAGMA foreign_keys = ON` is set on every connection at steady state. During migrations, `_apply_migrations` temporarily sets `foreign_keys = OFF` (required for the table-rebuild pattern) and restores it in a `finally` block. The v2 migration inherits this existing toggle — no additional FK handling needed. The orphan-message DELETE runs before the branches rebuild as a sequencing choice (clean data first, then restructure schema), not an FK requirement
- The `UNIQUE(session_id)` constraint on `branches` (added by v1, enforcing one-active-row-per-session — CLAUDE.md invariant #4) must be preserved through the v2 table rebuild
- The `is_active = 1` read filters on `branches` are permanent guards — do not remove them during the split
- The `no-lazy-imports` lint rule is absolute — module splits use structural reorganization, not function-level lazy loading

## Dependencies and Assumptions

- The v1 migration (`_migrate_to_v1`) has run on all known machines — no pre-v1 databases exist in the wild
- The `PRAGMA user_version` mechanism established in round one handles the v2 migration identically
- Both install paths (pip/uv package + Claude Code plugin) must continue working — no entry-point changes in this round (all hooks keep their existing console scripts)
- `prek` pre-commit framework enforces no-future-annotations, no-lazy-imports, and custom checks

## Architecture

### session_ops.py decomposition

Split the 744-line file into five focused modules. The public API surface is only 3 symbols (`sync_session`, `embed_branch_chunks`, `MAX_WRITE_PATH_EMBEDS_PER_SYNC`) consumed by 5 importers.

| Module | Functions | Est. lines |
|---|---|---|
| `import_log_ops.py` | `import_log_skip_check`, `upsert_import_log` | ~50 |
| `message_ops.py` | `upsert_session`, `build_message_row`, `insert_new_messages` | ~90 |
| `branch_ops.py` | `update_branch_row`, `insert_branch_row`, `upsert_branch`, `diff_branch_messages`, `sync_branch` | ~220 |
| `embed_ops.py` | `write_branch_summary`, `_stamp_branch_watermark`, `embed_branch_chunks`, `MAX_WRITE_PATH_EMBEDS_PER_SYNC` | ~240 |
| `session_ops.py` (slimmed) | `sync_session` — imports from the four above | ~95 |

`sync_branch` stays in `branch_ops.py` rather than the orchestrator because it is a per-branch operation that calls `upsert_branch`, `diff_branch_messages`, `write_branch_summary`, and `embed_branch_chunks` — it is the branch-level coordinator, not the session-level one.

`write_branch_summary` moves to `embed_ops.py` because it is called exclusively by `sync_branch` in the context of per-branch embedding work, and its error-handling pattern (catch `(ValueError, TypeError, KeyError)` for content errors and `sqlite3.Error` for infra errors, then continue to embedding) ties it to the embedding pipeline flow.

### memory_context.py decomposition

Split the 699-line hook into four focused modules. No production code imports from this module — only 2 test files.

| Module | Functions | Est. lines |
|---|---|---|
| `hooks/context_alerts.py` | `_proactive_alert_block` | ~90 |
| `hooks/session_selection.py` | `_row_to_entry`, `_find_first_substantive`, `_load_messages_for`, `_finalize`, `_find_cleared_from_session_uuid`, `_select_cleared_sessions`, `select_sessions`, SQL query constants, `HANDOFF_STALE_SECONDS`, `_CANDIDATE_LIMIT` | ~225 |
| `hooks/context_rendering.py` | `_build_fallback_context`, `_extract_topic`, `build_origin_block`, `build_context`, `_pending_question_block`, `TOPIC_PREVIEW_MAX_CHARS` | ~115 |
| `hooks/memory_context.py` (slimmed) | `_emit_empty`, `_emit_with_proactive`, `main`, `_CHARS_PER_TOKEN_ESTIMATE` — imports from the three above | ~170 |

`main` will shrink significantly once the three extracted concerns are imported rather than inline — the function's 141 lines include inline calls to alert building, session selection, and context rendering that become single-line calls to the extracted modules.

### backfill_embeddings.py decomposition

Split the 491-line file into three focused modules. In-file extraction was considered but rejected — it relocates code into new function bodies without reducing total line count, so the file would remain above 400 lines.

| Module | Functions | Est. lines |
|---|---|---|
| `hooks/backfill_query.py` | `cleanup_pid`, `days_modifier`, `build_selection`, constants (`BATCH_SIZE`, `BACKFILL_BATCH_DELAY_SECONDS`, `DEFAULT_PROGRESS_EVERY`, `BACKFILL_NICE_LEVEL`, exit codes, `PID_KEY`) | ~60 |
| `hooks/backfill_status.py` | `count_status`, `format_duration`, `run_status` | ~100 |
| `hooks/backfill_embeddings.py` (slimmed) | `run()` — imports from the two above | ~330 |

`run()` retains the `with get_connection(settings, load_vec=True) as conn:` block — the connection lifecycle stays in the orchestrator so the context manager's commit/rollback/close semantics are preserved. The batch loop body stays inline in `run()`. The extracted functions (`cleanup_pid`, `days_modifier`, `build_selection`, `count_status`, `format_duration`, `run_status`) are already separate top-level functions today — moving them to new modules reduces the file's total line count but does not change `run()` itself, which remains ~234 lines. Each extracted module owns a clear responsibility: query construction, status reporting, and orchestration.

### v2 schema migration

Add `_migrate_to_v2(conn)` following the established v1 pattern. Bump `SCHEMA_VERSION` from 1 to 2. Migration steps in order:

1. **Delete orphan messages**: `DELETE FROM messages WHERE id NOT IN (SELECT DISTINCT message_id FROM branch_messages)` — these are messages that were linked only to inactive branches deleted by v1. No FK dependency to worry about since `messages` is the parent table.
2. **Rebuild branches table** without `fork_point_uuid`: create `branches_new` with an explicit column list (omitting `fork_point_uuid`) and preserve the `UNIQUE(session_id)` constraint (a load-bearing invariant from v1 — see CLAUDE.md invariant #4). Copy data with a matching explicit SELECT, drop old, rename new. Re-create the four indexes and FTS sync triggers (same pattern as v1's table rebuild). Use explicit column lists in both the CREATE and INSERT — do NOT use `SELECT *`, which would break if the source table's column count doesn't match.

The `_apply_migrations` function already handles sequential version checks — add `if current < 2: _migrate_to_v2(conn)` after the existing v1 check. On a fresh install, both v1 and v2 run sequentially: v1 rebuilds `branches` (with `fork_point_uuid` intact), then v2 rebuilds again without it.

### Tail mtime fix

Replace the sort key in `list_transcripts()` (`session_tail.py:258-263`) from `p.stat().st_mtime` to a new helper `_last_event_timestamp(path)` that reads the tail of the JSONL file and extracts the last `timestamp` field. Use `collections.deque(fh, maxlen=20)` (already imported in session_tail.py) to read only the last ~20 lines. Fall back to mtime as an ISO string when no parseable timestamp exists.

### sanitize_fts_term relocation

Move the function definition from `content.py:11-32` to `search_query.py` (its only consumer). Update `test_security.py` imports from `ccrecall.content` to `ccrecall.search_query`. The `content.py` module retains `extract_text_content`, `parse_origin`, `extract_plain_text`, and other content-extraction helpers.

## Implementation Preferences

- New focused modules, naming per the Architecture section
- `sanitize_fts_term` moves to `search_query.py` (single consumer, not a shared SQL module)
- Follow codebase conventions for all other decisions

## Replacement Targets

| Target | Replaced by | Action |
|---|---|---|
| `session_ops.py` (monolithic 744 lines) | 4 focused modules + slim orchestrator | Split; remove original after all functions extracted |
| `memory_context.py` `main` inlining alerts/selection/rendering | 3 extracted modules imported by slim `main` | Split; `main` becomes an orchestrator |
| `backfill_embeddings.py` (491-line monolith) | 2 focused modules + slim orchestrator | Split into `backfill_query.py`, `backfill_status.py`, slimmed `backfill_embeddings.py` |
| `fork_point_uuid` column in `branches` | Nothing — dead column | Drop via v2 migration |
| Orphan `messages` rows | Nothing — unreachable data | Delete via v2 migration |
| `list_transcripts()` mtime sort | Timestamp-based sort from JSONL content | Replace sort key |
| `sanitize_fts_term` in `content.py` | Same function in `search_query.py` | Move + update imports |

## Migration

### v2 schema migration

**What changes**: `fork_point_uuid` column dropped from `branches` via table rebuild in the v2 migration (SCHEMA_CORE retains the column for v1 compatibility); orphan `messages` rows deleted.

**Migration steps** (inside `BEGIN IMMEDIATE ... COMMIT`):
1. Delete orphan messages (messages not referenced by any branch_messages row)
2. Rebuild `branches` table without `fork_point_uuid` column — same create-copy-drop-rename pattern as v1
3. Re-create indexes and FTS sync triggers on the rebuilt `branches` table
4. `PRAGMA user_version = 2`

**Reversibility**: The DB is a derived cache of JSONL transcripts. Full recovery = drop DB + re-import via `ccrecall import`. Not reversible in-place.

**What happens to data written by old code**: Old code writes `fork_point_uuid = NULL` — since the column is being dropped entirely, there is no data loss. Orphan messages have no consumers — their deletion is invisible to all queries.

### SCHEMA_CORE and v1 migration — do NOT edit

Keep `fork_point_uuid` in both `schema.py`'s `SCHEMA_CORE` and `db.py`'s `_migrate_to_v1` DDL. Removing it from either would break fresh installs: `SCHEMA_CORE` runs first via `CREATE TABLE IF NOT EXISTS` (creating 17 columns), then `_migrate_to_v1` runs its table rebuild with `INSERT INTO branches_new SELECT * FROM branches` — if `branches_new` has 18 columns but the source table has 17, SQLite raises `table branches_new has 18 columns but 17 values were supplied`. The v2 migration's table rebuild is the sole mechanism that drops the column from live tables. After v2 runs, the column is gone; SCHEMA_CORE's stale definition is harmless because `CREATE TABLE IF NOT EXISTS` is a no-op on existing tables.

Remove `fork_point_uuid` from `parsing.py`'s branch dict (no longer emitted by the parser) and from `session_ops.py`'s INSERT column list (the column exists in the table until v2 runs, but NULL is the default so omitting it from INSERT statements is safe — and after v2 drops it, old statements that still name the column would crash).

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
DON'T: Assume all hooks use the same pattern — `memory_context.py` emits `{}` (no `"continue"` key) via `_emit_empty()` on error/early-return paths, while `memory_setup.py` emits `{"continue": true}`. Preserve whichever pattern the hook currently uses.

### Module decomposition — plain boundary types

**Source:** round one's search decomposition (`search_query.py`, `search_vector.py`, `search_hydrate.py`)

Boundary types between extracted modules are plain Python types: `int` (branch IDs), `tuple[int, float]` (score tuples), `dict` (result cards). No custom classes cross module boundaries. This keeps the coupling loose and avoids circular imports.

DO: Pass plain types (IDs, tuples, dicts) between extracted modules.
DON'T: Create new dataclasses or custom types just to shuttle data between the split modules.

## Alternatives Considered

**Inline function extraction only (no new modules)**: Extract the oversized functions within the same file (e.g., break `embed_branch_chunks` into 3 helpers inside `session_ops.py`). This brings function sizes under 50 lines but does not address the single-responsibility violation — the file still mixes 5 concerns and stays at 744 lines. Rejected because the goal is SOLID adherence, not just function-length compliance.

**Inline extraction only for backfill_embeddings**: Extract helpers within the same file (e.g., `_process_batch`, `_validate_and_configure`) without creating new modules. Rejected because in-file extraction relocates code into new function bodies without reducing total line count — the file would remain above 400 lines (AC#1).

**Move sanitize_fts_term to db.py**: `db.py` already hosts `escape_like` (an SQL helper). But `sanitize_fts_term` is FTS-specific query sanitization, not general SQL escaping, and its only consumer is `search_query.py`. Moving it to `db.py` would add an FTS concern to a module that owns connections and schema. Rejected in favor of colocation with the consumer.

**Skip orphan message cleanup**: Orphan messages are benign (no query surfaces them). But the v2 migration already touches the `branches` table for the `fork_point_uuid` drop — adding the orphan DELETE is one additional statement with zero incremental risk. Including it is cleaner than leaving known dead data.

## Test Strategy

### Existing Tests to Adapt

- `tests/test_session_ops.py` (26 tests): Update imports from `ccrecall.session_ops` to the new modules (`embed_ops`, `branch_ops`, etc.) for any tests that directly import extracted functions. Critically, update ~12 `patch("ccrecall.session_ops.embed_text", ...)` calls and ~1 `patch("ccrecall.session_ops.compute_context_summary", ...)` call to target `ccrecall.embed_ops` — after the split, `embed_text` resolves in `embed_ops`'s namespace, not `session_ops`'s, so patches against the old path would silently fail to intercept.
- `tests/test_context_injection.py` (37 tests): Update imports from `ccrecall.hooks.memory_context` to the new modules (`context_alerts`, `session_selection`, `context_rendering`) for tests that directly import `_proactive_alert_block`, `select_sessions`, `build_context`, `_build_fallback_context`, and `TOPIC_PREVIEW_MAX_CHARS` (moves to `context_rendering`). Also update `monkeypatch` targets for `probe_filesystem` to the correct new module path.
- `tests/test_clear_handoff_contract.py`: Update import of `_find_cleared_from_session_uuid` from `memory_context` to `session_selection`.
- `tests/test_backfill_embeddings.py` (34 tests): Update import of `MAX_WRITE_PATH_EMBEDS_PER_SYNC` from `ccrecall.session_ops` to `ccrecall.embed_ops` (line 27). Update ~8 `patch("ccrecall.session_ops.embed_text", ...)` calls to target `ccrecall.embed_ops` (same namespace issue as test_session_ops). Update imports of `BATCH_SIZE`, `EXIT_OK`, `EXIT_ABORT` from `backfill_embeddings` to `backfill_query` where those constants moved.
- `tests/test_session_tail.py` (31 tests): Update `TestResolveTarget.test_picks_second_newest` to write JSONL entries with `timestamp` fields instead of relying on `os.utime()` mtime manipulation.
- `tests/test_parsing.py`: Remove the `assert active["fork_point_uuid"] is None` assertion (line 93) — the key no longer exists in the branch dict.
- `tests/test_db.py`: Update schema introspection test (lines 860-879) — remove the `fork_point_uuid` row and decrement the `cid` ordinal (first element) of every subsequent column tuple by one (14 rows shift from cid 4-17 to cid 3-16). Add v2 migration tests.
- `tests/test_security.py` (17 tests): Update import of `sanitize_fts_term` from `ccrecall.content` to `ccrecall.search_query`.

### New Test Coverage

- **FR#4, FR#5**: Test v2 migration: seed DB at user_version=1 with orphan messages and fork_point_uuid column, run migration, assert orphans deleted and column dropped. Test re-entrancy (partial migration retry).
- **FR#6**: Test `list_transcripts()` ordering: write two JSONL files with timestamps in opposite order to their mtime, assert the timestamp-ordered file comes first. Test mtime fallback for files with no parseable timestamps.
- **FR#1, FR#2**: No new behavioral tests needed — the module splits are structural, not behavioral. Existing tests exercising the same functions (now in new modules) verify behavioral equivalence.

### Tests to Remove

- No tests are removed — all existing behavioral tests remain valid after the structural splits. Only imports and assertions about removed schema elements change.

## Documentation Updates

- **CLAUDE.md**: Update the Architecture section — add the new modules from the session_ops and memory_context splits to the relevant descriptions. Update "Four invariants to preserve" to reflect any new module boundaries.
- **GitHub issues**: Close #9 (token refactor — moot, subsystem deleted in PR #54) and #26 (remove legacy migration — done in PR #54) with comments referencing the resolving PR.
- **Issue #22**: Update to reflect which items are now complete: the `memory_context.py` and `backfill_embeddings.py` large function splits are addressed; the `sanitize_fts_term` relocation from the "Pre-existing nits" section is addressed by FR#7; the token-subsystem dedup items were already moot (deleted in PR #54). Note that `summarizer.py`'s `render_context_summary` (~97 lines) from the "Large function splits" section remains unaddressed — it is out of scope for this cleanup.

## Impact

### Changed Files

**Created files (source):**
- create `src/ccrecall/import_log_ops.py` — import-log skip check and upsert
- create `src/ccrecall/message_ops.py` — session upsert, message row building, message insertion
- create `src/ccrecall/branch_ops.py` — branch row CRUD, branch-message diffing, per-branch sync orchestration
- create `src/ccrecall/embed_ops.py` — branch summary writing, watermark management, chunk embedding pipeline
- create `src/ccrecall/hooks/backfill_query.py` — query construction, constants, PID cleanup for backfill
- create `src/ccrecall/hooks/backfill_status.py` — status counting, duration formatting, status reporting
- create `src/ccrecall/hooks/context_alerts.py` — proactive health alert block builder
- create `src/ccrecall/hooks/session_selection.py` — session selection algorithm, DB queries, candidate scoring
- create `src/ccrecall/hooks/context_rendering.py` — context block rendering, topic extraction, fallback context

**Modified files (source — cross-cutting, highest risk):**
- modify `src/ccrecall/session_ops.py` — slim to orchestrator only (~95 lines), importing from the four new modules
- modify `src/ccrecall/hooks/memory_context.py` — slim to hook entry point only (~180 lines), importing from the three new modules
- modify `src/ccrecall/hooks/backfill_embeddings.py` — slim to orchestrator only, importing from `backfill_query` and `backfill_status`; update import of `embed_branch_chunks` from `session_ops` to `embed_ops`
- modify `src/ccrecall/db.py` — add `_migrate_to_v2`, bump `SCHEMA_VERSION` to 2
- modify `src/ccrecall/schema.py` — no change to SCHEMA_CORE (fork_point_uuid kept for v1 compatibility); verify `SCHEMA` test constant stays in sync
- modify `src/ccrecall/parsing.py` — remove `fork_point_uuid` from branch dict (line 152) and docstring (line 120)
- modify `src/ccrecall/session_tail.py` — replace mtime sort with timestamp-based sort in `list_transcripts()`; update `resolve_target()` docstring which references "newest file by mtime"
- modify `src/ccrecall/search_query.py` — add `sanitize_fts_term` definition (moved from content.py)
- modify `src/ccrecall/content.py` — remove `sanitize_fts_term` definition

**Modified files (source — lower risk):**
- modify `src/ccrecall/cli/commands.py` — update `backfill_embeddings_mod.DEFAULT_PROGRESS_EVERY` and `backfill_embeddings_mod.cleanup_pid()` references to import from `backfill_query` (both symbols move there); update stale `upsert_branch` comment (line 94) to reference `branch_ops`
- modify `src/ccrecall/hooks/sync_current.py` — update import of `sync_session` (if the re-export from session_ops.py is insufficient)
- modify `src/ccrecall/hooks/import_conversations.py` — update import of `sync_session` (same)

**Modified files (tests):**
- modify `tests/test_session_ops.py` — update imports for split modules
- modify `tests/test_context_injection.py` — update imports for split modules
- modify `tests/test_clear_handoff_contract.py` — update import of `_find_cleared_from_session_uuid`
- modify `tests/test_session_tail.py` — update `test_picks_second_newest` for timestamp-based ordering
- modify `tests/test_parsing.py` — remove `fork_point_uuid` assertion
- modify `tests/test_db.py` — update schema introspection, add v2 migration tests
- modify `tests/test_security.py` — update `sanitize_fts_term` import
- modify `tests/test_backfill_embeddings.py` — update import of `MAX_WRITE_PATH_EMBEDS_PER_SYNC` if moved

**Modified files (docs/config):**
- modify `src/ccrecall/__init__.py` — update submodule-listing docstring to include the new top-level modules (`import_log_ops`, `message_ops`, `branch_ops`, `embed_ops`)
- modify `CLAUDE.md` — update architecture section for new modules from the session_ops and memory_context splits

### Behavioral Invariants

- `sync_session` and `embed_branch_chunks` continue to produce identical DB state for the same JSONL input — the split is structural, not behavioral
- Hook stdout contract: `memory_context.py`'s `main()` always prints valid JSON (`{}` via `_emit_empty()` or `{"hookSpecificOutput": {...}}` via `_emit_with_proactive()` — no `"continue"` key)
- Embedding watermark protocol: clear-first/set-last transaction boundary preserved exactly across the embed_ops extraction
- `ccrecall search` returns equivalent results — `sanitize_fts_term` relocation does not change its behavior
- `ccrecall recent` ordering unchanged — it queries the DB by `ended_at`, unaffected by the tail fix
- `PRAGMA foreign_keys = ON` on every connection
- `is_active = 1` read filters retained on all branch queries

### Blast Radius

- **ccrecall consumers**: Transparent for all changes except the tail fix, which is a bug fix (correct behavior replaces broken behavior)
- **Plugin installs**: No entry-point changes — hooks/hooks.json is unchanged
- **CI**: Test import updates are mechanical but touch 8 test files — CI must pass before merge
- **Release-please**: Conventional commits drive changelog automatically

## Open Questions

None.
