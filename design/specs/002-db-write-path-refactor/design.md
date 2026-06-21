# Design: DB-Write-Path Refactor (Part 2 of Issue #20)

**Date:** 2026-06-21
**Status:** approved
**Scope-mode:** hold

## Problem

Issue #20's clean-code tail has two remaining high-blast-radius functions in the DB-**write** path that exceed the project's 50-line guideline and bundle several responsibilities each. Unlike part 1 (PR #21, the token read path), these mutate the SQLite file on disk, so a botched split corrupts user data silently rather than producing a wrong chart:

- `src/ccrecall/migrations.py` — `migrate_columns` (~146 lines) runs on **every** DB open (via `db.get_db_connection`). It interleaves idempotent DDL (column adds to `messages`/`branches`, the `token_snapshots` table create, the `data_source` add) with six version-gated DML migrations (`user_version` 1→6), three of which (v1, v2, v4) are inlined while the rest already delegate to `_migrate_v5`/`_migrate_v6`/`_migrate_project_paths`/`_backfill_origin`. The DDL/DML interleave and the inline-vs-delegated asymmetry make the gate sequence hard to read end-to-end.
- `src/ccrecall/session_ops.py` — `sync_session` (~339 lines, lines 49–387; the whole module is essentially this one function) runs on every sync and every import. Its own docstring enumerates seven distinct responsibilities: import_log dedup, session upsert, message insertion with UUID dedup, branch detection, per-branch metadata + branch_messages diff, aggregated-content assembly, and context-summary + embed-on-write. They run as one flat body threading a shared `cursor`.

`migrate_db` (~61 lines, lines 26–86) is also over the guideline — it bundles schema detection, a destructive backup+nuke+WAL-cleanup block, and a fresh-schema recreate. A related pre-existing nit lives in its recreate block: the reconnect path (`migrations.py:73-74`) sets `journal_mode=WAL` and `busy_timeout` but **not** `foreign_keys=ON`, whereas the canonical `db.apply_base_pragmas` (db.py:69) sets all three. The pragma triple is duplicated and divergent.

This matters now because part 1 deliberately deferred these two files (they are write-path, needing a different — DB-state — characterization harness than the read-path output snapshots part 1 used). Left alone, `migrate_columns` keeps accreting inline version gates and `sync_session` keeps growing per-branch logic, both in code where a regression means data corruption.

## Goals

- Every function in `migrations.py` and `session_ops.py` lands under the 50-line guideline, except top-level orchestrators (`migrate_columns`, `sync_session`, `migrate_db`) that contain only sequenced calls to named helpers plus the control flow tying them together, and already-cohesive batch loops whose body is a single named per-row helper.
- `migrate_columns` reads as: DDL helpers, then a uniform version-gated DML dispatch where every gate (v1–v6) is a named helper called the same way — no inline-vs-delegated asymmetry.
- `sync_session` reads as orchestration over named helpers, one per responsibility its docstring already names.
- The base-pragma triple (`WAL`, `busy_timeout`, `foreign_keys=ON`) exists once and `migrate_db`'s reconnect uses it, without creating a `db → migrations → db` import cycle.
- **No observable behavior change.** For identical inputs and identical starting DB state, the resulting DB state (schema, `user_version`, every row) and the functions' return values are identical to current behavior.

## Non-Goals

- The read/render-path splits deferred to **part 3**: `summarizer.py` (`render_context_summary`), `hooks/memory_context.py` (`main`, `select_sessions`), `hooks/backfill_embeddings.py` (`run`). Separate design doc, same branch.
- The **insights/findings/recommendations triple-representation** decision from issue #20 — that is a behavior change (deciding on one representation), not a refactor, and is out of scope for every part.
- The helper relocations/renames issue #20 lists for the read path (`sanitize_fts_term` → `db.py`, `_CONFIG_KEYS` rename). Not in the write path; defer.
- No change to SQL semantics, migration outcomes, schema, `user_version` end state, dedup logic, embed-on-write ordering, or any row value. Structure only.
- No change to the public import surface: `migrate_db`, `migrate_columns` (imported by `db.py`, `hooks/memory_setup.py`), `sync_session` (imported by `hooks/sync_current.py`, `hooks/import_conversations.py`).
- `_backup_db_before_migration`, `_reaggregate_notification_branches`, `_backfill_origin` keep their names — they are already cohesive sub-50-line (or batch-loop) helpers and any caller/test depending on the names must keep working.

## User Scenarios

### Maintainer (Jessica): sole developer on ccrecall
- **Goal:** add a schema migration or adjust sync logic without reading a 150–340-line function end-to-end.
- **Context:** bumping the schema to `user_version = 7`, or changing how a branch's summary is written during sync.

#### Add a new versioned migration
1. **Find the version dispatch.**
   - Sees: a uniform sequence in `migrate_columns` where each `user_version` gate calls one named `_migrate_vN(...)` helper.
   - Decides: add a new `_migrate_v7` helper and one gate, matching the existing shape.
   - Then: the DDL block above is untouched; the new gate sits alongside its peers with no inline/delegated split to reconcile.

#### Adjust per-branch sync behavior
1. **Locate the per-branch helper.**
   - Sees: a `sync_session` orchestrator that loops branches and calls one focused per-branch helper, which itself delegates summary/embed to named functions.
   - Decides: edit the one helper that owns that step.
   - Then: dedup, session upsert, and import_log logic are untouched.

## Functional Requirements

- **FR#1** `migrate_columns(conn)` applied to a DB at any prior schema state (missing columns, `user_version` 0–6) produces the identical resulting schema, `user_version`, and row contents as the pre-refactor function for the same starting state.
- **FR#2** `migrate_columns` is idempotent post-refactor exactly as before: a second call on an already-migrated DB performs no further changes and leaves `user_version` unchanged.
- **FR#3** `migrate_db(conn)` returns the same boolean and performs the same nuke-and-recreate / no-op decision as before for each starting schema (pre-v3, fresh, already-v3). Its recreate path routes through `apply_base_pragmas` (which sets `foreign_keys=ON` in addition to WAL + busy_timeout) — a **consistency** change with no observable effect, since the recreate connection runs DDL only and is closed immediately, and `foreign_keys` is connection-scoped with no on-disk persistence.
- **FR#4** `sync_session(conn, filepath, project_dir, ...)` returns the same integer (new-message count, `0`, or `-1`) and writes the same rows to `sessions`, `messages`, `branches`, `branch_messages`, and `import_log` as the pre-refactor function for identical inputs and starting state.
- **FR#5** The embed-on-write ordering invariant is preserved: vec0 upsert before version-column write, only for active leaves with a successful summary and a queryable vec table.
- **FR#6** Each version-gated DML migration (v1–v6) is reachable through a named helper invoked uniformly from the `migrate_columns` dispatch; the inline v1/v2/v4 bodies become named helpers like the already-extracted v3/v5/v6.
- **FR#7** Every function in both modules is ≤50 lines, except the top-level orchestrators `migrate_columns`, `sync_session`, and `migrate_db` (pure sequencing + control flow over named helpers) and batch loops whose per-row body is a single named helper.
- **FR#8** The public import surface (`migrate_db`, `migrate_columns`, `sync_session`) and the preserved helper names (`_backup_db_before_migration`, `_reaggregate_notification_branches`, `_backfill_origin`, `_migrate_project_paths`, `_migrate_v5`, `_migrate_v6`) remain importable with unchanged signatures.

## Edge Cases

- **Minimal test DBs missing tables.** `_backfill_origin` guards on `sessions`/`import_log` existence (migrations.py:317 — **not** `projects`); `_migrate_project_paths` guards on `projects`/`sessions` existence (migrations.py:384). Each returns early when its required tables are absent. The decomposition must preserve each guard with its own table set — a split that moves the guard away from its early-return, or conflates the two guards' table sets, changes behavior on minimal DBs.
- **Backup failure aborts destructive migration.** `migrate_db` refuses to `unlink` the DB when `_backup_db_before_migration` returns False (disk full / permissions). This refuse-to-destroy path must survive the split exactly.
- **v4 backup return is intentionally discarded.** In `migrate_columns`' v4 gate, `_backup_db_before_migration(db_path, "v4")` is called but its `bool` return is **ignored** — unlike `migrate_db`, the v4 DML (`UPDATE messages SET origin = NULL ...`) runs even if the backup failed. This is existing behavior; the split must preserve it (do **not** "fix" it by adding a guard — that would be a smuggled behavior change). It stays as-is whether v4 is extracted into `migrate_v4` or not.
- **FTS table absent.** `_migrate_v5`'s final FTS rebuild is wrapped in `contextlib.suppress(Exception)` because the FTS virtual table may not exist (FTS-disabled SQLite build). Preserve the suppression boundary.
- **Oversized JSON still oversized after truncation.** `_migrate_v6` falls back to emptying `last_exchanges` when `truncate_mid` alone doesn't get the JSON under 50KB. Preserve this second-pass fallback.
- **Embed/summary failures during sync.** `sync_session` classifies summary write failures three ways (content errors → skip; `sqlite3.Error` → log+skip; embed failure → broad suppress) so one branch's failure never aborts the import. Every `except` boundary and its logging must move intact into whatever helper owns that step.
- **sqlite-vec not loaded.** `vec_writable = branch_vec_queryable(conn)` is probed once before the branch loop; embed-on-write is skipped entirely when false. The probe must stay hoisted (once per call, not per branch).
- **Empty / branchless / messageless session.** `sync_session` early-returns `0` when parsing yields no entries, no branches, or no messages. These early returns must be preserved at the orchestrator level.
- **import_log NULL-hash staleness.** A stored NULL hash with a provided `file_hash` is treated as stale and re-processed; an exact non-NULL hash match returns `-1`. The dedup helper must preserve this asymmetry.

## Acceptance Criteria

- **AC#1** Migration characterization tests pin the resulting `PRAGMA table_info` sets, `user_version`, and affected row transformations (notification backfill, origin nullify, v5 aggregated_content/disposition rewrite, v6 truncation) for old-schema starting states. **Substantial coverage already exists in `tests/test_db.py`** (`TestMigrateColumns`, `TestVersionedMigration`, `TestMigrateDb`, with the `_pre_migration_db`/`_versioned_db` old-schema builders). These tests stay green through the split. Any gap they leave (e.g., a per-`user_version` gate or a row-transform assertion not currently pinned) is filled **before** the corresponding split. Whether the migration tests are relocated into a new `tests/test_migrations.py` or augmented in place in `test_db.py` is a planning decision — but the existing `test_db.py` coverage is the starting pin, not a blank slate. (FR#1, FR#6)
- **AC#2** A migrations idempotence test runs `migrate_columns` twice and asserts the second call is a no-op (no schema change, `user_version` stable). `test_db.py:TestMigrateColumns` already exercises the column-add idempotence (`migrate_columns(memory_db)` called a second time); extend to assert `user_version` stability if not already pinned. (FR#2)
- **AC#3** The existing `migrate_db` boolean/decision matrix (`test_db.py:TestMigrateDb` → pre-v3 → True + recreate, fresh → False, already-v3 → False) stays green through the split — this is the observable behavior of `migrate_db` and the real pin. The `foreign_keys=ON` addition is verified by **inspection** that `recreate_fresh_schema` routes through `apply_base_pragmas` (no separate runtime assertion). **Observability gap:** the recreate connection's `foreign_keys` state cannot be asserted after the fact — the connection is closed immediately after DDL and the pragma is connection-scoped with no on-disk effect, so there is no surface to observe. This is named as a gap, not silently skipped. (FR#3)
- **AC#4** Existing `sync_session` tests (`tests/test_session_ops.py`, `tests/test_import_pipeline.py`, `tests/test_sync_hook.py`) pass **unchanged** — they pin `sync_session` at the public level. A new DB-state golden pin is added only for any write path those tests leave unasserted (e.g., exact `import_log` row contents on the NULL-hash-stale path). (FR#4, FR#5)
- **AC#5** A manual or `ruff`-assisted scan confirms no function in either module exceeds the line guideline except the documented orchestrators/batch-loops. (FR#7)
- **AC#6** The full test suite passes with zero failures after the refactor. (all)
- **AC#7** Importing each public/preserved name from its module succeeds with unchanged signature. (FR#8)

## Key Constraints

- **Behavior-pin-before-move.** No structural change to a function ships before a characterization test pinning that function's DB effect is green on the *current* code. For migrations this means writing the net-new `test_migrations.py` first; for `sync_session` it means confirming the existing tests cover the touched write paths (and adding a pin where they don't) before splitting. This is a `refactoring-discipline.md` requirement.
- **No smuggled behavior changes.** If the split surfaces a latent migration or sync bug, do not fix it here — note it, file it separately, preserve current behavior including warts. The pins encode current behavior. This work introduces **no observable behavior change**: the `foreign_keys=ON` that comes with adopting `apply_base_pragmas` in `migrate_db`'s recreate path is a non-observable consistency change (see FR#3/AC#3 and the observability gap noted there); everything else is pure structure.
- **No import cycle.** `migrations.py` must not import from `db.py` (the module docstring states this; `db.py` imports `migrate_columns`/`migrate_db` from `migrations.py`). The pragma-consolidation fix therefore relocates `apply_base_pragmas` to the cycle-free `models.py` (which already hosts `BUSY_TIMEOUT_MS`, `LOGGER_NAME`) rather than importing it into `migrations.py` from `db.py`.
- **Cursor/connection threading, not methods.** Both functions thread an explicit `sqlite3.Cursor`/`Connection` through their steps today. The decomposition keeps that — module-level helpers taking `cursor`/`conn` (and the per-branch / per-row data) as explicit args. No `SyncSession` or `Migrator` class with methods (personal style: functions over methods, no `_`-private-method ceremony). A small frozen state object is acceptable only if explicit threading reads worse.
- **Preserve `commit()` placement.** `migrate_columns` commits after each DDL group and after each version gate; `sync_session` commits are driven by the caller (it never commits — callers own the transaction). Helpers must not introduce or remove a `commit()`; commit boundaries are behavior.
- **Preserve every `except` boundary.** The three summary/embed failure classifications in `sync_session` and the FTS-rebuild suppression in `_migrate_v5` are load-bearing. Each `try/except` moves wholesale into the helper that owns the guarded operation — never widened, narrowed, or dropped.

## Dependencies and Assumptions

- No external systems. In-process SQLite plus JSONL file reads.
- `migrations.py` import base: imports from `ccrecall.schema`, `ccrecall.content`, `ccrecall.parsing`, `ccrecall.summarizer`, `ccrecall.models` — **never** `ccrecall.db`. The relocated `apply_base_pragmas` lands in `models.py` (already a `migrations.py` import, already cycle-free), so no new edge is added.
- `session_ops.py` imports from `ccrecall.db` (`branch_vec_queryable`, `write_branch_embedding`) — it sits *above* `db.py`, so it may continue to. Its helpers stay in `session_ops.py`.
- Assumes the existing fixtures (`memory_db`, `make_vec_conn`, the JSONL `fixtures/*.jsonl`) and the existing `test_db.py` old-schema builders (`_pre_migration_db`, `_versioned_db`) are sufficient to characterize both functions. **Note:** `SCHEMA_CORE` does *not* set `user_version`, so a fresh `memory_db` starts at `user_version = 0` and `migrate_columns` runs **all** DML gates (v1–v6) on every `memory_db` construction — the gates are *not* no-ops there. This means ordinary `memory_db`-based tests already exercise the DDL-add and DML-gate paths; any asymmetry the split introduces between them surfaces in the existing suite, not only in bespoke old-schema tests. The `_versioned_db(user_version=N)` builder is the tool for pinning a specific starting version.
- Assumes `sqlite-vec` availability is already optional and probed (`branch_vec_queryable`); the embed-on-write split must keep working on FTS/vec-disabled builds.

## Architecture

### migrations.py

`migrate_columns` decomposes into a DDL phase and a uniform DML dispatch:

- **DDL helpers** (each idempotent, column-existence gated, owning its own `commit()` cadence as today):
  - `add_message_columns(conn, cursor)` — `tool_summary`, `is_notification`, `origin` adds.
  - `add_branch_columns(conn, cursor)` — the `branches` column adds + the two `CREATE INDEX IF NOT EXISTS`.
  - `ensure_token_snapshots(conn, cursor)` — the `token_snapshots` table create + the `data_source` column add.
- **Versioned DML dispatch.** Extract the inline v1, v2, v4 bodies into named helpers (`migrate_v1`, `migrate_v2`, `migrate_v4`) so they match the already-delegated `_migrate_project_paths`/`_migrate_v5`/`_migrate_v6`/`_backfill_origin`. `migrate_columns` then reads as: resolve `version` and `db_path`, then a flat sequence of `if version < N: migrate_vN(...)` gates. (Naming: drop leading `_` on the newly extracted helpers per personal style; the existing `_`-prefixed names stay as-is to avoid churn/test breakage — issue #20 explicitly says "keep the `_`-prefixed convention here," so this is a deliberate non-uniformity, not an oversight.)
- **Version-bump placement is currently asymmetric — resolve it deliberately, do not "preserve as-is" blindly.** Today the `PRAGMA user_version = N` + `conn.commit()` for **v1, v2, v3, v4 sit inline in `migrate_columns`** (lines 229–259), *after* the gate body runs (v3's `_backfill_origin` returns, then `migrate_columns` bumps to 3). For **v5 and v6 the bump sits inside `_migrate_v5`/`_migrate_v6`**. When extracting v1/v2/v4, choose **one** convention and apply it uniformly: move the bump+commit *into* each `migrate_vN` helper (the v5/v6 convention), passing `conn` so the helper can run the PRAGMA. This makes every gate self-contained and is behavior-identical (the bump still runs once, after the body, on the same `version <` condition). The orchestrator then never touches `user_version` directly. AC#1/AC#2 pin that the resulting `user_version` sequence is unchanged. (Do not split the difference — leaving v1/v2/v4 bumps in the orchestrator while v5/v6 own theirs is the asymmetry being removed.)
- `migrate_columns` becomes the orchestrator: the two `PRAGMA table_info` reads it needs for gating stay (or move into the DDL helpers), then the DDL helper calls, then the version dispatch.
- **`_migrate_v5` (~78L) and `_migrate_project_paths` (~76L).** These are already cohesive single-purpose steps, each a batch/loop over branches or projects sitting just over the guideline. The clean split is to lift the **per-row body** into a named helper (`recompute_branch_v5(cursor, row)`, `fix_project_path(cursor, proj_id, real_cwd, ...)`), leaving the function as the batch-loop skeleton + commit cadence — a loop orchestrator that reads under the guideline. Do **not** fragment further; over-splitting a cohesive migration step hurts readability (`laziness-protocol.md`).
- **`_migrate_v6` (~55L) already essentially fits** — it is barely over once the docstring is excluded. Leave it as-is unless lifting its per-row truncation body (`truncate_branch_json_v6(cursor, row)`) genuinely improves readability; do not force a split to chase a handful of lines.
- **`migrate_db` split + reconnect pragma fix.** `migrate_db` (~61L) decomposes into a detect-and-decide orchestrator over two helpers along its existing seams:
  - `nuke_old_db(conn, db_path) -> bool` — the destructive block (backup via `_backup_db_before_migration`, refuse-to-destroy on backup failure, `conn.close()` + `unlink` + WAL/SHM cleanup). Returns whether the nuke proceeded.
  - `recreate_fresh_schema(db_path) -> None` — the fresh connect + schema create. This is where the pragma fix lands: replace the inline `PRAGMA journal_mode = WAL` + `PRAGMA busy_timeout` pair (lines 73–74) with `apply_base_pragmas(new_conn)`, which additionally sets `foreign_keys=ON`.
  - `migrate_db` keeps the schema detection (branches/sessions probes), the early returns, and the orchestration calling the two helpers — landing under the guideline as a decision orchestrator.
- **Pragma relocation.** Relocate `apply_base_pragmas` from `db.py` to `models.py`. `db.py` imports it from `models` (its two internal call sites, db.py:254 and db.py:261, unchanged in behavior). Note on the fix: `journal_mode=WAL` persists to the file while `foreign_keys`/`busy_timeout` are connection-scoped and the recreate connection is closed immediately after DDL — so adding `foreign_keys=ON` is non-observable consistency-hardening, not a behavioral change to the created schema. It is therefore verified by inspection (recreate routes through `apply_base_pragmas`), not a runtime assertion (see AC#3 observability gap).

### session_ops.py

Decompose `sync_session` into module-level helpers, one per responsibility its docstring names, all taking explicit `cursor`/`conn` + data:

- `import_log_skip_check(cursor, filepath, write_import_log, file_hash) -> tuple[row, bool]` — the dedup probe; returns the existing log row and whether to short-circuit to `-1`.
- `upsert_session(cursor, session_uuid, project_id, meta) -> int` — the session INSERT…ON CONFLICT + id fetch.
- `insert_new_messages(cursor, session_id, messages, valid_branch_uuids, existing_uuids) -> int` — the message-insert loop with UUID dedup, notification flagging, tool-result skip, text-empty skip; returns `new_count`.
- `sync_branch(conn, cursor, branch, messages, uuid_to_msg_id, existing_branches, session_id, vec_writable)` — the per-branch loop body (lines 210–366, ~157L), so it further delegates:
  - `upsert_branch(cursor, branch, branch_meta, session_id, existing_branches) -> int` (INSERT vs UPDATE + the single-active-branch enforcement) → `branch_db_id`.
  - `diff_branch_messages(cursor, branch_db_id, branch_uuids, uuid_to_msg_id)` (add/remove link diff).
  - `write_branch_summary(cursor, branch_db_id) -> str | None` (the `compute_context_summary` + 3-way `except` classification).
  - `embed_branch(cursor, branch_db_id, summary_md, is_active, vec_writable)` (the guarded embed-on-write, ordering invariant intact).
- `write_import_log(cursor, filepath, session_id, file_hash, log_row)` — the final UPDATE-or-INSERT.
- `sync_session` becomes orchestration: dedup check (early `-1`), parse + branch/message early-returns (`0`), project upsert, `upsert_session`, build the valid-uuid set + existing-uuid set, `insert_new_messages`, build the `uuid_to_msg_id` map, probe `vec_writable` once, loop `sync_branch` over branches, `write_import_log`, return `new_count`.

The branch loop's `vec_writable` probe and the `existing_uuids`/`valid_branch_uuids`/`uuid_to_msg_id` maps stay computed in the orchestrator (they are cross-helper shared reads), passed down as args — matching the cursor-threading constraint.

## Replacement Targets

The inline `PRAGMA journal_mode = WAL` / `PRAGMA busy_timeout` pair in `migrate_db` (migrations.py:73–74) is replaced by the relocated `apply_base_pragmas(new_conn)` — remove the inline pair, do not leave it beside the call. The `apply_base_pragmas` *definition* moves from `db.py` to `models.py` (db.py's copy is deleted, not duplicated). The inline v1/v2/v4 migration bodies in `migrate_columns` are replaced by extracted named helpers — the inline bodies are superseded, not kept in parallel. No other code is replaced.

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

## Alternatives Considered

- **Minimal pragma fix: inline `PRAGMA foreign_keys = ON` in `migrate_db` only.** Smallest diff (one line), no cross-file churn, leaves `db.py` untouched. Rejected as the primary because it leaves the pragma triple duplicated and divergent in two places — the exact smell the goal calls out. The relocate-to-`models.py` form consolidates the decision (`coding-style.md`: one source of truth) at the cost of touching `db.py`'s import line. Kept as the documented fallback if relocating `apply_base_pragmas` proves to ripple further than expected.
- **A `Migrator` / `SyncSession` class holding `conn`/`cursor` as state with handler methods.** Rejected — personal style is functions over methods and no `_`-private-method ceremony; the cursor threads cleanly as an explicit arg, and these functions have exactly one call path each, so a class adds state-to-hold (`reader-load.md`) with no payoff.
- **Split `_migrate_v5`/`_migrate_v6`/`_migrate_project_paths` aggressively into many sub-helpers.** Rejected — they are cohesive single-purpose migration steps already near the guideline; lifting only the per-row body keeps them readable without fragmenting one logical migration across five functions.
- **Fold migrations + session_ops pins into the part-1 golden harness.** Rejected — part 1 pins *output dicts* (read path); these pin *DB state* (write path). Different harness shape (old-schema fixture builder, schema/row diffing), which is exactly why #20 split write from read.
- **Do nothing.** Rejected — these are the two highest-blast-radius functions left in #20; `migrate_columns` runs on every DB open and `sync_session` on every import, and both keep accreting.

## Test Strategy

### Existing Tests to Adapt
- `tests/test_db.py` — **already holds the migration characterization suite**: `TestMigrateColumns`, `TestVersionedMigration`, `TestMigrateDb`, plus the `_pre_migration_db()` and `_versioned_db(user_version=N)` old-schema builders. This is the existing behavior pin for `migrate_columns`/`migrate_db`. It must stay green through the split. Planning decides whether to **relocate** this suite into a new `tests/test_migrations.py` (cleaner home, mirrors the module split) or **augment in place** — either way the existing assertions are preserved, not rewritten.
- `tests/test_session_ops.py`, `tests/test_import_pipeline.py`, `tests/test_sync_hook.py` — must pass **unchanged**; they pin `sync_session` at the public level. If any needs editing, that signals a behavior change and is a red flag, not a routine adaptation.
- `tests/conftest.py` (`memory_db`, `make_vec_conn`) — reused as-is. `memory_db` starts at `user_version = 0`, so it already drives all six DML gates each construction (see Assumptions); no change needed.
- `tests/test_integration.py` — references `migrate_columns`/`user_version` indirectly; confirm it still passes, no edits expected.

### New Test Coverage
- **Gap-fill the existing migration suite before splitting**, not from scratch: identify any `user_version` gate or row-transform (notification backfill, origin nullify, v5 aggregated_content/disposition rewrite, v6 truncation second-pass fallback, v4-backup-failure-still-runs-DML) that `test_db.py` does not already assert, and add the missing pin (FR#1/FR#6, AC#1/AC#2).
- **No new `foreign_keys` runtime assertion** — it is non-observable on the recreate path (see AC#3 observability gap). `TestMigrateDb`'s boolean/decision matrix is the migrate_db pin and stays green; the pragma adoption is verified by inspection.
- **DB-state golden pin for `sync_session`** only where existing tests leave a write path unasserted (e.g., exact `import_log` row on the NULL-hash-stale path) (FR#4/AC#4).

All gap-fill pins are committed **first**, green on the current code, then kept green through each refactor commit. (If the suite is relocated to `test_migrations.py`, the move is its own commit, green before and after.)

### Tests to Remove
No tests to remove — nothing is deleted from the public surface.

## Documentation Updates

- `src/ccrecall/migrations.py` module docstring — update if the DDL/DML helper split changes what the top-of-file summary describes (it currently names `migrate_db`/`migrate_columns`; keep those accurate).
- `CHANGELOG` — add a part-2 entry referencing issue #20, mirroring the part-1 (PR #21) entry style.
- No README, CLI-help, or rules-file references to these functions exist — confirmed during reconnaissance. No other doc updates required.

## Impact

### Changed Files
- `src/ccrecall/models.py` — modify: add `apply_base_pragmas` (relocated from `db.py`). (cross-cutting base — imported by both `db.py` and `migrations.py`.)
- `src/ccrecall/db.py` — modify: remove `apply_base_pragmas` definition; import it from `models`. (shared — its two internal call sites, db.py:254 and db.py:261, now resolve the name via import.)
- `src/ccrecall/migrations.py` — modify: split `migrate_columns` into DDL helpers + uniform version dispatch; extract inline v1/v2/v4 (moving the version-bump into each helper uniformly); lift per-row bodies of v5/project_paths; split `migrate_db` into `nuke_old_db`/`recreate_fresh_schema` + detect orchestrator; call `apply_base_pragmas` in `recreate_fresh_schema`.
- `src/ccrecall/session_ops.py` — modify: split `sync_session` into per-responsibility helpers threading `cursor`/`conn`.
- `tests/test_migrations.py` — create: old-schema builder + `migrate_columns`/`migrate_db` characterization + idempotence pins.
- `tests/test_session_ops.py` — modify: add a DB-state golden pin only for any unasserted write path.

### Behavioral Invariants
- For identical starting DB state and inputs, the resulting schema, `user_version`, and every row written by `migrate_columns`/`migrate_db`/`sync_session` are identical to current behavior. There is no observable behavior change: the `foreign_keys=ON` that `migrate_db`'s recreate path gains by adopting `apply_base_pragmas` is connection-scoped on a connection closed immediately after DDL — it alters no on-disk state (see AC#3 observability gap).
- `migrate_columns` runs on **every** DB open (read and write connections alike via `db.get_db_connection`); its idempotence — a no-op on an already-fully-migrated DB (all columns present, `user_version = 6`) — must not change.
- `sync_session` never commits (callers own the transaction); commit ownership must not move into it.
- Public/preserved import names keep their signatures.

### Blast Radius
- `migrate_columns`/`migrate_db` are called by `db.get_db_connection` (every connection) and `hooks/memory_setup.py`. Behavior-preserving, so all consumers unaffected.
- `sync_session` is called by `hooks/sync_current.py` and `hooks/import_conversations.py`. Behavior-preserving.
- `apply_base_pragmas` relocation touches every `db.py` connection-open path — but the function body is identical, only its definition site moves, so runtime behavior is unchanged. This is the one cross-file ripple; AC#6 (full suite green) covers it.
- All within `src/ccrecall/`; no cross-package or external consumers.

## Open Questions

None.
