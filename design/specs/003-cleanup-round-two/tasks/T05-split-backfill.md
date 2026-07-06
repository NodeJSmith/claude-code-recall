---
task_id: "T05"
title: "Split backfill_embeddings.py into focused modules"
status: "planned"
depends_on: ["T03"]
implements: ["FR#3", "AC#1", "AC#2", "AC#3"]
---

## Summary
Decompose the 491-line `hooks/backfill_embeddings.py` into three focused modules. Query construction/constants and status reporting move to their own modules; the slimmed file retains only `run()`. Depends on T03 because `backfill_embeddings.py` imports `embed_branch_chunks` from `session_ops`, which T03 moves to `embed_ops`.

## Target Files
- create: `src/ccrecall/hooks/backfill_query.py`
- create: `src/ccrecall/hooks/backfill_status.py`
- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `tests/test_backfill_embeddings.py`
- read: `src/ccrecall/embed_ops.py` (created by T03 — import source for embed_branch_chunks)
- read: `src/ccrecall/config.py` (imported by backfill for settings/logging)
- read: `src/ccrecall/db.py` (imported by backfill for get_connection)

## Prompt
### Module extraction

Read `src/ccrecall/hooks/backfill_embeddings.py` fully. Extract functions and constants into new modules:

**`src/ccrecall/hooks/backfill_query.py`** (~60 lines):
- `cleanup_pid` (lines 64-66)
- `days_modifier` (lines 69-75)
- `build_selection` (lines 78-114)
- Constants: `BATCH_SIZE`, `BACKFILL_BATCH_DELAY_SECONDS`, `DEFAULT_PROGRESS_EVERY`, `BACKFILL_NICE_LEVEL`, `EXIT_OK`, `EXIT_ABORT`, `PID_KEY`
- Move only the imports these functions/constants need

**`src/ccrecall/hooks/backfill_status.py`** (~100 lines):
- `count_status` (lines 117-207)
- `format_duration` (lines 210-219)
- `run_status` (lines 222-255)
- Import `build_selection` and `days_modifier` from `backfill_query`

**`src/ccrecall/hooks/backfill_embeddings.py`** (slimmed ~330 lines):
- `run()` (lines 258-491) — the only remaining function
- Import constants and helpers from `backfill_query` and `backfill_status`
- `run()` retains the `with get_connection(settings, load_vec=True) as conn:` block — the connection lifecycle stays in the orchestrator

### Import updates

In `src/ccrecall/cli/commands.py`:
- The file does `from ccrecall.hooks import backfill_embeddings as backfill_embeddings_mod` and then uses:
  - `backfill_embeddings_mod.DEFAULT_PROGRESS_EVERY` (line 147) — moves to `backfill_query`
  - `backfill_embeddings_mod.cleanup_pid()` (line 164) — moves to `backfill_query`
- Update to import these from `backfill_query` instead. The `run()` call still comes from `backfill_embeddings_mod.run(...)`.
- Also update the stale comment at line 94 that references `session_ops.upsert_branch` → `branch_ops.upsert_branch` (T03 moved it)

Note: T03 already updated `backfill_embeddings.py`'s import of `embed_branch_chunks` from `session_ops` to `embed_ops`. If T03 did not make this change (verify), do it here.

### Test updates

In `tests/test_backfill_embeddings.py`:
- Update `from ccrecall.hooks.backfill_embeddings import BATCH_SIZE, EXIT_ABORT, EXIT_OK, run` — `BATCH_SIZE`, `EXIT_ABORT`, `EXIT_OK` now come from `backfill_query`; `run` stays in `backfill_embeddings`
- Verify T03 already updated the `MAX_WRITE_PATH_EMBEDS_PER_SYNC` import and mock.patch targets — if not, do it here

## Focus
- `run()` is 234 lines and stays in `backfill_embeddings.py` — the file will be ~330 lines total (run + imports/docstring), well under 400
- The extracted functions are already separate top-level functions today — moving them to new modules is a clean cut with no intra-function extraction needed
- `cli/commands.py` uses the `backfill_embeddings_mod` alias pattern (imports the module, accesses attributes on it) — need to restructure to either import the new modules or import specific symbols
- `count_status` calls `build_selection` — this becomes a cross-module call (`backfill_status` → `backfill_query`), which is fine
- `run_status` calls `count_status` — both are in `backfill_status`, no cross-module call needed
- `run()` calls `build_selection` and references `BATCH_SIZE`, `BACKFILL_BATCH_DELAY_SECONDS`, `DEFAULT_PROGRESS_EVERY`, `BACKFILL_NICE_LEVEL`, `EXIT_OK`, `EXIT_ABORT`, `PID_KEY` — these become imports from `backfill_query`. Note: `cleanup_pid` is called only from `cli/commands.py`, not from `run()`

## Verify
- [ ] FR#3: `backfill_embeddings.py` is decomposed into 3 focused modules (backfill_query, backfill_status, slimmed backfill_embeddings)
- [ ] AC#1: No created or modified source file exceeds 400 lines
- [ ] AC#2: `uv run pytest` passes with zero failures
- [ ] AC#3: `uvx prek run --all-files` passes
