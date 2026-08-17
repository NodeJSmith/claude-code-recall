# Design: Wave 3 Structural Decompositions

**Date:** 2026-08-17
**Status:** archived
**Mode:** sketch

## Problem

Three structural issues from the codebase health audit (#137, #140, #138) block safe evolution of the codebase. `db.py` couples `get_connection()` to numpy/fastembed via a module-scope import of `ccrecall.embeddings`, so every SessionStart hook pays an avoidable ~200ms+ import tax. `session_tail.py` (653 lines) mixes path resolution, pending-question detection, tail rendering, and CLI orchestration. `embed_branch_chunks` (198 lines) carries the most critical invariant in the codebase (the embedding watermark protocol) in a function too large to review confidently.

## Goals

- `get_connection()` importable without pulling numpy/fastembed/onnxruntime
- `session_tail.py` split into cohesive modules under 300 lines each
- `embed_branch_chunks` decomposed into named steps under 80 lines each (the two backfill `run()` functions are left as-is — their structural similarity is superficial, and they diverge in tested ways)

## Non-Goals

- Dropping dead schema columns (deferred to next `branches` table rebuild per CLAUDE.md)
- CLI flag changes (#146 — wave 4)
- Duplicated pattern consolidation (#141 — wave 4)
- Decomposing the two backfill `run()` functions (#138 partial — their structural similarity is superficial; they diverge in abort-vs-skip stuck-batch behavior, progress math, and SAVEPOINT handling, all test-covered)

## Functional Requirements

- **FR#1** Importing `get_connection` does not transitively import `numpy`, `fastembed`, or `onnxruntime`
- **FR#2** All callers of vec-specific functions update their imports from `ccrecall.db` to `ccrecall.db_vec`; no re-exports from `db.py` (re-exports would reintroduce numpy)
- **FR#3** Vec-dependent functions (`vec_available`, `chunk_vec_queryable`, `_ensure_vec_schema`, `upsert_chunk_vec`, `write_chunk_embedding`, `fetch_branch_messages`, `branch_embedding_coverage`) importable from `ccrecall.db_vec`
- **FR#4** `session_tail.py` split into three modules: path resolution (`tail_resolve.py`), pending-question detection (`tail_pending.py`), and tail rendering + CLI (`session_tail.py` slimmed)
- **FR#5** `embed_branch_chunks` decomposed into named steps (exchange prep, diff/eligibility, embed+write, prune, watermark) each under 80 lines
- **FR#6** `session_tail.py` split avoids circular imports: `tail_pending.py` is self-contained (does not import from `session_tail`); `session_tail.py` imports from both new modules one-directionally
- **FR#7** All existing tests pass without modification (characterization tests from wave 2 are the safety net)

## Acceptance Criteria

- **AC#1** (FR#1): `python -c "from ccrecall.db import get_connection; import sys; assert 'numpy' not in sys.modules"` exits 0
- **AC#2** (FR#2, FR#3, FR#7): `uv run pytest` — full suite passes with 0 failures, 0 import errors
- **AC#3** (FR#4): `session_tail.py` is under 300 lines; `tail_resolve.py` exists and is under 250 lines; `tail_pending.py` exists and is under 150 lines
- **AC#4** (FR#5): `embed_branch_chunks` body is under 80 lines; extracted helpers are each under 80 lines
- **AC#5** (FR#6): `python -c "from ccrecall.tail_pending import find_pending_question; from ccrecall.session_tail import run"` exits 0 (no circular import)
- **AC#6** (FR#7): `uv run pytest -q` reports the same pass count as before (1178) with 0 failures
- **AC#7**: `uvx prek run --all-files` passes clean

## Approach

### T01: Split db.py (#137)

The root cause is `db.py:18`: `from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION`. This pulls numpy onto every `get_connection()` caller.

**Strategy: move vec-dependent code out of `db.py` into a new `db_vec.py`.**

`db.py` keeps: `get_connection`, `_open_connection`, `apply_base_pragmas`, `escape_like`, `parse_project_filter`, `resolve_db_settings`, the filter constants (`EMBEDDABLE_BRANCH_FILTER`, `CHUNK_EMBEDDABLE_BRANCH_FILTER`, `CONTENT_ERROR_VERSION`), and migration re-exports. Remove `from ccrecall.embeddings import ...` and `import sqlite_vec` from module scope — both are only needed by vec operations.

New `db_vec.py` gets: `vec_available`, `chunk_vec_queryable`, `upsert_chunk_vec`, `write_chunk_embedding`, `fetch_branch_messages`, `branch_embedding_coverage`, `_ensure_vec_schema`, `TRIGGER_CHUNKS_VEC_AD`, `VEC_BUSY_TIMEOUT_MS`. This module imports `sqlite_vec` and `ccrecall.embeddings` for the constants it needs.

`db.py` does NOT re-export vec functions — that would reintroduce the numpy import chain. All callers of vec-specific functions update their imports from `ccrecall.db` to `ccrecall.db_vec`. The filter constants stay in `db.py` since they're plain strings/ints with no heavy deps.

**Migration path fix**: `db.py`'s `_apply_migrations` currently passes `prepare=vec_available` to `db_base._apply_migrations`. After `vec_available` moves to `db_vec.py`, this would be a NameError. Fix: wrap in a deferred-import closure so numpy is only imported when `prepare` actually fires (i.e., when `user_version < SCHEMA_VERSION` — a one-time event per DB version bump, not on every connection):

```python
def _apply_migrations(conn):
    def _prepare_vec(c):
        from ccrecall.db_vec import vec_available
        return vec_available(c)
    db_base._apply_migrations(conn, prepare=_prepare_vec, ...)
```

On a fully migrated DB, `_prepare_vec` is never called, so numpy is never imported. This preserves the hot-path invariant.

**Callers to update** (from `from ccrecall.db import X` to `from ccrecall.db_vec import X`):
- `branch_ops.py`: `fetch_branch_messages` → `db_vec`
- `backfill_status.py`: `chunk_vec_queryable` → `db_vec`
- `status.py`: `branch_embedding_coverage`, `chunk_vec_queryable`, `vec_available` → `db_vec`
- `session_ops.py`: `chunk_vec_queryable` → `db_vec`
- `embed_ops.py`: `write_chunk_embedding` → `db_vec`
- `search_conversations.py`: `branch_embedding_coverage`, `chunk_vec_queryable` → `db_vec`
- `search_cli.py`: vec-specific imports → `db_vec`
- `backfill_embeddings.py`: `chunk_vec_queryable`, `fetch_branch_messages` → `db_vec`
- `sync_current.py`: `chunk_vec_queryable` → `db_vec`
- `backfill_tool_content.py`: `VEC_BUSY_TIMEOUT_MS` → `db_vec`
- `recent_chats.py`: vec-specific imports → `db_vec`
- `import_conversations.py`: vec-specific imports → `db_vec`

Callers that stay on `ccrecall.db` (non-vec imports): `memory_context.py` (`get_connection`), `memory_setup.py` (`CONTENT_ERROR_VERSION`, `get_connection`), `session_tail.py` (`DEFAULT_PROJECTS_DIR`), `cli/commands.py` (`DEFAULT_PROJECTS_DIR`), `search_query.py` (`escape_like`), `backfill_query.py` (filter constants), `backfill_summaries.py` (`get_connection`).

The `_open_connection` function currently calls `vec_available(conn)` and `_ensure_vec_schema(conn)` inline when `load_vec=True`. After the split, it conditionally imports `db_vec` only inside the `if load_vec:` branch — this is the one acceptable lazy import, guarded by a runtime flag rather than placed at module scope, and it exists to preserve the architectural invariant (no numpy on the hook path). The no-lazy-imports rule's `TYPE_CHECKING` exemption doesn't cover this, but the CLAUDE.md hook-hot-path invariant (#3) takes precedence as the governing constraint.

### T02: Split session_tail.py (#140)

Extract two modules from `session_tail.py`:

**`tail_resolve.py`** (~180 lines) — path resolution and session selection:
- `transcript_dir`, `transcript_for_uuid`, `list_transcripts`, `resolve_target`, `resolve_target_global`
- `_last_event_timestamp`, `_extract_branch`, `_pick_branch_match`
- `_build_search_dirs`, `_resolve_across_dirs`
- Constants: `_TIMESTAMP_TAIL_LINES`, `_BRANCH_HEAD_LINES`

**`tail_pending.py`** (~120 lines) — pending-question detection, formatting, and shared helpers:
- `find_pending_question`, `format_pending_block`
- `typed_instruction`, `_is_main_chain`, `clip` — moved here so `tail_pending.py` is self-contained (no imports from `session_tail`)
- Constants: `_INJECTION_OPTION_CLIP`, `_CLI_OPTION_CLIP`, `_NOISE_PREFIXES`, `_TEXT_CLIP`

**`session_tail.py`** slimmed (~250 lines) — tail rendering + CLI entry points:
- `load_entries`, `load_tail_entries`, `last_typed_instruction`, `last_assistant_text`
- `_tool_event`, `build_tail`, `first_typed_preview`
- `_emit_header`, `emit`, `_emit_full`, `run`
- Imports `clip`, `typed_instruction`, `find_pending_question`, `format_pending_block` from `tail_pending`
- Constants: `DEFAULT_TAIL_EVENTS`, `_HOOK_TAIL_LINES`, `_PREVIEW_CLIP`, `_TOOL_CLIP`

**Circular import avoidance**: `tail_pending.py` imports only from `ccrecall.content` (for `is_tool_result`, `is_task_notification`, `is_teammate_message`, `extract_text_content`). `session_tail.py` imports from `tail_pending` and `tail_resolve`. Neither new module imports from `session_tail`. This is a one-directional dependency graph with no cycles.

`context_rendering.py` currently imports `find_pending_question`, `format_pending_block`, `load_tail_entries`, `transcript_for_uuid` from `session_tail`. After the split, update to import from the new modules directly:
- `find_pending_question`, `format_pending_block` from `ccrecall.tail_pending`
- `transcript_for_uuid` from `ccrecall.tail_resolve`
- `load_tail_entries` stays in `ccrecall.session_tail`

### T03: Decompose monolith functions (#138)

**`embed_branch_chunks`** (198 lines → ~5 helpers + 40-line orchestrator):
- `_prepare_exchange_data(exchanges)` — step 3 (compute text, hash, display per exchange)
- `_diff_exchanges(exchange_data, existing_chunks)` — step 5 (diff, eligibility, prune set)
- `_embed_and_write_chunks(cursor, branch_db_id, needing_embed, exchange_data)` — steps 6-7 (embed batch, write, prune)
- `_check_watermark_status(exchange_data, embedded_indices, existing_chunks)` — step 8 (watermark decision)

**Backfill runners are NOT unified.** Despite structural similarity, the two backfill `run()` functions diverge in tested, load-bearing ways: `backfill_embeddings` aborts on stuck batches (`return EXIT_ABORT`) while `backfill_tool_content` excludes stuck IDs and continues; progress/ETA math differs (windowed rate with warmup vs flat ratio); SAVEPOINT handling differs (`backfill_tool_content` has `backfill_with_retry` with lock-retry backoff). Forcing these into a shared runner would risk silently changing tested behavior. Leave both as-is.

## Changed Files

- modify: `CLAUDE.md` — corrected the "config.py / db.py split" description after the db_vec.py extraction (T01 fixer loop)
- modify: `tests/test_context_injection.py` — corrected a stale docstring claim about db.py importing fastembed (T01 fixer loop)
- modify: `src/ccrecall/db.py` — remove vec functions, remove embeddings import, keep get_connection + constants
- create: `src/ccrecall/db_vec.py` — vec-dependent functions moved from db.py
- modify: `src/ccrecall/branch_ops.py` — update import from db to db_vec
- modify: `src/ccrecall/session_ops.py` — update import from db to db_vec
- modify: `src/ccrecall/embed_ops.py` — update import, decompose embed_branch_chunks
- modify: `src/ccrecall/status.py` — update import from db to db_vec
- modify: `src/ccrecall/search_conversations.py` — update import from db to db_vec
- modify: `src/ccrecall/search_cli.py` — update import from db to db_vec
- modify: `src/ccrecall/recent_chats.py` — update import from db to db_vec
- modify: `src/ccrecall/hooks/backfill_embeddings.py` — update import from db to db_vec
- modify: `src/ccrecall/hooks/backfill_tool_content.py` — update import from db to db_vec
- modify: `src/ccrecall/hooks/backfill_status.py` — update import from db to db_vec
- modify: `src/ccrecall/hooks/sync_current.py` — update import from db to db_vec
- modify: `src/ccrecall/hooks/import_conversations.py` — update import from db to db_vec
- modify: `src/ccrecall/session_tail.py` — extract path resolution and pending-question modules
- create: `src/ccrecall/tail_resolve.py` — path resolution extracted from session_tail
- create: `src/ccrecall/tail_pending.py` — pending-question detection extracted from session_tail
- modify: `src/ccrecall/hooks/context_rendering.py` — update imports from session_tail to tail_pending/tail_resolve
- modify: `tests/conftest.py` — update `_ensure_vec_schema` import from db to db_vec
- modify: `tests/test_db.py` — update vec-specific imports from db to db_vec
- modify: `tests/test_session_ops.py` — update `_ensure_vec_schema, vec_available` imports from db to db_vec
- modify: `tests/test_search.py` — update `upsert_chunk_vec, vec_available, write_chunk_embedding` imports from db to db_vec
