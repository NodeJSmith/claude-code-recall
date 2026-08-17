---
task_id: "T03"
title: "Consolidate FIXTURE_DIR and _make_conn test infrastructure"
status: "planned"
depends_on: []
implements: ["FR#7", "FR#8"]
---

## Target Files

- modify: `tests/test_project_ops.py`
- modify: `tests/test_integration.py`
- modify: `tests/test_sync_hook.py`
- modify: `tests/test_import_pipeline.py`
- modify: `tests/test_session_ops.py`
- modify: `tests/test_context_alerts.py`
- modify: `tests/test_backfill_tool_content.py`

## Prompt

Consolidate duplicated test infrastructure. Read `design/specs/011-wave-1-logging-and-test-infra/tasks/context.md` for conventions.

### Part 1: FIXTURE_DIR (5 files)

`conftest.py` already defines `FIXTURE_DIR = Path(__file__).parent / "fixtures"` at line 14. Five test files redefine it identically. In each file:

1. Remove the line `FIXTURE_DIR = Path(__file__).parent / "fixtures"`
2. Add `from conftest import FIXTURE_DIR` (or add `FIXTURE_DIR` to an existing conftest import if one exists)
3. If the file imported `Path` only for the `FIXTURE_DIR` definition and has no other `Path` usage, remove the unused `Path` import

Files to change:
- `tests/test_project_ops.py` (line 9)
- `tests/test_integration.py` (line 33)
- `tests/test_sync_hook.py` (line 26)
- `tests/test_import_pipeline.py` (line 19)
- `tests/test_session_ops.py` (line 23)

Do NOT touch `tests/test_llm_summary_evaluation.py` — it uses a different fixtures subdirectory (`fixtures/llm_summary_evaluation`).

### Part 2: _make_conn (2 files)

`_make_conn()` in `test_context_alerts.py` and `test_backfill_tool_content.py` creates an in-memory SQLite connection with schema applied — functionally identical to the `memory_db` fixture in conftest.

For each file:

1. Delete the `_make_conn()` function definition
2. Remove any imports that were only used by `_make_conn()` (check `sqlite3`, `SCHEMA` imports)
3. Replace every `conn = _make_conn()` call site with a `memory_db` fixture parameter on the test method

For `test_context_alerts.py`: the `_make_conn()` calls are inside test methods of a class. Add `memory_db` as a method parameter and replace `conn = _make_conn()` with `conn = memory_db`. Example:
```python
# Before:
def test_something(self):
    conn = _make_conn()
# After:
def test_something(self, memory_db):
    conn = memory_db
```

For `test_backfill_tool_content.py`: same pattern. Check if any test creates a cursor from conn (`conn.cursor()`) — that should still work with `memory_db`.

Important: verify that `memory_db` in conftest applies the same schema and pragmas as `_make_conn()`. Both use `SCHEMA` (which is `SCHEMA_CORE + SCHEMA_FTS5`) with `PRAGMA foreign_keys = ON`.

### Verification

Run full test suite: `uv run pytest` — all 1084+ tests must pass.
Run lint: `uvx prek run --all-files` — must pass.

## Verify

- [ ] FR#7: `grep -rn 'FIXTURE_DIR = Path' tests/` returns only `tests/conftest.py`
- [ ] FR#8: `grep -rn 'def _make_conn(' tests/` returns only `test_summarizer.py` (the unrelated `_make_conn_for_sync_branch`)
- [ ] AC#1: `uv run pytest` passes with zero failures
