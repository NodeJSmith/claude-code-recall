# Design: First-Pass Architecture Cleanup

**Date:** 2026-07-01
**Status:** archived
**Scope-mode:** hold
**Research:** design/specs/001-architecture-cleanup/research.md

## Problem

`ccrecall` has accumulated several product responsibilities and runtime concerns in the same import and storage boundaries. Token analytics are active surface area even though they are unused, semantic search dependencies are mandatory package dependencies even though the product already degrades to keyword search, and `db.py` mixes paths, settings, logging, SQLite setup, PID files, vector storage, and coverage reporting.

The result is a codebase that is harder to change than the product requires. Hook paths need careful comments to avoid native/vector imports, search fallbacks are runtime behavior rather than packaging behavior, and exchange-level retrieval stores canonical-looking exchange data in embedding-specific `chunks` rows.

## Goals

- Remove active token analytics from the product surface.
- Make semantic/vector search an optional install extra while preserving base keyword search.
- Preserve the existing recall/resume/search user workflow.
- Split core runtime concerns out of `db.py` into narrower modules.
- Introduce canonical exchange rows so embeddings derive from conversation structure rather than owning it.
- Add a small DB-backed jobs table and migrate one background process to prove the pattern.
- Preserve synced conversation history and leave existing token tables untouched.

## User Scenarios

### Jessica: Maintainer/User

- **Goal:** keep using recall normally while the internals become cleaner.
- **Context:** local development and personal Claude Code usage.

#### Base Install Without Semantic Extra

1. **Installs and uses ccrecall normally.**
   - Sees: the same `ccrecall recent`, `ccrecall search`, `ccrecall tail`, and hook behavior.
   - Decides: nothing new; semantic support is not required for base usage.
   - Then: `ccrecall search -q ...` returns keyword/FTS results only.

2. **Runs exchange-level search without semantic support.**
   - Sees: an explicit caveat that `search-messages` requires semantic support and should use `ccrecall search -q ...` for keyword session search.
   - Decides: whether to install the semantic extra or use session-level keyword search.
   - Then: the command exits successfully with an empty unranked result, not an ambiguous “no matches” result.

#### Install With Semantic Extra

1. **Installs semantic support.**
   - Sees: current fused search behavior.
   - Decides: whether to backfill historical embeddings as today.
   - Then: `ccrecall search` fuses keyword and vector results, and `search-messages` returns semantic exchange hits.

#### Maintains The Codebase

1. **Works on runtime/storage code.**
   - Sees: paths, settings, connection setup, vector storage, and jobs in distinct modules.
   - Decides: where a change belongs without importing unrelated native dependencies.
   - Then: hook-path invariants are easier to preserve because dependency boundaries are structural.

## Functional Requirements

- **FR#1** Base installation without semantic extras must support setup hooks, sync hooks, recent sessions, tail, context injection, and `ccrecall search` keyword/FTS results.
- **FR#2** Base installation without semantic extras must not require `sqlite-vec`, `fastembed`, or `numpy` to import the CLI, hook adapters, database connection path, or keyword search path.
- **FR#3** Semantic installation must preserve current fused `ccrecall search` behavior when vector support and embeddings are available.
- **FR#4** `ccrecall search-messages` without semantic support must exit successfully with an explicit caveat directing callers to `ccrecall search -q ...` for keyword session search.
- **FR#5** The `ccrecall tokens` command and `/ccr-tokens` skill must be removed from active product surfaces.
- **FR#6** Existing token analytics tables in user databases must be left untouched and ignored.
- **FR#7** Sync/import must create canonical exchange rows independently of semantic support.
- **FR#8** Chunk embeddings and `chunk_vec` rows must derive from canonical exchange rows.
- **FR#9** Existing synced conversation data in `sessions`, `branches`, `messages`, `branch_messages`, and `import_log` must not be deleted or rewritten destructively by this change.
- **FR#10** SessionStart/Stop hook stdout must remain valid hook JSON and must not include diagnostic text.
- **FR#11** At least one background process currently guarded by PID files must be migrated to a DB-backed job record with dedupe and observable terminal status.
- **FR#12** The first jobs migration must not introduce a daemon or require a long-running service.

## Edge Cases

- Existing DBs may contain token analytics tables from older versions; those tables remain orphaned.
- Existing DBs may contain `chunks` rows with exchange locator/display data but no canonical `exchanges` rows.
- Existing DBs may have stale or missing `chunk_vec` rows; derived vector self-healing must continue.
- Rewinds or resyncs may remove an exchange from a branch; stale `exchanges` and derived chunks must be pruned.
- Users may install the base package only and never install semantic dependencies.
- Users may install semantic dependencies after running the base package for some time.
- SessionStart may run before setup/import jobs complete.
- `ccrecall search --status` must avoid native/model probing in a base install while still reporting useful degraded status.

## Acceptance Criteria

- **AC#1** In an environment where semantic dependencies are unavailable, importing `ccrecall.cli`, running hook entry points, opening the DB, and running keyword `ccrecall search` do not fail because of missing `sqlite-vec`, `fastembed`, or `numpy`. (FR#1, FR#2)
- **AC#2** With semantic dependencies installed and available, existing fused search tests still pass. (FR#3)
- **AC#3** Without semantic support, `ccrecall search-messages --query X` returns a valid empty unranked result and includes a caveat that suggests `ccrecall search -q X`. (FR#4)
- **AC#4** `ccrecall tokens` is no longer registered, and `skills/ccr-tokens/` is no longer bundled as an active skill. (FR#5)
- **AC#5** Opening/upgrading a DB containing token analytics tables preserves those tables and rows. (FR#6)
- **AC#6** Sync/import creates `exchanges` rows for active branches even when semantic support is absent. (FR#7)
- **AC#7** Embedding backfill/search hydrates exchange snippets through `exchanges` joined to `chunks`, not through duplicated display columns as the source of truth. (FR#8)
- **AC#8** Existing `chunks` rows are promoted into `exchanges` and linked through `chunks.exchange_id` without reducing row counts in core conversation tables. (FR#8, FR#9)
- **AC#9** SessionStart and Stop hooks still print only their JSON hook envelopes under success and failure conditions. (FR#10)
- **AC#10** Repeated SessionStart setup attempts enqueue at most one import job for the same dedupe key. (FR#11, FR#12)
- **AC#11** A one-shot worker can claim an import job, mark it running, and mark it succeeded or failed with `last_error` without breaking hook stdout. (FR#11, FR#12)
- **AC#12** When an exchange disappears during resync, its canonical `exchanges` row and derived chunk/vector rows are removed. (FR#7, FR#8)

## Key Constraints

- Do not add lazy imports to solve optional semantic dependencies; the repo explicitly forbids imports inside functions.
- Do not make `sqlite-vec`, `fastembed`, or `numpy` mandatory for the base install.
- Do not drop token analytics tables from existing user databases.
- Do not redesign user-facing recall/resume/search workflows in this pass.
- Do not add exchange-level FTS fallback for `search-messages` in this pass.
- Do not convert `sync-current` or embedding backfill to DB jobs first; use import as the first jobs migration.

## Dependencies and Assumptions

- PyPI/package metadata changes are required for optional semantic dependencies.
- Claude Code plugin skill docs must remove `/ccr-tokens` and keep recall/resume behavior stable.
- Local SQLite under `~/.ccrecall/conversations.db` remains the system of record.
- Existing archived design docs for chunk embeddings remain historical references; this design supersedes their claim that `chunks` are the exchange carrier.

## Architecture

### Runtime Boundary Extraction

Split `src/ccrecall/db.py` by responsibility. Prefer updating internal imports to the new modules over keeping `db.py` as a permanent facade.

- `paths.py`: stable runtime path constants and path helpers: `RUNTIME_DIR`, `DEFAULT_DB_PATH`, `DEFAULT_PROJECTS_DIR`, `DEFAULT_LOG_PATH`, `CONFIG_PATH`, `CLEAR_HANDOFF_FILENAME`, `ensure_parent_dir`.
- `settings.py`: `DEFAULT_SETTINGS`, onboarding version, config load/merge, `get_db_path`, `resolve_db_settings`.
- `database.py`: SQLite connection setup, pragmas, schema application, FTS setup.
- `runtime_files.py`: atomic JSON writes and temporary/PID sentinel helpers that remain during transition.
- `logging_config.py`: rotating log setup and hook exception logging.
- `semantic_store.py`: sqlite-vec loading, vec schema creation, vector serialization/upserts, semantic coverage helpers.

`health.py` and SessionStart context code must remain semantic-native-free. Tests should keep enforcing that `health.py` imports none of vec/fastembed/onnxruntime.

### Optional Semantic Boundary

Move `sqlite-vec`, `fastembed`, and `numpy` from base dependencies into a semantic extra:

```toml
[project.optional-dependencies]
semantic = [
    "sqlite-vec==0.1.9",
    "fastembed==0.8.0",
]
```

Avoid needing `numpy` by replacing `embeddings.normalize()` with stdlib/list math, unless another semantic path truly requires numpy. `embeddings.py` may remain import-safe by catching import errors for `fastembed`, but no base path should require native packages to exist. `semantic_store.py` should use a top-level guarded `sqlite_vec` sentinel and return unavailable status when missing.

`search_conversations.py` should not import `sqlite_vec` directly. Vector serialization and KNN query helpers should live behind the semantic boundary. Base `ccrecall search` should open the DB without vec and run FTS5/FTS4/LIKE.

`memory_setup.py` should not spawn `ccrecall-warm-model` when semantic support is absent. `warm_model.py` and `backfill_embeddings.py` remain semantic-aware commands and should fail/caveat clearly when the extra is absent.

### Canonical Exchanges

Add an `exchanges` table to the conversation schema:

```sql
CREATE TABLE IF NOT EXISTS exchanges (
  id INTEGER PRIMARY KEY,
  branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
  exchange_index INTEGER NOT NULL,
  first_message_uuid TEXT,
  timestamp TEXT,
  user_text TEXT,
  assistant_text TEXT,
  exchange_hash TEXT NOT NULL,
  UNIQUE(branch_id, exchange_index)
);
```

`exchange_hash` is a semantic-free hash over the canonical exchange display payload, not over model-tokenized or embedding-capped text. Use a deterministic payload such as `user_text + "\n\n" + assistant_text` after the same non-semantic message extraction used for display. Do not call `cap_for_embedding()` to compute this hash; base installs without semantic support must still create exchanges.

`summarizer.build_exchange_pairs()` remains the exchange derivation helper. Sync/import should upsert exchanges from branch messages before embedding. Exchanges are conversation structure, so this happens even without semantic support. Resync must also prune exchanges whose `(branch_id, exchange_index)` no longer exists in the branch; the prune must cascade or explicitly delete derived chunks and vectors so stale exchanges never become the retrieval source of truth.

Make `chunks` embedding-specific by adding `exchange_id`:

```sql
ALTER TABLE chunks ADD COLUMN exchange_id INTEGER REFERENCES exchanges(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS idx_chunks_exchange ON chunks(exchange_id);
```

For this pass, keep old locator/display columns in `chunks` as redundant legacy columns to avoid a risky SQLite table rebuild. Code should treat `exchanges` as canonical once `chunks.exchange_id` is populated. A later cleanup can rebuild `chunks` and drop redundant columns.

`chunks.content_hash` keeps its current meaning: hash of the exact text used for embedding after embedding-specific capping. It is not the canonical exchange hash. This distinction is required because base installs can create exchanges without semantic/model tokenizer support, while semantic installs still need to know whether the embedded text changed.

Cascade behavior must work in base installs too. Add `ON DELETE CASCADE` where new FKs permit it and keep explicit cleanup/triggers where SQLite cannot retrofit existing FKs. Deleting a branch must remove exchanges; deleting an exchange must remove its derived chunk; deleting a chunk must remove its `chunk_vec` row when vec schema exists.

Search Track B hydrates snippets by joining `chunks -> exchanges -> branches -> sessions -> projects`. `chunk_vec` remains keyed by `chunks.id` to minimize vector-index churn.

### Existing Data Upgrade

Use an idempotent upgrade routine during schema setup:

1. Create `exchanges` if absent.
2. Insert exchange rows from existing `chunks` using `(branch_id, exchange_index, first_message_uuid, timestamp, user_text, assistant_text)` and compute `exchange_hash` from user/assistant text.
3. Add `chunks.exchange_id` if absent.
4. Populate `chunks.exchange_id` by joining on `(branch_id, exchange_index)`.
5. Add indexes.
6. Leave old `chunks` columns in place.

Do not introduce a generic migration framework in this pass. Do not reuse the generic `schema_version` table name because token analytics already uses it.

### DB-Backed Jobs

Add a small jobs table:

```sql
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed')),
  payload_json TEXT,
  dedupe_key TEXT UNIQUE,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  finished_at TEXT,
  heartbeat_at TEXT,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_kind ON jobs(status, kind);
CREATE INDEX IF NOT EXISTS idx_jobs_heartbeat ON jobs(heartbeat_at);
```

First migrated process: `ccrecall import`.

SessionStart setup should enqueue an import job with a stable dedupe key such as `import:all`, then spawn a one-shot worker. Dedupe applies only to active work: if an existing job for the key is `queued` or non-stale `running`, enqueue is a no-op; if the existing job is `succeeded`, `failed`, or stale `running`, enqueue must reopen that row by setting it back to `queued`, clearing `last_error`/terminal timestamps as appropriate, and incrementing/retaining attempts according to the worker's accounting. Do not use `INSERT OR IGNORE` in a way that permanently suppresses future imports after the first terminal job.

The worker claims one queued or stale import job transactionally, runs the existing import implementation, and marks success/failure. A transitional PID guard around the worker is acceptable, but durable dedupe/status lives in the jobs table.

For this first pass, stale-running semantics can stay intentionally small: a `running` import job with `heartbeat_at` older than a fixed timeout is claimable; otherwise, it is treated as active. The timeout should be long enough for normal imports and primarily protects against crashed workers.

Do not migrate `sync-current` first. It mixes hook input temp files, Stop-hook timing, and sync concurrency. Do not migrate `backfill embeddings` first because it depends on the optional semantic boundary.

### Token Analytics Removal

Remove active token analytics entirely:

- Delete or stop registering `cmd_tokens`.
- Remove `token_dashboard_mod` imports from CLI registration.
- Remove `/ccr-tokens` from docs and plugin skill bundle.
- Remove token tests and token fixtures except for preservation coverage around orphaned token tables.
- Leave existing token analytics tables and `~/.ccrecall/dashboard.html` files untouched.

## Replacement Targets

- `src/ccrecall/db.py` — replace broad module with narrower runtime modules.
- `src/ccrecall/schema.py` — add `exchanges`, `jobs`, and additive chunk exchange linkage.
- `src/ccrecall/session_ops.py` — replace in-memory/chunk-owned exchange diff with canonical exchange upsert plus derived embedding work.
- `src/ccrecall/search_conversations.py` — replace direct semantic imports with optional semantic-store boundary and add `search-messages` unavailable caveat.
- `src/ccrecall/hooks/memory_setup.py` — replace direct import spawning with import-job enqueue plus one-shot worker spawn; gate warm-model on semantic availability.
- `src/ccrecall/hooks/import_conversations.py` — adapt existing import implementation to be callable by the first job worker.
- `src/ccrecall/hooks/sync_current.py` — keep process model but update imports and disable embed-on-write cleanly without semantic support.
- `src/ccrecall/hooks/backfill_embeddings.py` and `warm_model.py` — keep semantic-only behavior with clear absent-extra handling.
- `src/ccrecall/embeddings.py` — make import-safe without native deps and remove numpy if possible.
- `src/ccrecall/cli/commands.py` — remove tokens command and avoid semantic-import failures during base CLI registration.
- `src/ccrecall/token_*.py`, `src/ccrecall/templates/dashboard.html`, `tests/test_token*.py`, `tests/test_ingest_token_data.py`, `tests/token_helpers.py` — remove active token analytics source/tests unless a minimal orphan-table preservation fixture is retained elsewhere.
- `skills/ccr-tokens/` — remove from bundled plugin.
- `README.md`, `CLAUDE.md`, `skills/ccr-recall/references/tool-reference.md` — update active product docs.

## Migration

The migration is additive and one-way.

- Existing core conversation rows are preserved.
- Existing token analytics tables are ignored and preserved.
- Existing `chunks` rows are promoted into `exchanges` where possible.
- Existing `chunks` rows gain `exchange_id` linkage.
- Existing `chunk_vec` rows remain derived data keyed by `chunks.id`.
- Old redundant `chunks` columns remain for this pass.
- Future sync/import runs prune missing exchanges and their derived chunks/vectors.

Downgrading should be mostly safe because old code can ignore new `exchanges`, `jobs`, and `chunks.exchange_id` columns, and token tables are not dropped.

## Convention Examples

### Thin CLI Wrappers

**Source:** `src/ccrecall/cli/commands.py`

```python
@app.command(name="recent")
def cmd_recent(..., ctx: CLIContextParam = DEFAULT_CLI_CONTEXT) -> None:
    """List recent conversation sessions."""
    recent_chats_mod.run(..., output_format=ctx.output_format, ...)
```

New CLI commands should stay as thin cyclopts wrappers over module-level `run()` logic.

### Hook JSON Stdout Discipline

**Source:** `src/ccrecall/hooks/memory_sync.py`

```python
try:
    subprocess.Popen(["ccrecall", "sync-current", "--input-file", tmp_path], **kwargs)
except Exception:
    log_hook_exception("memory-sync")

print(json.dumps({"continue": True}))
```

Hook adapters must catch failures best-effort and print only the hook JSON envelope.

### Vec Fixture Isolation

**Source:** `tests/conftest.py`

```python
def make_vec_conn(db_path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _ensure_vec_schema(conn)
    conn.commit()
    return conn
```

Semantic tests should isolate vec setup and skip/degrade when semantic support is absent.

### Boundary Validation

**Source:** `src/ccrecall/models.py`

```python
def is_valid(model: type[BaseModel], data: object, label: str) -> bool:
    try:
        model.model_validate(data)
    except ValidationError as e:
        _LOG.info("Skipping malformed %s: %s", label, e)
        return False
    return True
```

External Claude Code JSON remains untrusted input and should be validated/skipped at boundaries.

## Alternatives Considered

- **Keep token analytics as deprecated active code.** Rejected because the user does not use it and wants the active surface removed; preserving tables is enough.
- **Make semantic optional only at runtime but keep package dependencies mandatory.** Rejected because install fragility and native dependency drag are part of the problem.
- **Migrate all PID-file background work to jobs now.** Rejected as too broad; import is the safest first job because it is already deduped by `import_log` and avoids semantic/native failure modes.
- **Rebuild `chunks` table immediately.** Rejected because additive `exchange_id` migration preserves history with less SQLite DDL risk.
- **Add FTS exchange fallback for `search-messages`.** Rejected as a new retrieval feature outside this cleanup pass.

## Test Strategy

### Existing Tests to Adapt

- `tests/test_db.py` — update for extracted modules, `exchanges`, `jobs`, optional semantic behavior, and `chunks.exchange_id`.
- `tests/conftest.py` — remove token fixtures; make semantic fixtures isolated/skippable.
- `tests/test_search.py` — update `search-messages` unavailable caveat and exchange hydration through `exchanges`.
- `tests/test_embeddings.py` — update if numpy normalization is removed; keep integration tests semantic-extra only.
- `tests/test_backfill_embeddings.py` — update for `chunks.exchange_id` and semantic-extra absence behavior.
- `tests/test_import_pipeline.py`, `tests/test_sync_hook.py`, `tests/test_integration.py` — assert sync/import creates exchanges and preserves core rows.
- `tests/test_health.py` — preserve semantic-native-free import guard.
- `tests/test_token_parser.py`, `tests/test_ingest_token_data.py`, `tests/test_token_output.py`, `tests/test_token_insights.py` — remove with active token analytics.
- `tests/test_cli_context.py` and CLI coverage — update command registry/help expectations after removing `tokens`.
- `tests/test_legacy_migration.py` — ensure legacy copy plus schema setup preserves token tables and creates additive new schema.

### New Test Coverage

- Base install simulation for FTS-only search without semantic imports. (FR#1, FR#2)
- `search-messages` unavailable caveat in markdown and JSON. (FR#4)
- Token orphan preservation. (FR#6)
- Sync/import exchange creation without semantic support. (FR#7)
- Existing chunk promotion into exchanges and `chunks.exchange_id`. (FR#8, FR#9)
- Branch delete and exchange prune cascades remove exchanges, chunks, and chunk vectors. (FR#7, FR#8)
- Import job enqueue dedupe and worker claim behavior. (FR#11, FR#12)
- Hook stdout under job enqueue/worker spawn failures. (FR#10, FR#11)

### Tests to Remove

- Token parser, token output, token insight, token dashboard, and token ingest tests tied to removed active functionality.

## Documentation Updates

- `README.md` — document base vs `semantic` install, remove tokens skill/command, update semantic degradation, update DB table list, update data flow for import jobs.
- `CLAUDE.md` — update architecture map, remove active token subsystem, add optional semantic boundary invariant.
- `skills/ccr-recall/SKILL.md` and `skills/ccr-recall/references/tool-reference.md` — document base FTS behavior and `search-messages` semantic caveat.
- `skills/ccr-tokens/SKILL.md` — remove from active plugin bundle.
- `CHANGELOG.md` or release notes — mention token analytics removal and optional semantic install.
- `.claude-plugin/plugin.json` — no content change required unless keywords/description are adjusted during release.

## Impact

### Changed Files

- `pyproject.toml` — modify: move semantic deps to optional extra.
- `src/ccrecall/db.py` — modify/split: remove broad responsibilities and semantic top-level imports.
- `src/ccrecall/paths.py` — create: runtime paths.
- `src/ccrecall/settings.py` — create: config/settings.
- `src/ccrecall/database.py` — create: SQLite connection/schema setup.
- `src/ccrecall/runtime_files.py` — create: atomic JSON/temp/PID helpers during transition.
- `src/ccrecall/logging_config.py` — create: logging setup.
- `src/ccrecall/semantic_store.py` — create: sqlite-vec/vector storage boundary.
- `src/ccrecall/schema.py` — modify: add `exchanges`, `jobs`, additive upgrade DDL.
- `src/ccrecall/session_ops.py` — modify: canonical exchanges and derived embeddings.
- `src/ccrecall/search_conversations.py` — modify: optional semantic boundary and caveat.
- `src/ccrecall/hooks/memory_setup.py` — modify: import job enqueue and semantic warm gating.
- `src/ccrecall/hooks/import_conversations.py` — modify: callable job worker target.
- `src/ccrecall/hooks/sync_current.py` — modify: clean no-semantic embed skip.
- `src/ccrecall/hooks/backfill_embeddings.py` — modify: absent-extra message/failure behavior.
- `src/ccrecall/hooks/warm_model.py` — modify: absent-extra no-op/failure behavior.
- `src/ccrecall/embeddings.py` — modify: import-safe native handling, likely remove numpy.
- `src/ccrecall/cli/commands.py` — modify: remove tokens and avoid optional-dep import failures.
- `src/ccrecall/token_*.py` — delete or orphan from active imports.
- `src/ccrecall/__init__.py` — modify: remove token subsystem module descriptions.
- `skills/ccr-tokens/` — delete from active plugin bundle.
- `tests/` — modify/delete as described in Test Strategy.
- `README.md`, `CLAUDE.md`, `skills/ccr-recall/references/tool-reference.md` — modify docs.

<!-- Gap check 2026-07-01: included unlisted dependencies in task planning — src/ccrecall/__init__.py token docs, tests/test_boundary_validation.py token parser tests, tests/test_context_injection.py semantic import guard comments, tests/test_formatting.py snippet/exchange fields, tests/test_legacy_migration.py warm-model spawn expectations, skills/ccr-recall/SKILL.md search-messages guidance. -->

### Behavioral Invariants

- Hook stdout remains JSON-only.
- Hook entry points remain direct console scripts, not `ccrecall hook ...` subcommands.
- SessionStart context/health path does not import semantic native dependencies.
- Existing conversation rows are preserved.
- `ccrecall search` remains the primary keyword/session search path.
- Semantic vector data remains derived and rebuildable.

### Blast Radius

This affects packaging, CLI command registration, hook setup/sync, schema initialization, import/sync pipelines, semantic search/backfill, tests, and documentation. Token analytics users lose the command/skill, but existing token tables remain untouched.

## Open Questions

None.
