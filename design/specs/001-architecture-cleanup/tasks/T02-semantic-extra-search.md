---
task_id: "T02"
title: "Make semantic search optional"
status: "planned"
depends_on: ["T01"]
implements: ["FR#1", "FR#2", "FR#3", "FR#4", "AC#1", "AC#2", "AC#3"]
---

## Summary

Move native semantic dependencies behind an optional `semantic` extra. Keep base `ccrecall search` working through FTS/LIKE only, and keep semantic installs using fused keyword/vector search. Make `search-messages` explicitly caveat when semantic exchange search is unavailable.

## Target Files

- modify: `pyproject.toml`
- create: `src/ccrecall/semantic_store.py`
- modify: `src/ccrecall/embeddings.py`
- modify: `src/ccrecall/search_conversations.py`
- modify: `src/ccrecall/formatting.py`
- modify: `src/ccrecall/session_ops.py`
- modify: `src/ccrecall/hooks/sync_current.py`
- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- modify: `src/ccrecall/hooks/warm_model.py`
- modify: `src/ccrecall/hooks/memory_setup.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `tests/conftest.py`
- modify: `tests/test_db.py`
- modify: `tests/test_embeddings.py`
- modify: `tests/test_search.py`
- modify: `tests/test_backfill_embeddings.py`
- modify: `tests/test_sync_hook.py`
- modify: `tests/test_context_injection.py`
- read: `design/specs/001-architecture-cleanup/design.md`
- read: `design/specs/001-architecture-cleanup/tasks/context.md`

## Prompt

Implement the `Architecture -> Optional Semantic Boundary` section.

Move `sqlite-vec`, `fastembed`, and `numpy` out of base dependencies and into `[project.optional-dependencies].semantic`. If `numpy` is no longer needed after rewriting normalization, do not include it in the extra. Update dev/test expectations so semantic tests can run in an environment with the extra, but base tests can simulate absence.

Create `src/ccrecall/semantic_store.py` for sqlite-vec responsibilities: guarded top-level `sqlite_vec` sentinel, vec availability checks, vec schema setup, vector serialization/upsert/write helpers, `chunk_vec_queryable`, and semantic coverage helpers. Do not use function-local imports.

Make `src/ccrecall/embeddings.py` import-safe without fastembed/numpy. Replace numpy normalization with stdlib/list math unless there is a concrete remaining need for numpy. Constants such as model name/version/dim and default threads must remain importable without the extra.

Update `search_conversations.py` so base `ccrecall search` uses FTS5/FTS4/LIKE without loading vec/model. Semantic installs should preserve fused search. `search-messages` without semantic support should exit 0 with a valid empty unranked result plus an explicit caveat telling callers to use `ccrecall search -q ...` for keyword session search. Use the existing output envelope style where possible.

Gate warm-model spawning in `memory_setup.py` on semantic availability. `warm_model.py` and `backfill_embeddings.py` should produce clear absent-extra behavior without breaking hook stdout.

## Focus

Current native import sites include `db.py`, `search_conversations.py`, `embeddings.py`, `session_ops.py`, `sync_current.py`, `backfill_embeddings.py`, `warm_model.py`, `cli/commands.py`, and tests. The repo forbids lazy imports, so make modules import-safe or keep semantic-only modules out of base command registration paths. `tests/test_search.py` has many direct `chunks`/`chunk_vec` fixtures; those may be fully updated in T03, but this task should isolate semantic availability and search caveat behavior.

## Verify

- [ ] FR#1: Base install still supports `ccrecall search -q ...` through keyword/FTS search.
- [ ] FR#2: Base CLI/search/hook/database imports do not require `sqlite-vec`, `fastembed`, or `numpy`.
- [ ] FR#3: Semantic install still returns fused keyword/vector `ccrecall search` results.
- [ ] FR#4: `search-messages` without semantic support exits 0 with an explicit caveat directing callers to `ccrecall search -q ...`.
- [ ] AC#1: Tests simulate missing semantic packages and still import CLI, hooks, DB connection, and keyword search.
- [ ] AC#2: Existing fused search behavior remains covered and passing in semantic-enabled tests.
- [ ] AC#3: Markdown and JSON `search-messages` outputs include the unavailable caveat and empty unranked envelope.
