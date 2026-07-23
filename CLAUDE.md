# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

`ccrecall` is conversation history and semantic search for Claude Code, shipped two ways from one repo:

- a **Python package** (`ccrecall` on PyPI) providing the `ccrecall` CLI and the hook console scripts, and
- a **Claude Code plugin** (`.claude-plugin/plugin.json`) providing the `/ccr-*` skills and the hook wiring (`hooks/hooks.json`).

It is an independent community project — not affiliated with Anthropic.

## Names (and one deliberate mismatch)

| Surface | Name |
|---|---|
| PyPI package / CLI binary / plugin | `ccrecall` |
| GitHub repo | `claude-code-recall` |
| Skills | `/ccr-recall`, `/ccr-resume` |
| Hook entry points | `ccrecall-setup`, `ccrecall-sync`, `ccrecall-context`, `ccrecall-clear-handoff`, `ccrecall-warm-model` |
| Runtime data dir | `~/.ccrecall/` |

Under a plugin install, skills are namespaced by the plugin name — invoked as `/ccrecall:ccr-recall` etc. The bare `/ccr-recall` form is what the skill folders are named and what a non-plugin (vendored) install exposes.

The **GitHub repo** is `claude-code-recall` while everything else (package, CLI, plugin, data dir) is `ccrecall` — that one mismatch is deliberate: the repo name is more discoverable/descriptive, and renaming a published repo breaks clone URLs and stars. Do not "fix" it.

## Architecture

The hard dependency is on undocumented Claude Code internals (the `~/.claude/projects/<slug>/*.jsonl` transcript layout and the hook-event protocol). Anthropic changes these at patch cadence, so the design contains the coupling rather than spreading it:

- **One parse boundary.** `models.py` (Pydantic) + `parsing.py` own JSONL decoding; downstream code consumes typed objects, not raw transcript shapes. Keep new transcript knowledge here.
- **`content.py:extract_text_content`** returns a 5-tuple: `(text, has_tool_use, has_thinking, tool_summary, tool_content)`. `tool_content` is generic field-join extraction — for each `tool_use` block it walks the `input` dict's string-valued fields (recursively, so nested lists/dicts like AskUserQuestion's `questions` are covered) and joins them into a `[ToolName: ...]` marker, capped per-field and per-block. No per-tool dispatch table; new tool types are covered automatically. Never raises on malformed input.
- **`config.py` / `db.py` split.** `config.py` owns paths, config/settings loading, PID files, and logging setup — zero heavy dependencies (no fastembed/onnxruntime/sqlite_vec). It's the module hooks that don't touch the DB (health checks, the clear-handoff writer, the warm-model process) import instead of pulling the full `db.py` stack onto the hook hot path. `db.py` owns connections, schema application, and vec operations. `get_connection()` is a `@contextlib.contextmanager` (`with get_connection(settings) as conn:`) that commits on success, rolls back and closes on exception, and always closes — no connection is left open on an error path.
- **`schema.py`** holds `SCHEMA_CORE` as a single baseline (embedding DDL folded in), applied idempotently via `CREATE TABLE IF NOT EXISTS` — there is no migration-DML ladder for the base schema. Schema *deltas* on top of that baseline are tracked via `PRAGMA user_version`, applied in a `BEGIN IMMEDIATE … COMMIT` transaction after `SCHEMA_CORE` executes (so a fresh install always has tables before any delta DML runs); a failed delta rolls back and retries on the next connection. The conversations DB is a public contract now; don't evolve it in a way that silently loses a user's synced history. `messages_fts` (a dead FTS index nothing read) has been dropped — `branches_fts`, the live keyword search index, is untouched. The embedding layout is a **two-table design**: `chunks` (one row per exchange — the user turn plus its following assistant turns — carrying the exchange locator, bounded display text, and embedding bookkeeping) and `chunk_vec` (a sqlite-vec virtual table keyed by chunk rowid, 512-dim float vectors). `branches.embedding_version` is retained as a per-branch *watermark* meaning "every current exchange of this branch has a current-version chunk vector"; per-chunk staleness (`chunks.embedding_version`) is the source of truth for query-time filtering. The hot-path framing is unaffected — embedding runs in a detached `sync-current` process, never on the hook thread. `messages` carries a `tool_content TEXT` column (populated by `extract_text_content`, alongside `content` for prose) — the existing `tool_summary` and `has_tool_use` columns remain unpopulated dead columns (`branches.tool_counts` already serves that purpose).
- **Session-keyed branch identity.** `branches` rows are keyed on `session_id` (`branch_ops.upsert_branch`) — one active row per session, updated in place on every sync, instead of a new row per leaf UUID. `is_active` is retained permanently as a guard against pre-existing inactive rows (historical forks); every read path still filters on `is_active = 1`. Don't drop those filters.
- **`session_ops.py` decomposition.** `session_ops.py` is a slim orchestrator (`sync_session`) that delegates to four modules by concern: `import_log_ops.py` (the `import_log` skip check and upsert), `message_ops.py` (session/message row upsert and insert), `branch_ops.py` (branch row CRUD, `branch_messages` diffing, and `sync_branch` — the per-branch coordinator that ties metadata, links, summary, and embedding together), and `embed_ops.py` (`write_branch_summary` and `embed_branch_chunks`, which owns the embedding watermark protocol — see invariant 3). Boundary types between them are plain Python types (ids, dicts, tuples); no new dataclasses cross these module boundaries. `summarizer.py:build_exchange_pairs` (consumed by `embed_branch_chunks`) now appends each assistant message's `tool_content` to the exchange text alongside its prose — including tool-only turns (empty prose, non-empty `tool_content`), which is what makes them embeddable instead of silently dropped. Consecutive identical tool-marker lines are collapsed into one summary line before embedding, so repetitive markers (e.g. 15 `[Read: ...]` calls) don't dilute the prose signal. The vestigial `[Tool: \w+]` regex that used to run over `content` here has been deleted — it matched nothing real.
- **`cli/`** — cyclopts app. Root `App` + `backfill` sub-`App`; commands live in `cli/commands.py` and self-register on import. A single global `--json` flag is the only output-format surface (carried by a frozen `CLIContext`); commands do not define their own `--json`.
- **Search decomposition.** `search_conversations.py` holds only the two orchestrators (`search_sessions`, `search_messages`) plus `compute_caveat`. `search_query.py` (FTS branch-id lookup), `search_vector.py` (chunk-KNN + snippet hydration), and `search_hydrate.py` (session dedup + card hydration) are separate modules; `search_cli.py` is the CLI-facing formatting layer. Boundary types are plain (branch IDs, score tuples, result dicts) — nothing custom crosses a module boundary.
- **`hooks/`** — the SessionStart/Stop/SessionEnd hook entry points plus the helpers they spawn (`import_conversations`, `sync_current`, `backfill_embeddings`, `backfill_summaries`, `warm_model`, `backfill_tool_content`).
- **`memory_context.py` decomposition.** `memory_context.py` stays the SessionStart hook entry point (`main()`, stdout envelope, DB connection lifecycle) and delegates to three modules: `context_alerts.py` (`proactive_alert_block` — the combined health-alert block, evaluated before every early-return gate), `session_selection.py` (`select_sessions` — the startup/clear session-selection algorithm and its DB queries), and `context_rendering.py` (`build_context`, `build_origin_block`, `pending_question_block` — markdown block rendering and topic extraction).
- **`backfill_embeddings.py` decomposition.** `backfill_embeddings.py` keeps only the `run()` orchestrator. `backfill_query.py` owns query construction (`build_selection`, `days_modifier`), the shared constants both the orchestrator and the status reporter agree on (`BATCH_SIZE`, `PID_KEY`, exit codes), and PID cleanup. `backfill_status.py` owns read-only progress counting (`count_status`), duration formatting, and the `--status` report (`run_status`).
- **Per-process logging.** Each process type writes to its own rotating log file (`~/.ccrecall/ccrecall-<process>.log`, 1MB/2 backups) via `config.py`'s `setup_logging(settings, process_name=...)` — avoids multiple processes racing on one file's rotation. Process names: `setup`, `sync`, `context`, `clear-handoff`, `import`, `backfill-embed`, `backfill-summary`, `backfill-tool-content`, `warm-model`, `cli` (the last one is what a direct, non-spawned `ccrecall <command>` invocation gets unless the command sets up its own process-named logger, as the spawned background commands do).
- **`health.py`** — the parse/format boundary for the **surfacing model** (the SessionStart proactive alerts + the reactive recall caveat). Owns the two small JSON sidecars under `~/.ccrecall/` (`embedding-status.json`, `alert-snooze.json`), the active writability probe, the told-once-snooze ledger, the reason-code → prose mapping, and the alert-block builder. Deliberately imports **no** vec/fastembed/onnxruntime — it only ever *reads* the embedding-status sidecar (see invariant 3). Embedding-failure is detected by the detached embedding process writing that sidecar, never by probing capability on the hook path.

### Four invariants to preserve

1. **Hook stdout.** The hooks print `{"continue": true}` / `{}` to stdout for the harness. Never emit anything else to their stdout.
2. **Hook hot path.** Hooks are separate console scripts, *not* `ccrecall hook …` subcommands, because routing them through the cyclopts app eager-imports the whole command surface (fastembed/numpy/onnxruntime, ~1800ms) vs ~440ms for a direct hook import. The no-lazy-imports rule (see Conventions) means you cannot dodge that, so keep hooks as direct entry points.
3. **Embedding health is read, never probed, on the hook path.** The SessionStart hook learns that embeddings are failing by *reading* `health.py`'s `embedding-status.json` sidecar — it must never load the vector extension or the embedding model to check capability inline, or it would pull the ~1800ms stack onto the ~440ms hot path. The authoritative detector is the detached embedding process (`sync_current` / `backfill_embeddings`), which records/clears that sidecar. `health.py` is structurally guarded to import none of vec/fastembed/onnxruntime (a test asserts this via AST inspection). `embed_ops.py` owns the write-path half of this invariant: `embed_branch_chunks` implements the clear-first/set-last watermark protocol (watermark cleared before the embed loop, set only after every exchange has a current-version chunk), and that transaction boundary must not change across module splits.
4. **One active branch per session.** Post-migration, `branches` is `UNIQUE(session_id)` — a DB-level constraint that makes a second row for the same `session_id` impossible, enforced by the v1 migration rebuild (`db.py`'s `_migrate_to_v1`), which runs unconditionally on every `get_connection()` call while `user_version < SCHEMA_VERSION`, including on a brand-new install. `ccrecall stats` still runs a standing runtime check (`SELECT session_id, COUNT(*) AS cnt FROM branches WHERE is_active = 1 GROUP BY session_id HAVING cnt > 1`) and logs a WARNING if it's ever violated — this is defense-in-depth for the upsert logic, not evidence that the schema allows duplicates. `SCHEMA_VERSION` is now `4`: `_migrate_to_v2` purges orphan messages and drops the dead `fork_point_uuid` column via table rebuild (SQLite has no `DROP COLUMN`-in-place path here) — `SCHEMA_CORE` and `_migrate_to_v1` both still carry `fork_point_uuid` deliberately, so a fresh install stays compatible with v1 running first; only the v2 rebuild removes it. `_migrate_to_v3` adds `file_size` and `file_mtime` columns to `import_log` for stat-based fast skip — runs outside the version gate (with duplicate-column error handling for concurrency) because the columns must exist even when `user_version` is ahead of this code's `SCHEMA_VERSION`. `_migrate_to_v4` adds `messages.tool_content TEXT` — additive and unconditional like `_migrate_to_v3`, for the same reason (the column must exist regardless of which `SCHEMA_VERSION` the running code targets).

## Conventions

Enforced by `prek` (pre-commit) hooks + custom checks in `tools/`:

- **No `from __future__ import annotations`** and **no lazy imports** (imports inside functions) — both have dedicated checks. Use `X | None`, not `Optional[X]`.
- **`whenever`** for all date/time, not stdlib `datetime` (convert only at library boundaries).
- **setuptools** build backend (never hatchling). License is declared as an SPDX expression (`license = "MIT"` + `license-files`).
- **Conventional Commits.** Releases are automated by **release-please**; the version lives in `pyproject.toml`, is mirrored into `.claude-plugin/plugin.json` (release-please `extra-files`), and `uv.lock` is re-locked on the release PR by a `sync-lockfile` CI job so its self-version never drifts. `feat`/`fix`/`perf`/`refactor`/`docs` land in the changelog.

## Commands

```bash
uv sync                       # install package + dev dependencies
uv run pytest                 # full test suite (the CI command)
uv run pytest -q --cov        # with coverage
uvx prek run --all-files      # lint, format, type-check, custom checks
uv build                      # build sdist + wheel
```

## Gotchas

- Skills under `skills/` are bundled into the plugin; their `references/` subdirs are loaded on demand by the skill, not eagerly.
