---
task_id: "T03"
title: "Split session_ops.py into focused modules"
status: "done"
depends_on: ["T02"]
implements: ["FR#1", "AC#1", "AC#2", "AC#3"]
---

## Summary
Decompose the 744-line `session_ops.py` into five focused modules along natural responsibility seams. This is the largest split and creates `embed_ops.py` which later tasks depend on. Each extracted module owns a single concern; the slimmed `session_ops.py` becomes a thin orchestrator importing from the four new modules. All existing tests must continue passing with updated imports and mock.patch targets.

## Target Files
- create: `src/ccrecall/import_log_ops.py`
- create: `src/ccrecall/message_ops.py`
- create: `src/ccrecall/branch_ops.py`
- create: `src/ccrecall/embed_ops.py`
- modify: `src/ccrecall/session_ops.py`
- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- read: `src/ccrecall/hooks/sync_current.py` (imports `sync_session` which stays in session_ops — no edit needed)
- read: `src/ccrecall/hooks/import_conversations.py` (same — no edit needed)
- modify: `tests/test_session_ops.py`
- modify: `tests/test_backfill_embeddings.py`
- read: `src/ccrecall/db.py` (for import references)
- read: `src/ccrecall/summarizer.py` (imported by embed_ops for `compute_context_summary`)
- read: `src/ccrecall/embeddings.py` (imported by embed_ops for `embed_text`)

## Prompt
### Module extraction

Read `src/ccrecall/session_ops.py` fully. Extract functions into new modules per this mapping:

**`src/ccrecall/import_log_ops.py`** (~50 lines):
- `import_log_skip_check` (lines 49-71)
- `upsert_import_log` (lines 625-649)
- Move only the imports these functions need

**`src/ccrecall/message_ops.py`** (~90 lines):
- `upsert_session` (lines 74-92)
- `build_message_row` (lines 95-129)
- `insert_new_messages` (lines 132-161)

**`src/ccrecall/branch_ops.py`** (~220 lines):
- `update_branch_row` (lines 164-200)
- `insert_branch_row` (lines 203-238) — note: T02 already removed `fork_point_uuid` from the INSERT
- `upsert_branch` (lines 241-297)
- `diff_branch_messages` (lines 300-333)
- `sync_branch` (lines 573-623)
- `sync_branch` calls `upsert_branch` and `diff_branch_messages` (both in `branch_ops`), plus `write_branch_summary` and `embed_branch_chunks` (both in `embed_ops`), plus `build_aggregated_content` from `parsing.py` and `fetch_branch_messages` from `db.py` — import from `embed_ops` for the summary/embedding calls

**`src/ccrecall/embed_ops.py`** (~240 lines):
- `write_branch_summary` (lines 335-366)
- `MAX_WRITE_PATH_EMBEDS_PER_SYNC` constant (line 374)
- `_stamp_branch_watermark` (lines 377-388)
- `embed_branch_chunks` (lines 390-570)
- Preserve the embedding watermark protocol exactly (clear-first/set-last transaction boundary)

**`src/ccrecall/session_ops.py`** (slimmed ~95 lines):
- `sync_session` (lines 652-744) — the only remaining function
- Import from all four new modules

### Import updates

For each file that imports from `session_ops`, update the import path:

- `src/ccrecall/hooks/backfill_embeddings.py` line 47: `from ccrecall.session_ops import embed_branch_chunks` → `from ccrecall.embed_ops import embed_branch_chunks`
- `src/ccrecall/hooks/sync_current.py` line 36: `from ccrecall.session_ops import sync_session` — this can stay since `sync_session` remains in `session_ops.py`
- `src/ccrecall/hooks/import_conversations.py` line 22: same — `sync_session` stays in `session_ops.py`

### Test updates

In `tests/test_session_ops.py`:
- Update direct imports of extracted functions (e.g., `embed_branch_chunks`, `MAX_WRITE_PATH_EMBEDS_PER_SYNC`) to their new modules
- **Critical**: Update ~12 `patch("ccrecall.session_ops.embed_text", ...)` calls to `patch("ccrecall.embed_ops.embed_text", ...)` — after the split, `embed_text` resolves in `embed_ops`'s namespace
- Update ~1 `patch("ccrecall.session_ops.compute_context_summary", ...)` to `patch("ccrecall.embed_ops.compute_context_summary", ...)`

In `tests/test_backfill_embeddings.py`:
- Update `from ccrecall.session_ops import MAX_WRITE_PATH_EMBEDS_PER_SYNC` to `from ccrecall.embed_ops import MAX_WRITE_PATH_EMBEDS_PER_SYNC` (line 27)
- Update ~8 `patch("ccrecall.session_ops.embed_text", ...)` calls to `patch("ccrecall.embed_ops.embed_text", ...)`

### Boundary types

Pass plain Python types between the extracted modules — no new dataclasses or custom types crossing boundaries. Functions receive and return the same types they do today.

## Focus
- `sync_branch` in `branch_ops.py` will import from `embed_ops` (for `write_branch_summary` and `embed_branch_chunks`) — verify no circular imports. It does NOT call anything from `message_ops`
- `write_branch_summary` imports `compute_context_summary` from `summarizer.py`; `embed_branch_chunks` imports `embed_text` from `embeddings.py` and `build_exchange_pairs` from `summarizer.py` — these cross-module imports stay as-is in `embed_ops.py`
- The `write_branch_summary` function catches `(ValueError, TypeError, KeyError)` for content errors and `sqlite3.Error` for infra errors — preserve this exactly in `embed_ops.py`
- `sync_session` in the slimmed `session_ops.py` calls functions from all four new modules — trace the call graph to ensure all needed imports are present
- After this task, no single source file should exceed 400 lines (AC#1)
- Run `uv run pytest tests/test_session_ops.py tests/test_backfill_embeddings.py` to verify the mock.patch target updates don't silently fail

## Verify
- [ ] FR#1: `session_ops.py` is decomposed into 5 focused modules (import_log_ops, message_ops, branch_ops, embed_ops, slimmed session_ops)
- [ ] AC#1: No source file created by this task exceeds 400 lines (the 4 new modules + slimmed session_ops.py; `backfill_embeddings.py` remains >400 until T05)
- [ ] AC#2: `uv run pytest` passes with zero failures
- [ ] AC#3: `uvx prek run --all-files` passes
