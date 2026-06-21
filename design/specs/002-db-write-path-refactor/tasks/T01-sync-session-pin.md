---
task_id: "T01"
title: "Pin sync_session DB-write behavior before the split"
status: "planned"
depends_on: []
implements: ["AC#1"]
---

## Summary
Establish the characterization-test baseline that the split must keep green, on the **current** (unrefactored) code. The existing public-level suites (`test_session_ops.py`, `test_import_pipeline.py`, `test_sync_hook.py`) already pin most of `sync_session`, but two write paths are unasserted: the `import_log` NULL-hash-stale update path and the exact-hash `-1` skip. Add a DB-state golden pin for those. This task changes **tests only** — no `src/` changes — and must be green before T02 splits.

## Target Files
- modify: `tests/test_session_ops.py`
- read: `src/ccrecall/session_ops.py`
- read: `tests/conftest.py`
- read: `tests/test_import_pipeline.py`
- read: `tests/test_sync_hook.py`
- read: `design/specs/002-db-write-path-refactor/design.md`

## Prompt
Read `design/specs/002-db-write-path-refactor/design.md` (especially `## Test Strategy`, `## Edge Cases`, `## Acceptance Criteria`) and `tasks/context.md`.

Survey the existing `sync_session` coverage in `tests/test_session_ops.py`, `tests/test_import_pipeline.py`, and `tests/test_sync_hook.py`. Identify the write paths they leave unasserted and add a DB-state golden pin in `tests/test_session_ops.py` for:
- **NULL-hash-stale `import_log` update path**: when `write_import_log=True` and a prior `import_log` row exists with a **NULL** `file_hash`, calling `sync_session` with a provided `file_hash` re-processes the file and **updates** that row (sets `file_hash`, `imported_at`, `messages_imported`) — it does NOT skip. Assert the exact resulting `import_log` row contents.
- **Exact-hash `-1` skip**: when `write_import_log=True` and a prior `import_log` row exists with a **non-NULL** `file_hash` equal to the provided `file_hash`, `sync_session` returns `-1` and writes nothing new. Assert the `-1` return and that message/branch counts are unchanged.

Reuse the `memory_db` fixture and `fixtures/*.jsonl`. Use a real fixture session file; drive the two scenarios by pre-seeding the `import_log` row (NULL hash vs matching hash) before the second `sync_session` call.

Run `uv run pytest -q tests/test_session_ops.py` then the full `uv run pytest -q` — everything must pass on current `src/`.

## Focus
- `tests/conftest.py`'s `memory_db` runs `executescript(SCHEMA)` then `migrate_columns`; it's a complete current-schema DB. Reuse it.
- `sync_session`'s import_log dedup is at session_ops.py:77–87 (skip decision) and the import_log write is at session_ops.py:367–385 (UPDATE-vs-INSERT). Read both to get the exact column set you must assert (`file_hash`, `imported_at`, `messages_imported`).
- This is the RED-baseline of the refactor sequence (`sequence-verifiable-units.md`): the pin is the evidence the split preserves behavior. Commit it as its own unit, green on unrefactored code.
- Tests only — do not modify `src/`.

## Verify
- [ ] AC#1: `tests/test_session_ops.py` has a DB-state pin covering the NULL-hash-stale `import_log` update path (exact row asserted) and the exact-hash `-1` skip; it passes on current `src/`, and the existing `test_session_ops.py`/`test_import_pipeline.py`/`test_sync_hook.py` still pass unchanged.
