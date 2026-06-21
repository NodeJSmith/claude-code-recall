---
task_id: "T03"
title: "Split migrate_columns into DDL helpers + uniform version dispatch"
status: "planned"
depends_on: ["T02"]
implements: ["FR#1", "FR#2", "FR#6", "FR#7", "FR#8", "AC#5", "AC#6", "AC#7"]
---

## Summary
Decompose `migrate_columns` (~146L) into a thin orchestrator over DDL helpers plus a uniform version-gated DML dispatch, and remove the version-bump asymmetry between v1–v4 (currently inline) and v5/v6 (currently inside their delegates). Lift the per-row bodies of the just-over-guideline `_migrate_v5` and `_migrate_project_paths` into named helpers, leaving loop-orchestrator skeletons. Behavior-preserving — every resulting schema, `user_version`, and row stays identical; the T01 pins stay green.

## Target Files
- modify: `src/ccrecall/migrations.py`
- read: `design/specs/002-db-write-path-refactor/design.md`
- read: `design/specs/002-db-write-path-refactor/tasks/context.md`
- read: `tests/test_migrations.py`

## Prompt
Read `tasks/context.md` and `design/specs/002-db-write-path-refactor/design.md` (`## Architecture` → the `migrations.py` bullets, `## Edge Cases`, `## Key Constraints`).

Refactor `src/ccrecall/migrations.py` (do **not** touch `migrate_db`/`nuke_old_db`/`recreate_fresh_schema` — T02 owns those; this task may sit on top of T02's committed changes):

**1. DDL helpers** — extract from `migrate_columns`, each idempotent and owning its current `commit()` cadence (drop leading `_` on new helpers):
- `add_message_columns(conn, cursor)` — the `tool_summary`/`is_notification`/`origin` adds.
- `add_branch_columns(conn, cursor)` — the `branches` column adds + the two `CREATE INDEX IF NOT EXISTS`.
- `ensure_token_snapshots(conn, cursor)` — the `token_snapshots` table create + `data_source` column add.

**2. Uniform version dispatch.** Extract the inline v1, v2, v4 bodies into `migrate_v1(conn, cursor)`, `migrate_v2(conn, cursor)`, `migrate_v4(conn, cursor, db_path)` to match the already-delegated `_backfill_origin`(v3)/`_migrate_v5`/`_migrate_v6`. **Move the `PRAGMA user_version = N` + `conn.commit()` INTO each `migrate_vN` helper** (the v5/v6 convention) so every gate is self-contained — including v1/v2/v4 whose bumps currently live inline in `migrate_columns` (lines 229-259). For v3, the bump currently runs in `migrate_columns` *after* `_backfill_origin` returns; move it so v3's bump is owned consistently too (either inside `_backfill_origin` or a thin `migrate_v3` wrapper — pick the form that keeps `_backfill_origin`'s name importable, since `db.py`/tests reference migrations symbols). This is behavior-identical: each bump still runs exactly once, after its body, gated on the same `version < N` condition.
- `migrate_columns` becomes: read `version` (`PRAGMA user_version`) and `db_path` (`PRAGMA database_list`), call the DDL helpers, then a flat sequence of `if version < N: migrate_vN(...)` gates. It no longer touches `user_version` directly.

**3. Lift per-row bodies** of the just-over-guideline batch migrations (leave the batch-loop skeleton + per-batch commit cadence in place so each reads as a loop orchestrator):
- `_migrate_v5` (~78L): lift the per-branch transform into `recompute_branch_v5(cursor, row)` (parse files/commits JSON, recompute aggregated_content, INTERRUPTED→ABANDONED rewrites). Keep the final `contextlib.suppress(Exception)` FTS rebuild and the `PRAGMA user_version = 5` in `_migrate_v5`.
- `_migrate_project_paths` (~76L): lift the per-project fix into `fix_project_path(cursor, proj_id, real_cwd, ...)` (the merge-vs-update branch). Keep the table-existence guard + early return adjacent to the query inside `_migrate_project_paths`.
- `_migrate_v6` (~55L): leave as-is unless lifting `truncate_branch_json_v6(cursor, row)` genuinely reads better — do not force a split for a few lines.

Preserve **every** `commit()` boundary, the v4 backup-return-discarded behavior, and all early-return guards adjacent to their queries (see context.md Constraints).

Run `uv run pytest -q` — full suite green, T01 migration pins unchanged.

## Focus
- **`migrate_columns` runs on every DB open** (read and write connections via `db.get_db_connection`, also `hooks/memory_setup.py`). Its idempotence and no-op-on-already-migrated behavior are load-bearing — a stray extra `commit()` or a moved gate condition is a behavior change.
- The DML gates run on every `memory_db` construction (starts at `user_version=0`), so the existing suite exercises them heavily — a version-bump-placement mistake will surface immediately as a failing pin, not silently.
- Naming: new helpers (`add_*`, `ensure_*`, `migrate_v1/v2/v4`, `recompute_branch_v5`, `fix_project_path`) are public (no `_`); existing `_`-prefixed names stay (`_backfill_origin`, `_migrate_v5`, `_migrate_v6`, `_migrate_project_paths`, `_reaggregate_notification_branches`) — `db.py`/tests import some of these by name.
- This is behavior-preserving structure only. If a split surfaces a latent migration bug, do NOT fix it here — note + file separately; the pins encode current behavior including warts.

## Verify
- [ ] FR#1: for an old-schema starting DB, `migrate_columns` produces identical resulting schema, `user_version`, and rows as before (T01 pins green).
- [ ] FR#2: `migrate_columns` is still idempotent — second call is a no-op, `user_version` stable (T01 idempotence pin green).
- [ ] FR#6: each version gate v1–v6 is reachable through a named helper invoked uniformly from `migrate_columns`; v1/v2/v4 are extracted helpers like v3/v5/v6, and no `PRAGMA user_version` remains in `migrate_columns`.
- [ ] FR#7: every function in `migrations.py` touched here is ≤50 lines except `migrate_columns` (pure orchestrator) and the batch loops whose per-row body is a single named helper.
- [ ] FR#8: `migrate_columns`, `_backfill_origin`, `_migrate_v5`, `_migrate_v6`, `_migrate_project_paths` remain importable with unchanged signatures.
- [ ] AC#5: a scan confirms no function in `migrations.py` exceeds the guideline except the documented orchestrators/batch-loops.
- [ ] AC#6: `uv run pytest -q` passes with zero failures.
- [ ] AC#7: importing each public/preserved name from `ccrecall.migrations` succeeds.
