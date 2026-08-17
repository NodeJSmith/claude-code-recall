# Context: Wave 3 Structural Decompositions

## Problem & Motivation

Three structural issues from the codebase health audit block safe evolution: `db.py` couples `get_connection()` to numpy via a module-scope embeddings import (every SessionStart hook pays ~200ms+ import tax), `session_tail.py` (653 lines) mixes four concerns, and three functions (198-273 lines each) carry critical invariants in monolith bodies. Wave-2 safety-net tests are in place — this wave does the structural refactoring they protect.

## Key Decisions

1. Vec-dependent code moves to a new `db_vec.py` rather than staying in `db.py` with re-exports (re-exports would re-introduce the numpy import chain).
2. All 12 callers of vec functions update their imports from `ccrecall.db` to `ccrecall.db_vec`. No compatibility shim.
3. The `_open_connection` `load_vec=True` branch uses a conditional import of `db_vec` — the one accepted lazy import, governed by CLAUDE.md's hook-hot-path invariant (#3) taking precedence over the no-lazy-imports rule.
4. `session_tail.py` splits into `tail_resolve.py` (path resolution), `tail_pending.py` (pending-question detection), and a slimmed `session_tail.py` (tail rendering + CLI). `context_rendering.py` updates its imports from the new modules.
5. `embed_branch_chunks` decomposes into named-step helpers within `embed_ops.py` (no new module needed).
6. The two backfill `run()` functions are NOT unified — they diverge in tested, load-bearing ways (abort vs skip-and-continue on stuck batches, different progress math, different SAVEPOINT handling). Only `embed_branch_chunks` is decomposed.
7. `db.py`'s `_apply_migrations` wraps `vec_available` in a deferred-import closure so numpy is only imported when actually migrating (not on every connection).

## Constraints

- No behavioral changes — this is pure structural refactoring. All existing tests must pass without modification.
- Dead schema columns stay (per CLAUDE.md — wait for next `branches` table rebuild).
- The `CONTENT_ERROR_VERSION`, `EMBEDDABLE_BRANCH_FILTER`, `CHUNK_EMBEDDABLE_BRANCH_FILTER` constants stay in `db.py` (they're plain strings/ints with no heavy deps, and many modules import them).
- `DEFAULT_PROJECTS_DIR` stays importable from `ccrecall.db` (re-exported from `ccrecall.config`).
- The hook hot-path invariant (invariant #3 in CLAUDE.md) governs the db.py split — `get_connection()` must not pull numpy.
- Tasks are sequential: T01 changes import paths that T02 and T03 depend on.
