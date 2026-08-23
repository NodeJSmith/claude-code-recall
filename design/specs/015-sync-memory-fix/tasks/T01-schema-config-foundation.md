---
task_id: "T01"
title: "Add schema, config, and foundation constants"
status: "done"
depends_on: []
implements: ["FR#5", "AC#11"]
---

## Summary

Add the foundational pieces everything else builds on: the `cap_tokens` column in the `chunks` table (schema migration v7→v8 with partial index), the `sync_path_token_limit` config setting, the `SYNC_PATH_TOKEN_LIMIT` constant in `embeddings.py`, the `FULL_QUALITY_TOKEN_LIMIT` constant in `health.py`, and the `effective_cap_tokens` helper in `embed_ops.py`. No behavioral changes — just the types, columns, and constants other tasks consume.

## Target Files

- modify: `src/ccrecall/schema.py` — add `cap_tokens INTEGER` to `SCHEMA_CORE` chunks table definition, add `CREATE INDEX IF NOT EXISTS idx_chunks_cap_tokens ON chunks(cap_tokens) WHERE cap_tokens IS NOT NULL`
- modify: `src/ccrecall/db_base.py` — bump `SCHEMA_VERSION` from 7 to 8, add `_migrate_to_v8` (ALTER TABLE + CREATE INDEX IF NOT EXISTS)
- modify: `src/ccrecall/config.py` — add `"sync_path_token_limit": 4096` to `DEFAULT_SETTINGS`
- modify: `src/ccrecall/embeddings.py` — add `SYNC_PATH_TOKEN_LIMIT = 4096` constant after `MODEL_TOKEN_LIMIT` (line 48)
- modify: `src/ccrecall/health.py` — add `FULL_QUALITY_TOKEN_LIMIT = 8192` constant near the other alert constants (around line 58)
- modify: `src/ccrecall/embed_ops.py` — add `effective_cap_tokens(cap_tokens: int | None) -> int` helper that returns `MODEL_TOKEN_LIMIT` for `None`
- modify: `tests/test_db.py` — add test for schema v8, `cap_tokens` column existence, partial index
- modify: `tests/test_config.py` — add test that `sync_path_token_limit` appears in `DEFAULT_SETTINGS` with value 4096
- modify: `tests/test_health.py` — add cross-check test: `health.FULL_QUALITY_TOKEN_LIMIT == embeddings.MODEL_TOKEN_LIMIT`
- read: `src/ccrecall/db_base.py` — existing migration pattern (`_migrate_to_v4` at line 214 for the `ALTER TABLE ADD COLUMN` + duplicate guard)

## Prompt

Add the foundational schema, config, and constants for the sync-current memory fix.

**Schema (src/ccrecall/schema.py):** In the `chunks` table definition (line 131-144), add `cap_tokens INTEGER` after the `was_capped` column (line 140). After the existing indexes (lines 145-146), add `CREATE INDEX IF NOT EXISTS idx_chunks_cap_tokens ON chunks(cap_tokens) WHERE cap_tokens IS NOT NULL;`.

**Migration (src/ccrecall/db_base.py):** Bump `SCHEMA_VERSION` from 7 to 8 (line 23). Add `_migrate_to_v8` following the `_migrate_to_v4` pattern (line 214): `ALTER TABLE chunks ADD COLUMN cap_tokens INTEGER` guarded by `"duplicate column name"` check, then `CREATE INDEX IF NOT EXISTS idx_chunks_cap_tokens ON chunks(cap_tokens) WHERE cap_tokens IS NOT NULL`. Wire it into `_apply_migrations` — it runs unconditionally (outside the version gate) like v3/v4, because the column must exist even when `user_version` is ahead.

**Config (src/ccrecall/config.py):** Add `"sync_path_token_limit": 4096` to the `DEFAULT_SETTINGS` dict (around line 42-49).

**Constants (src/ccrecall/embeddings.py):** Add `SYNC_PATH_TOKEN_LIMIT = 4096` right after `MODEL_TOKEN_LIMIT = 8192` (line 48).

**Constants (src/ccrecall/health.py):** Add `FULL_QUALITY_TOKEN_LIMIT = 8192` near the existing `ALERT_*` constants (line 56-58). This is a deliberate duplication — `health.py` must never import from `embeddings.py` (invariant 3).

**Helper (src/ccrecall/embed_ops.py):** Add `effective_cap_tokens(cap_tokens: int | None) -> int` at module level (near the top, after imports). Returns `MODEL_TOKEN_LIMIT` when `cap_tokens` is `None`, otherwise returns `cap_tokens`. Every site that compares `cap_tokens` calls this to avoid `TypeError` on `None < int`.

**Tests:** See Verify section for specific test requirements.

## Focus

- `db_base.py:_apply_migrations` (line 270) is where migrations are wired. Unconditional migrations (v3, v4) run before the version gate at lines 274-279. Follow the same pattern for v8.
- `health.py` is structurally guarded to import none of vec/fastembed/onnxruntime — a test asserts this via AST inspection (`tests/test_context_injection.py`). The new `FULL_QUALITY_TOKEN_LIMIT` constant must be a plain `int` literal, not imported from `embeddings.py`.
- `config.py:load_settings` (line 184) only merges keys that exist in `DEFAULT_SETTINGS`, so adding the key there is sufficient for it to be overridable from `~/.ccrecall/config.json`.

## Verify

- [ ] FR#5: The `chunks` table has a `cap_tokens INTEGER` column (check via `PRAGMA table_info(chunks)` in a fresh DB)
- [ ] AC#11: `effective_cap_tokens(None)` returns `MODEL_TOKEN_LIMIT`; `effective_cap_tokens(4096)` returns `4096`; no `TypeError` on `None` input
