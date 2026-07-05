# Brief: ccrecall Productionization Cleanup

**Date:** 2026-07-05
**Status:** explored

## Idea

Restructure ccrecall into a production-quality codebase without a ground-up rewrite. Keep working core logic (parsing, branch detection, embed pipeline, PID-guarded concurrency) but reorganize module boundaries, add systematic logging, improve error handling, and delete dead weight. The goal is a codebase that biases future Claude-authored changes toward clean patterns rather than perpetuating accumulated mess.

## Key Decisions Made

- **Embeddings stay a required dependency** — fastembed/numpy/sqlite-vec are already unconditional in `dependencies` (no optional-deps section exists). No packaging change needed. Runtime degradation behavior (keyword fallback when embeddings unhealthy) stays as-is.
- **Freedom to break everything** — no backwards-compatibility constraints. User base is effectively 1.
- **Cut the token analytics subsystem entirely** — 2,936 lines of source + 1,715 lines of dedicated tests. Separate concern, thin tests, no logging. Does not earn its place. Blast radius into shared files:
  - `cli/commands.py`: imports `token_dashboard` (line 19), defines `cmd_tokens` (line 283) — remove import + command
  - `tests/conftest.py`: imports `token_analytics`, `token_parser`, `token_schema`, `token_helpers` (lines 8-15); defines `token_db` and `populated_token_db` fixtures (lines 73-107) — remove imports + fixtures
  - `tests/token_helpers.py` (41 lines): token test data builders — delete entirely (counted in the 1,715 test total: 620+211+530+313+41=1,715)
  - `tests/test_db.py`: imports `token_schema.ensure_schema` (line 26); `TestNoTokenSnapshotsOnConversationDb` class (lines 731-749, 2 tests); `token_snapshots` simulation test (lines 785-818) — remove import + class + test
  - `tests/test_boundary_validation.py`: imports `token_parser.JnlFile, parse_session` (line 15); section header + helper `_jnl` (lines 75-81); `TestTokenValidation` class (lines 84-120, 3 tests) — remove import + helper + class
  - `tests/test_ingest_token_data.py`: dedicated token test file (620 lines) — delete entirely (already counted in 1,715)
- **Cut the onboarding system** — replace the interactive prompt flow with "create config with defaults if missing." 130 lines source + 356 lines tests removed. Blast radius into shared files:
  - `cli/commands.py`: imports `hooks.write_config` (line 28); defines `cmd_write_config` (lines 289-297) — remove import + command
  - `db.py`: defines `CURRENT_ONBOARDING_VERSION = 1` (line 64) — remove constant
  - `hooks/memory_context.py`: `onboarding_completed` gate (lines 593-603) silently blocks context injection — remove the gate section (lines 593-603: the comment, `load_config()`, the `if` check, and its body including `conn.close()` and `_emit_with_proactive`). Keep line 591 (`proactive_block = ...`) which is read by other gates downstream
  - `tests/test_db.py`: `TestCurrentOnboardingVersion` class (lines 253-258) — remove
  - `hooks/onboarding.py` also imports `legacy.find_legacy_db` (line 12, lines 70-71) — moot since both onboarding and legacy are being cut
- **Cut the legacy migration** (~/.claude-memory -> ~/.ccrecall) — 172 lines source + 230 lines tests. Already ran on all machines. Blast radius into shared files:
  - `hooks/memory_setup.py`: imports `PID_KEY as MIGRATE_PID_KEY` and `find_legacy_db` (lines 26-27); `MIGRATION_NOTICE` string (line 36); migration control flow in `main()` (lines 161-182, ~20 lines) — remove imports + string + control flow
  - `cli/commands.py`: imports `legacy as legacy_mod` (line 15); defines `cmd_migrate` (lines 92-100) — remove import + command
  - `tests/test_sync_hook.py`: monkeypatches `memory_setup.find_legacy_db` in 3 places (lines 784, 806, 825) — remove patches
  - `hooks/onboarding.py`: imports `find_legacy_db` (line 12) — moot since onboarding is also being cut
- **Middle path: restructure, not rewrite** — keep working logic, reorganize modules, add logging/errors systematically, delete dead weight. Lower risk than a rewrite, still achieves clean codebase.
- **SQLite `PRAGMA user_version`** for schema versioning — no external migration framework. Check version on connect, run DDL deltas to bring current. Native, zero-dependency, appropriate for the tool's scale. DDL delta + version bump wrapped in one `BEGIN IMMEDIATE ... COMMIT` transaction; exceptions are fatal-and-loud. Explicitly partition "one-time structural DDL" (version-gateable) from "recurring self-heal checks" like `_ensure_vec_schema`'s dimension-mismatch detection (must stay outside the gate, run every connect as today).
- **Log everything by default, tune later** — every DB write, search query, embedding op, spawn, and error gets logged at INFO or DEBUG. Users dial down via config. Currently some paths (search, tokens) have zero logging. Use per-process log file naming (e.g. `ccrecall-sync.log`, `ccrecall-import.log`) to avoid the multi-process `RotatingFileHandler` rotation race — Python's stdlib only synchronizes in-process.
- **Both install paths must work** — pip/uv package install AND Claude Code plugin install.

### Resolved from investigation

- **Split `db.py` but keep separate hook entry points.** The ~440ms vs ~1800ms framing was misleading: all 6 hooks already pay the heavy import cost because `db.py` unconditionally imports `embeddings.py` (numpy/fastembed) and `sqlite_vec` at module level. The fix: split `db.py` into a lightweight module (config, paths, PID files, logging — zero heavy deps) and a heavy module (connections, vec operations — imports sqlite_vec/embeddings). Hooks that don't need DB connections (like `memory_sync`) import only the lightweight module. Challenge disproved the unified dispatcher — a module-level dict dispatch eagerly imports all handlers, dragging the heavy stack back in. Separate entry points also preserve per-hook failure isolation (a broken import in one hook doesn't crash the `{"continue": true}` contract for all events). Keep the 6 console-script entry points in `pyproject.toml`.

- **Stop persisting inactive branches — YES, but with session-keyed row identity.** Challenge revealed a critical defect: `leaf_uuid` changes on every incremental sync (the Stop hook re-parses the growing JSONL, `latest["uuid"]` changes each time), causing a new `branches` row per sync. Today `enforce_single_active_branch` cleans this up. The fix: key the surviving branch row by `session_id` alone (UPDATE WHERE session_id=? instead of insert-by-leaf_uuid) — one row per session, no churn by construction. `fork_point_uuid` is write-only (remove). `find_all_branches()` returns only the active branch. Keep `is_active` column and all `is_active = 1` read filters as a permanent guard against the 3,295 existing inactive rows — do NOT remove those filters (challenge Finding 2). Run a one-time migration to delete `is_active = 0` rows (FK-safe order: `branch_messages` first, then `chunks`/`chunk_vec`, then `branches`).

- **Config keys — keep all 6 DEFAULT_SETTINGS, remove 2 onboarding keys.** All 6 keys (`auto_inject_context`, `max_context_sessions`, `exclude_projects`, `logging_enabled`, `log_level`, `alert_snooze_hours`) are actively used by non-onboarding code. `max_context_sessions`, `log_level`, and `alert_snooze_hours` are each read in exactly one place (candidates for hardcoding but low-cost to keep). `onboarding_completed` and `onboarding_version` are dead after the onboarding cut — remove them and remove the `onboarding_completed` gate in `memory_context.py:595` that silently blocks context injection.

- **Search decomposition — 5-module split.** Boundary types are simple (branch IDs, score tuples, result dicts — no custom classes cross boundaries):

  | Module | Contents | Est. lines |
  |--------|----------|------------|
  | `search_query.py` | `scope_filter_clause`, `_get_fts_branch_ids` | ~134 |
  | `search_vector.py` | `_execute_chunk_knn`, `_get_vec_chunk_ids`, `_hydrate_snippets` | ~170 |
  | `search_hydrate.py` | `_dedup_by_session`, `_hydrate_cards` | ~165 |
  | `search_conversations.py` | `search_sessions`, `search_messages`, `_compute_caveat` (orchestrators) | ~175 |
  | `search_cli.py` | `run`, `run_messages`, `print_status`, format wrappers (CLI entry points) | ~215 |

  Plus existing `formatting.py` (381 lines) and `fusion.py` (40 lines) unchanged.

## Open Questions

None remaining — all resolved during investigation and challenge.

### Resolved

- **Connection management** (promoted from open question): `get_db_connection()` will return a context manager; callers use `with get_connection(settings) as conn:`. Live evidence: `sync_current.py` leaks its connection on the exception path (conn opened, closed only on success, abandoned on error). Use this bug as the pin/characterization case proving the new pattern closes connections on error paths. Migrate every `get_db_connection()` call site in the same wave.

## Scope Boundaries

### In scope
- Delete token analytics subsystem (token_schema, token_parser, token_analytics, token_output, token_insights, token_dashboard + all tests + CLI commands)
- Delete onboarding system (hooks/onboarding.py, hooks/write_config.py + tests), replace with default-config-on-missing
- Delete legacy migration (legacy.py + tests)
- Split `db.py` into lightweight config/utils module and heavy DB/vec module (repoint `health.py` imports to lightweight module)
- Keep 6 separate hook entry points; hooks that don't need DB import only the lightweight module
- Simplify branch handling: session-keyed single row identity, return only active from `find_all_branches()`, keep `is_active` column + filters, one-time migration to delete inactive rows
- Restructure search_conversations.py into 5 concern-separated modules
- Add systematic logging to all code paths (search, hooks, CLI, DB operations) with per-process log files
- Drop `messages_fts` virtual table and its insert/update/delete triggers — write-amplifying index with zero application-code readers (search uses `branches_fts`)
- Implement `PRAGMA user_version` schema versioning
- Standardize DB connection management (context managers, consistent closing)
- Remove `onboarding_completed`/`onboarding_version` config keys and the context-injection gate
- Review and clean up error handling patterns for consistency
- Add branch-count invariant check (one active branch per session) surfaced through `ccrecall stats` and logged at WARNING when violated — lands in the same commit that changes branch persistence
- Clean up dead branch data: delete `is_active = 0` branch rows cascading to `branch_messages` (no `ON DELETE CASCADE` — delete in FK-safe order: `branch_messages` first, then `chunks`/`chunk_vec` via existing cascade trigger, then `branches` rows). Retargeted from the brief's original "orphan messages" framing — live DB has 0 orphan messages but 3,295 dead branch rows with 191k `branch_messages` links

### Explicitly out of scope
- New features (no new search capabilities, no new CLI commands)
- Changes to the JSONL parsing contract (parsing.py tracks an external format — keep the parser, simplify what it returns)
- Changes to the embedding model or vector dimensions
- Changes to the plugin skill files (skills/ directory)
- Changes to the hook event contract (what goes to stdout)

### Deferred
- Further search quality improvements (Track A gaps, issue #35)
- The nudge system and alerting rethink (from next-steps plan)
- Issues #34, #35

## Risks and Concerns

- **Rewind regression**: Simplifying `find_all_branches()` to return only the active branch changes what messages get linked. The `diff_branch_messages` analysis shows this is safe (abandoned message links get removed on re-sync), but pin behavior with characterization tests on the rewind fixtures before changing the parser.
- **Orphan message accumulation**: Messages from abandoned branches remain in the `messages` table unreachable. Benign (no query surfaces them) but wastes storage. Add a periodic cleanup query if it matters.
- **`db.py` split boundary**: The split must be clean — the lightweight module must not transitively import anything heavy. A test should assert this (similar to the existing `health.py` import guard). Note: `health.py` currently imports from `ccrecall.db` (`health.py:23`), which transitively pulls in the full vec/fastembed stack — the existing AST test only checks `health.py`'s own source, not the transitive graph. The split must repoint `health.py`'s imports to the lightweight module, and add a transitive-import test (subprocess import of `ccrecall.health`, assert `fastembed`/`onnxruntime`/`sqlite_vec` absent from `sys.modules`).
- **Logging volume**: "Log everything" with 1MB rotating file (2 backups = 3MB max) may fill quickly. May need to tune sooner than expected or increase rotation size.
- **Test coupling**: 862 tests are coupled to current module structure. Restructuring modules means updating test imports and organization. Budget for this — it's mechanical but time-consuming.
- **Two install paths**: Changes to entry points and hook wiring must be validated against both pip and plugin installs.

## Codebase Context

- **10,805 lines source / 14,422 lines tests** across 39 source files and 32 test files (862 tests) before cuts
- **Estimated removals**: 3,238 lines source + 2,301 lines tests (tokens 2,936+1,715; onboarding 130+356; legacy 172+230)
- **Post-cut baseline**: ~7,567 lines source, ~12,121 lines tests
- **Densest file**: search_conversations.py (879 lines) — primary restructuring target
- **Error handling is deliberate** — hooks never crash, backfill distinguishes content from infra errors, no bare `except: pass`. The problem is inconsistency across modules, not absence.
- **Logging has gaps** — search never calls `setup_logging()`, so its logger calls go to the root logger. The token subsystem (being cut) has no logging at all.
- **DB connections are not pooled** and closing is inconsistent — some `contextlib.closing()`, some manual `.close()`, some unclear ownership.
- **No TODO/FIXME/HACK markers** in the codebase — deferred work is tracked via GitHub issue references.
- **Pre-commit hooks**: `prek` enforces no-future-annotations, no-lazy-imports, and custom checks in `tools/`. Conventional-commit enforcement is a separate GitHub Actions check (`.github/workflows/pr-title.yml`), not a prek hook.

## Investigation Findings (detail)

### Hook import timing

The CLAUDE.md claim that separate entry points give ~440ms vs ~1800ms is misleading. All 6 hooks already import `ccrecall.db`, which unconditionally imports `ccrecall.embeddings` (numpy + fastembed + onnxruntime) and `sqlite_vec` at module level. The separate-entry-point architecture does NOT avoid the heavy cost — it was either stale or never measured correctly.

The fix is structural: split `db.py` so that config/path/PID/logging utilities live in a module with zero heavy deps. Hooks that don't need DB connections (like `memory_sync`, which just writes a tempfile and spawns a subprocess) would import only the lightweight module and genuinely run in ~100-200ms. The `no-lazy-imports` rule is not an obstacle — this is module-level restructuring, not function-level lazy loading.

Cyclopts cannot lazily register commands (decorators fire at import time), so the CLI app construction will always be heavy. But hooks don't need cyclopts — a simple dict dispatch (`{"SessionStart": setup_main, "Stop": sync_main, ...}`) is sufficient and avoids the CLI import graph entirely.

### Branch simplification

`fork_point_uuid` is populated on write but never read back by any query — purely write-only. No code queries inactive branches except one COUNT for stats display in `import_conversations.py` (trivially removed). `diff_branch_messages` correctly handles the rewind case: on re-sync after a rewind, it diffs desired vs existing message links and removes the abandoned ones. Orphan messages stay in `messages` but are unreachable since all access goes through `branch_messages` joins.

Source file changes (9 files, hit counts): `parsing.py` (8 — delete ~55 lines of abandoned-fork detection), `session_ops.py` (29 — single-branch loop, delete `enforce_single_active_branch`), `import_conversations.py` (3 — remove abandoned count), `schema.py` (3 — `fork_point_uuid` column, `is_active` default), `db.py` (3 — `EMBEDDABLE_BRANCH_FILTER`/`CHUNK_EMBEDDABLE_BRANCH_FILTER` constants, remove `is_active = 1` clause), `search_conversations.py` (3 — remove `is_active = 1` WHERE clauses), `memory_context.py` (2 — remove `is_active = 1` JOIN clauses), `recent_chats.py` (1 — remove `is_active = 1` JOIN), `backfill_embeddings.py` (1 — `is_active=True` parameter).

Test file changes (11 files): `test_parsing.py` — structural rewrite of ~150 of 384 lines: the entire `TestFindAllBranchesProperties` Hypothesis class (lines 108-153), synthetic multi-branch generators `_build_uuid_tree`/`uuid_trees` (lines 53-105), and per-fixture branch-count expectations for rewind fixtures (lines 42-43 feeding `TestFixtureBranches.test_branch_count`) all assert multi-branch detection behavior that ceases to exist. `test_session_ops.py` (26 hits — inactive branch handling, `enforce_single_active_branch` tests), `test_search.py` (18 — `is_active` fixtures), `test_integration.py` (11), `test_backfill_embeddings.py` (10), `test_sync_hook.py` (7), `test_import_pipeline.py` (7), `test_context_injection.py` (6), `test_summarizer.py` (3), `test_db.py` (2), `test_recent_chats.py` (1). `test_legacy_migration.py` (1) is moot since legacy is being cut.

### Config audit

All 6 DEFAULT_SETTINGS keys are actively used. The `memory_context.py:595` gate (`if not config.get("onboarding_completed")`) silently blocks context injection for users who never completed onboarding — this must be removed when onboarding is cut, or context injection breaks for fresh installs.
