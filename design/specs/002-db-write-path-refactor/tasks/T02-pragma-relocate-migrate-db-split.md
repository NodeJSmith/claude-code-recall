---
task_id: "T02"
title: "Relocate apply_base_pragmas, split migrate_db"
status: "planned"
depends_on: ["T01"]
implements: ["FR#3", "FR#7", "FR#8", "AC#3", "AC#5", "AC#6", "AC#7"]
---

## Summary
Consolidate the base-pragma triple and split `migrate_db`. Move `apply_base_pragmas` from `db.py` to the cycle-free `models.py` so `migrations.py` can use it without creating a `db → migrations → db` import cycle. Decompose `migrate_db` (~61L) along its existing seams into a detect-and-decide orchestrator over two helpers, and make its recreate path call `apply_base_pragmas` — which adds the missing `foreign_keys=ON`. That addition is a **non-observable consistency change** (the recreate connection runs DDL only then closes; `foreign_keys` is connection-scoped with no on-disk effect), so it is verified by inspection, NOT a runtime assertion. The whole task is behavior-preserving — `migrate_db`'s boolean/decision matrix (existing `TestMigrateDb`) stays green.

## Target Files
- modify: `src/ccrecall/models.py`
- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/migrations.py`
- modify: `tests/test_migrations.py`
- read: `design/specs/002-db-write-path-refactor/design.md`
- read: `design/specs/002-db-write-path-refactor/tasks/context.md`

## Prompt
Read `tasks/context.md` and `design/specs/002-db-write-path-refactor/design.md` (`## Architecture` → the `migrate_db` split + pragma relocation bullets, `## Replacement Targets`).

**1. Relocate `apply_base_pragmas` to `models.py`.**
- Move the `apply_base_pragmas` function definition (currently `src/ccrecall/db.py:69`) into `src/ccrecall/models.py`, placed near `BUSY_TIMEOUT_MS` (which it references). Add `import sqlite3` to `models.py` (it currently imports only `logging` + pydantic). Keep the body and docstring identical.
- In `db.py`: delete the local definition and import the name from models: add `apply_base_pragmas` to the existing `from ccrecall.models import ...` (or add such an import). Its two call sites — `db.py:254` and `db.py:261` — keep calling `apply_base_pragmas(conn)` unchanged.
- In `migrations.py`: add `apply_base_pragmas` to the existing `from ccrecall.models import BUSY_TIMEOUT_MS` (line 17). After the recreate path swaps the inline `f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}"` for `apply_base_pragmas(new_conn)`, `BUSY_TIMEOUT_MS` becomes unused in `migrations.py` (it was the only use) — **remove it from that import** so the line reads `from ccrecall.models import apply_base_pragmas`. (Confirm with a grep that no other `migrations.py` line references `BUSY_TIMEOUT_MS` before removing.)
- Note on `models.py`: its docstring describes it as boundary pydantic models, but it already deliberately hosts cross-cutting cycle-free infra (`LOGGER_NAME`, `BUSY_TIMEOUT_MS`) "so db/session_ops/hooks can import it without a cycle" — `apply_base_pragmas` joins them for the same reason. No docstring rewrite needed; a one-line comment above the function noting it lives here to stay cycle-free is enough.

**2. Split `migrate_db`** (`src/ccrecall/migrations.py:26-86`) into a detect orchestrator + two helpers (drop the leading `_` on the new helpers per the naming convention in context.md):
- `nuke_old_db(conn, db_path) -> bool` — the destructive block (the body of `if db_path and db_path.exists():`, lines ~55-69; `db_path` is already resolved by the orchestrator and passed in): call `_backup_db_before_migration(db_path, "pre-v3-nuke")`, **refuse to destroy** (return False / signal abort) when backup fails, else `conn.close()`, `db_path.unlink()`, and the `-wal`/`-shm` cleanup. Preserve the refuse-to-destroy semantics exactly.
- `recreate_fresh_schema(db_path) -> None` — the fresh connect + schema create (lines ~71-86): connect, **call `apply_base_pragmas(new_conn)`** in place of the inline `PRAGMA journal_mode = WAL` + `PRAGMA busy_timeout` pair (lines 73-74) — this adds `foreign_keys=ON` — then `detect_fts_support`, `executescript(SCHEMA_CORE)` + the FTS variant, `commit()`, `close()`.
- `migrate_db` keeps: the branches/sessions schema-detection probes, the early `return False` paths (already-v3, fresh DB), resolving `db_path` from `PRAGMA database_list`, and orchestration: if the db_path exists, `nuke_old_db(...)` (returning False on backup failure as today), then `recreate_fresh_schema(db_path)`, `return True`. Net: `migrate_db` reads as a decision orchestrator under the guideline; the boolean return and nuke/no-op decision are byte-identical to today aside from the added connection-scoped `foreign_keys=ON`.

**3. Do NOT add a `foreign_keys` runtime assertion.** The `foreign_keys=ON` on the recreate connection is non-observable — that connection runs DDL only and is closed immediately (matching current lines 82-85), and `foreign_keys` is connection-scoped with no on-disk persistence, so there is no surface to assert it on after the fact. Verify the change by **inspection**: confirm `recreate_fresh_schema` calls `apply_base_pragmas(new_conn)` instead of the inline WAL+busy_timeout pair. The observable behavior of `migrate_db` — the boolean return and nuke/no-op decision per starting schema — is pinned by the existing `TestMigrateDb` (relocated into `tests/test_migrations.py` by T01), which must stay **green unchanged** through this split. Do not weaken or rewrite those assertions. (This is the AC#3 observability gap, named in the design — not a verification shortcut.)

Run `uv run pytest -q` — full suite green, including the T01 pins and `TestMigrateDb`, all unchanged.

## Focus
- **Import cycle is the trap.** `migrations.py` imports from `ccrecall.schema`, `ccrecall.content`, `ccrecall.parsing`, `ccrecall.summarizer`, `ccrecall.models` — **never** `ccrecall.db`. `db.py` imports `migrate_columns`/`migrate_db` from `migrations.py`. So `migrate_db` must reach `apply_base_pragmas` via `models.py`, never via `db.py`. Add `from ccrecall.models import apply_base_pragmas` (or extend the existing models import) to `migrations.py`.
- `apply_base_pragmas` has exactly two call sites in `db.py` (254, 261) and zero references in tests — confirmed by grep. The relocation ripple is contained to `db.py` + `models.py` + `migrations.py`.
- The `foreign_keys=ON` addition is connection-scoped, on a DDL-only connection closed immediately, so it changes nothing observable — do not try to assert it (see step 3). It exists purely so `migrate_db` stops diverging from the canonical `apply_base_pragmas`.
- This task is fully behavior-preserving: the split plus a non-observable consistency change. One commit is fine; there is no RED→GREEN behavior delta to sequence.

## Verify
- [ ] FR#3: `migrate_db` returns the same boolean and nuke/no-op decision per starting schema (pre-v3 → True+recreate, fresh → False, already-v3 → False); `recreate_fresh_schema` routes through `apply_base_pragmas` (inspection — the non-observable `foreign_keys=ON` consistency change).
- [ ] AC#3: the existing `TestMigrateDb` boolean/decision matrix stays green unchanged; no `foreign_keys` runtime assertion is added (observability gap named in design).
- [ ] FR#7: `migrate_db`, `nuke_old_db`, and `recreate_fresh_schema` are each ≤50 lines (or migrate_db is a pure orchestrator over the two helpers).
- [ ] FR#8: `migrate_db` and `migrate_columns` remain importable from `ccrecall.migrations` with unchanged signatures; `apply_base_pragmas` is importable from `ccrecall.models` and still callable from `db.py`.
- [ ] AC#5: no function added/touched in this task exceeds the line guideline except a pure orchestrator.
- [ ] AC#6: `uv run pytest -q` passes with zero failures.
- [ ] AC#7: importing `migrate_db`, `migrate_columns` from `ccrecall.migrations` and `apply_base_pragmas` from `ccrecall.models` succeeds.
