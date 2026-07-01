# Research Brief: First-Pass Architecture Cleanup

## Current State

`ccrecall` ships as one Python package plus a Claude Code plugin. The package exposes the unified `ccrecall` CLI and direct hook console scripts. The direct hook scripts are intentional because hook stdout must remain JSON-only and hook startup must avoid importing the whole CLI surface.

Semantic/vector dependencies are mandatory package dependencies today: `sqlite-vec`, `fastembed`, and `numpy` are listed in base dependencies. Runtime code degrades when semantic support fails, but package installation and broad imports still assume those libraries exist.

Native semantic imports currently leak into broad surfaces:

- `db.py` imports `sqlite_vec` and embedding constants at module import time.
- `search_conversations.py` imports `sqlite_vec` and `embeddings.py` at module import time.
- `cli/commands.py` imports search/backfill embedding modules while registering all commands.
- `session_ops.py`, `sync_current.py`, `warm_model.py`, and `backfill_embeddings.py` import embedding helpers directly.

`db.py` owns too many unrelated responsibilities: runtime paths, config/settings, atomic JSON writes, PID sentinels, SQLite connection setup, schema application, sqlite-vec loading, vector writes, branch-message fetch helpers, embedding coverage, and logging.

The conversation schema already has chunk-level embeddings, but there is no canonical `exchanges` table. `chunks` currently carries exchange locator/display text and embedding bookkeeping. `summarizer.build_exchange_pairs()` recomputes exchanges from branch messages; `session_ops.embed_branch_chunks()` then diffs those exchanges against `chunks` and writes `chunk_vec`.

Token analytics are active surface area through `ccrecall tokens`, `skills/ccr-tokens/`, `token_*.py`, token fixtures/tests, and token tables in the shared SQLite DB. The desired product direction is to remove that active surface while preserving existing token tables as orphaned data.

Background work is currently process/PID-file based. `memory_setup._spawn_background()` guards import, summary backfill, migration, and warm-model with `.pid-*` files. `sync_current` and embedding backfill have their own PID conventions. There is no DB-backed job table.

## Recommended Approach

Treat the change as an additive cleanup pass, not a behavior redesign.

Recommended sequence:

1. Extract runtime boundaries from `db.py`.
2. Move semantic support behind an optional dependency boundary.
3. Add canonical `exchanges` and make chunks/vector rows derive from exchanges.
4. Add a small `jobs` table and migrate import as the first background job.
5. Remove active token analytics surfaces while preserving existing token tables.

Runtime extraction should produce narrow modules for paths, settings, database connection/schema setup, runtime files, logging, and semantic vector storage. A temporary `db.py` compatibility facade is acceptable only if it does not keep importing semantic/native modules at top level.

Semantic optionality should move `sqlite-vec`, `fastembed`, and likely `numpy` out of base dependencies into a `semantic` extra. The no-lazy-import rule means the code should either make semantic modules import-safe without the extra or ensure base command surfaces never import semantic-only modules. Removing numpy from normalization is likely simpler than trying to optionalize it.

Canonical exchanges should be added additively. Existing `chunks` rows can be promoted into `exchanges`, then linked with a new nullable `chunks.exchange_id` column. Old chunk locator/display columns can remain redundant for this pass to avoid a risky table rebuild.

The first DB-backed job should be import, not sync or embedding backfill. Import already has durable dedupe via `import_log`, avoids semantic dependencies, and is spawned by SessionStart today.

## Key Risks

- Optional semantic dependencies will fail if `db.py` remains a top-level semantic import hub.
- The no-lazy-import rule makes optional dependency boundaries a structural design problem, not a local import trick.
- Existing `chunks` must be promoted into `exchanges`; otherwise old semantic search snippets can disappear until sessions are re-synced.
- Token analytics uses a generic `schema_version` table name, so this cleanup should not introduce another generic version table without considering orphaned token state.
- `ccrecall search --status` needs degraded/base-install behavior that does not load the model or vector extension.
- SessionStart setup must keep JSON-only stdout while enqueueing/spawning import jobs.

## Test Impacts

Affected areas include `tests/test_db.py`, `tests/conftest.py`, `tests/test_search.py`, `tests/test_embeddings.py`, `tests/test_backfill_embeddings.py`, `tests/test_import_pipeline.py`, `tests/test_sync_hook.py`, `tests/test_integration.py`, `tests/test_health.py`, token tests, CLI tests, and legacy migration tests.

New coverage should include base install simulation without semantic packages, `search-messages` unavailable caveats, token table preservation, exchange creation without semantic support, existing chunk promotion, vector cascade behavior after `exchange_id`, import job dedupe/claiming, and hook stdout under job failures.

## Documentation Impacts

Update README installation/degradation/database sections, CLAUDE.md architecture and invariants, ccr-recall tool reference caveats, plugin skill list, and release notes. Remove `/ccr-tokens` from active docs.
