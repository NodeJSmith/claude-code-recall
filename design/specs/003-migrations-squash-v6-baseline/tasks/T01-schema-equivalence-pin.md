---
task_id: "T01"
title: "Add schema-equivalence snapshot pin for the conversation DB"
status: "done"
depends_on: []
implements: ["AC#1"]
---

## Summary
Land a characterization pin that captures the full schema a fresh conversation DB produces today, so the squash can be proven behavior-preserving. The pin snapshots the set of tables (excluding the dead `token_snapshots`), each table's columns+order+types via `PRAGMA table_info`, and all indexes — from a DB built the production way (`get_db_connection`). It must be green on the *current* code and stay green through the DDL lift (T02) and the deletion (T04), proving a fresh DB's schema is identical to today's minus `token_snapshots`. This is the `refactoring-discipline.md` pin-before-move step: nothing structural ships until this is green.

## Target Files
- modify: `tests/test_db.py`
- read: `src/ccrecall/db.py`
- read: `src/ccrecall/schema.py`
- read: `design/specs/003-migrations-squash-v6-baseline/design.md`

## Prompt
Add a new test to `tests/test_db.py` that pins the conversation DB schema produced by the real connection path.

The test must:
1. Build a fresh conversation DB through the production path — `get_db_connection` against a temp file path (use a `tmp_path` fixture / `settings={"db_path": ...}` so it's a real file, not `:memory:`, to match production). On current code this applies `SCHEMA_CORE` + FTS + `migrate_columns`.
2. Snapshot, into an inline expected literal in the test:
   - The set of user tables (from `sqlite_master WHERE type='table'`), **excluding** `token_snapshots`, and excluding SQLite internals (`sqlite_*`) and FTS shadow tables (the `*_fts*` auto-created tables — capture only `messages_fts` / `branches_fts` virtual tables or exclude all `_fts` shadow tables; pick one consistent rule and document it in a comment).
   - For each captured table: `PRAGMA table_info(<table>)` as an ordered list of `(name, type)` (and any other column attributes you choose), preserving column order.
   - The set of indexes from `sqlite_master WHERE type='index'` whose names start with `idx_` (skip auto-indexes).
3. Assert the snapshot equals the expected literal.

Derive the expected literal by running the test once against current code and pasting the observed schema (so it is green immediately). Add a comment explaining that this pin guards the squash: after T02 lifts the embedding DDL into `SCHEMA_CORE` and T04 deletes `migrations.py`, this same test must still pass — proving the fresh-DB schema is unchanged except for the intentionally-removed `token_snapshots`.

**Calibration trap — the exclusion must be in the snapshot-building code, not just the pasted literal.** On current code `migrate_columns` creates `token_snapshots` on every fresh DB, so it WILL appear in the live query output. The snapshot-builder must filter `token_snapshots` out of the queried table set **before** you derive/paste the expected literal — so the expected literal never contains it. If you paste a raw observed snapshot that includes `token_snapshots`, the pin is green today but goes red at T04 for the wrong reason (T04 correctly stops creating it). Concretely: in the test, exclude `token_snapshots` in the `WHERE` clause / Python filter that builds the table set, and confirm by eye that the expected literal has no `token_snapshots` entry. (AC#3 separately asserts its absence in T04.)

See the design doc's `## Test Strategy → New Test Coverage` and `## Edge Cases` (column-order drift) for the intent.

## Focus
- `get_db_connection(settings, load_vec=...)` lives at `src/ccrecall/db.py:241`. Pass `settings={"db_path": str(tmp_path / "conv.db")}`. Default `load_vec=False` is correct — the vec virtual table (`branch_vec`) is only created on `load_vec=True` and is out of scope for this pin; do not load vec.
- Current `SCHEMA_CORE` (`src/ccrecall/schema.py:12`) already contains all `messages` columns and most `branches` columns; `migrate_columns` appends `embedding_version`, `embedding_model`, `summary_version_at_embed` and creates `idx_branches_embedding_version`. So the expected `branches` column list (today) ends with those three, and the expected index set includes `idx_branches_embedding_version` and `idx_branches_summary_version`. The pin captures the *combined* result, which is exactly what a schema-only DB must reproduce post-squash.
- FTS shadow tables (`messages_fts_data`, `messages_fts_idx`, etc.) are created by the FTS5 virtual table. Choose a deterministic exclusion rule (e.g. exclude any name containing `_fts`) and keep it stable across the comparison so the snapshot is reproducible across SQLite builds. Note: an FTS-disabled SQLite build won't create them — guard the test to the FTS5 path or assert only the core tables if FTS is unavailable (mirror how other tests in `test_db.py` handle FTS availability via `detect_fts_support`).
- This test does NOT import from `ccrecall.migrations`. It only uses `get_db_connection`, so it survives the deletion in T04 unchanged.

## Verify
- [ ] AC#1: A schema-snapshot test in `tests/test_db.py` captures tables (excluding `token_snapshots`), per-table `table_info` (column names + order + types), and `idx_*` indexes from a `get_db_connection`-built fresh DB, asserts against an inline expected literal, and passes on current code (`uv run pytest -q tests/test_db.py -k <pin name>`).
