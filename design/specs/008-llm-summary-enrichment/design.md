# Design: LLM Summary Enrichment

**Date:** 2026-08-07
**Status:** approved
**Scope-mode:** hold
**Research:** design/research/2026-08-07-issue-32-llm-session-summaries/research.md

## Problem

`ccrecall` currently stores deterministic branch context that is safe, local, fast, and predictable, but it loses much of the "what happened and why" context in long branches. The current middle-branch summary is mostly a file-name gap marker, so recalled work can preserve endpoints while still missing decision rationale, unresolved work, failed approaches, and continuation hints.

The user wants richer recall/search-card UX using the user's existing Claude Code auth, without putting LLM calls on hook stdout paths and without replacing the deterministic summary contract that currently keeps SessionStart reliable.

## Goals

- Preserve deterministic summaries as the baseline and fallback for every existing consumer.
- Add an optional, Claude-backed **branch resume brief** that makes the latest state, causal history, decisions, failed paths, open work, and next steps easy to recover.
- Keep the resume brief bounded and evidence-backed: it should orient a future session quickly, not attempt to replace the deterministic exchange record.
- Use a controlled research-style Claude Code summarizer prompt that can read a branch-scoped transcript packet, but cannot edit files, run arbitrary shell tools, or load user customizations.
- Support two first-version execution paths: an explicit manual backfill command and opt-in current-session enrichment after sync.
- Keep inline SessionStart/Stop hooks fast and JSON-only on stdout.
- Avoid writing raw transcript prompts, transcript excerpts, or raw Claude responses to ccrecall-owned logs. Claude Code may still create its own transcript unless the capability check proves `--no-session-persistence` prevents it.

## Non-Goals

- Do not embed LLM summaries into vector search in the first version.
- Do not add the Python Agent SDK in the first version.
- Do not replace `summary_version` or the deterministic `context_summary_json` shape.
- Do not make historical LLM backfill automatic on SessionStart.
- Do not support arbitrary provider/model integrations beyond the installed `claude` CLI in the first version.

## User Scenarios

### User: ccrecall user resuming work

#### Current Session Enrichment

1. **Enables LLM summary enrichment**
   - Sees: documentation or config that says branch-scoped transcript content and session metadata will be sent through Claude Code auth, and that `ccrecall backfill llm-summaries --check-capability` must pass first.
   - Decides: opt in and run the capability check because Claude was already part of the original conversation.
   - Then: future Stop-hook syncs may spawn a detached enrichment worker after the session is synced, but actual Claude calls run only when the capability sidecar is present and valid.

2. **Starts a later Claude Code session**
    - Sees: an injected **Branch Resume Brief** with the latest state, how the work reached that state, decisions with rationale, attempted/abandoned paths, open questions, and continuation hints above the deterministic exchange context.
   - Decides: whether to continue from the hints or use `/ccr-recall` for more detail.
   - Then: if enrichment is missing or invalid, the injected context is exactly the deterministic summary behavior.

### User: ccrecall user improving history

#### Manual Backfill

1. **Runs a manual LLM-summary backfill**
   - Sees: CLI progress or completion output that does not print transcript content.
   - Decides: optional filters such as limit, days, or session selector.
   - Then: eligible branches get enrichment status/version fields updated.

2. **Searches recent sessions**
    - Sees: session cards can prefer the LLM branch-resume title and latest-state preview when available.
   - Decides: which result to open or recall.
   - Then: ranking behavior remains unchanged unless a later feature explicitly changes indexing.

## Functional Requirements

- **FR#1** The system must preserve deterministic `context_summary`, `context_summary_json`, and `summary_version` behavior when LLM enrichment is disabled, missing, invalid, or failed.
- **FR#2** The system must store LLM enrichment data separately from deterministic summary versioning.
- **FR#3** The system must validate Claude output against a strict schema before persisting it as usable enrichment.
- **FR#4** The system must render LLM enrichment above deterministic context only when the enrichment version is current, source fingerprint matches, and status is successful.
- **FR#5** The system must provide a manual CLI path for enriching eligible historical/current sessions.
- **FR#6** The system must optionally spawn current-session enrichment only after `sync-current` has completed DB sync work and without adding hook-visible stdout.
- **FR#7** The Claude invocation must run through an argument-list subprocess call, not a shell string.
- **FR#8** The Claude invocation must restrict the summarizer to transcript reading and structured output; it must not grant edit tools or arbitrary shell tools.
- **FR#9** The worker must not hold an open DB write transaction while waiting for Claude.
- **FR#10** The worker must record retryable failure status without poisoning deterministic summaries or preventing future deterministic backfills.
- **FR#11** The feature must be opt-in and must document that selected local transcript content, branch/session metadata, and source-path provenance are sent through Claude Code auth.
- **FR#12** The worker must detect stale enrichment when branch content or deterministic summary input changes.
- **FR#13** The Claude invocation must receive only a branch-scoped packet directory via `--add-dir`, not the original transcript source directories.
- **FR#14** The enriched output must represent one active branch for one session, not a whole project history, session family, or inactive fork.
- **FR#15** The enriched output must distinguish the latest branch state from the causal history that led to it, and preserve evidenced decision rationale, failed/abandoned paths, unresolved work, and continuation hints when those facts exist in the source.
- **FR#16** Every factual resume-brief section must cite message UUIDs from the active branch; uncited or unsupported claims must be rejected rather than rendered.
- **FR#17** Rendered enrichment must have explicit budgets for the primary selected session and each supplementary selected session so it cannot displace the deterministic exchange record from useful SessionStart context.
- **FR#18** The worker must pass the user-configured model and maximum-budget setting to Claude unchanged, while treating Claude's reported budget as a stop threshold rather than a guaranteed spend ceiling.

## Edge Cases

- `claude` is not installed or not on `PATH`: mark enrichment failed with a retryable error code and keep deterministic output.
- Claude Code auth is missing, expired, rate-limited, or over budget: mark enrichment failed with a retryable or terminal status depending on the error category and keep deterministic output.
- The installed `claude` does not support a planned flag: fail fast during worker startup and record a concise `unsupported_cli` status.
- The transcript path no longer exists: record `missing_source` rather than retrying forever.
- The transcript source set fails source validation: refuse the run and record `unsafe_source_path`.
- The branch has no deterministic summary or no branch messages: it is not selected yet and enrichment remains pending (`NULL`) so a later run can retry after deterministic summary backfill succeeds.
- Claude emits invalid JSON, schema-invalid JSON, oversized fields, or unsupported enum values: reject the response and keep deterministic output.
- Claude summarizes messages outside the active branch: validation cannot fully prove this, so the packet contains only branch-scoped transcript data and requires source UUID citations for key facts.
- The branch contains no evidenced decision, failed path, or open question: Claude omits that section rather than inferring one from tool activity or generating generic advice.
- A deterministic summary refresh or new session sync runs after enrichment: enrichment remains separate, and stale enrichment is hidden until regenerated for the new source fingerprint.
- A second enrichment process starts while one is alive: PID guard skips rather than queues.

## Acceptance Criteria

- **AC#1** With enrichment disabled, existing summary, context injection, search, and hook tests pass unchanged or with only fixture updates unrelated to behavior.
- **AC#2** Unit tests prove valid enrichment renders above deterministic context, and absent/failed/stale enrichment renders deterministic context only.
- **AC#3** Unit tests reject malformed Claude outputs including invalid JSON, missing required fields, wrong types, oversized strings, unknown top-level fields, invalid confidence values, and invalid source UUIDs. Tests also prove the worker, not Claude, supplies persisted version, model, and generation timestamps.
- **AC#4** Worker tests with mocked subprocess cover success, timeout, nonzero exit, missing binary, unsupported flag, malformed stdout, and stderr diagnostics without logging raw prompts/responses.
- **AC#5** DB migration tests prove enrichment columns are added idempotently and old DBs retain deterministic summary data.
- **AC#6** Hook contract tests prove `memory_sync.py`, `memory_context.py`, and `memory_setup.py` still print only their JSON envelopes and do not import the LLM summarizer boundary; `sync_current.py` may spawn the worker but must not run Claude directly or print additional hook-visible stdout.
- **AC#7** A local command test or mocked command test proves the direct internal `ccrecall-llm-summaries --limit 1` worker entry point invokes the worker path without importing embedding modules, opening vec tables, setting `load_vec=True`, or constructing the embedding model during startup or worker execution.
- **AC#8** A search hydration test proves the LLM branch-resume title/latest-state preview are preferred on result cards when valid and current for the source fingerprint, and deterministic topic fallback remains when not.
- **AC#9** A manually reviewed evaluation corpus covers long branches with known decisions, rationale, failed/abandoned approaches, and unresolved work. For each applicable fact, the rendered branch resume brief must surface it, while the stored output retains valid active-branch message UUID citations as validation provenance; each citation must support the claim it accompanies, not merely belong to the active branch. The corpus must include bug investigation, implementation/refactor, and planning/discovery examples.
- **AC#10** Rendering tests prove that the primary selected session's branch resume brief stays within its explicit character budget, retains at least one evidenced continuation hint when one exists, supplementary-session briefs use their smaller budget, and the deterministic exchange context remains present below both.
- **AC#11** Worker invocation tests prove the configured model and `$1.00` default budget threshold are passed to Claude, and documentation explains that the CLI can exceed the threshold before stopping.

## Key Constraints

- LLM calls must never run inside inline hook entry points such as `memory_sync.py` or `memory_context.py`. `sync_current.py` is already a detached helper; it may spawn another detached worker, but it must not run Claude in its own process.
- `--bare` must not be the default invocation mode because installed help says it bypasses OAuth/keychain auth and requires API-key-style auth.
- Use `--safe-mode` for the Claude invocation so user customizations, plugins, hooks, MCP servers, slash commands, and custom agents are disabled. The summarizer role is supplied as prompt/system-prompt text, not as a custom Claude Code agent.
- Claude gets branch-scoped transcript packet read access only after explicit opt-in; this is acceptable because Claude was already part of the original conversation, but it still changes local-only deterministic processing into provider-backed processing.
- Raw prompts, transcript excerpts, and raw Claude responses must not be written to ccrecall-owned logs. This does not by itself control Claude Code's own transcript persistence; that is guarded separately by the capability check.
- The feature must not change search ranking or embeddings in the first version.
- V1 preserves a complete validated branch packet rather than truncating source evidence to meet a fixed cost target. `--max-budget-usd` is an upstream stop threshold, not a hard spend ceiling: a call can exceed it before Claude stops. The default threshold is `$1.00`, is user-configurable, and must be disclosed wherever enrichment is enabled or invoked manually.

## Dependencies and Assumptions

- Depends on the installed `claude` CLI supporting the observed flags: `-p`, `--safe-mode`, `--disable-slash-commands`, `--strict-mcp-config`, `--mcp-config`, `--tools`, `--allowedTools`, `--permission-mode`, `--add-dir`, `--output-format json`, `--json-schema`, `--max-budget-usd`, `--model`, `--effort`, `--append-system-prompt`, and `--no-session-persistence`.
- Assumes `claude -p` with normal mode can use the user's existing Claude Code auth on the target machine.
- Requires `--no-session-persistence` to prevent the enrichment invocation from creating a new Claude Code transcript that `ccrecall` would later import or expose. Implementation must verify this before either manual backfill or current-session enrichment can call Claude. If verification fails or cannot be performed, this design is not feasible as an automatic feature: the worker exits without invoking Claude and records the classified capability status (`capability_unverified`, `unsupported_cli`, `claude_unavailable`, `auth_required`, `rate_limited`, `budget_exceeded`, or `error`).
- Accepts that Claude will read selected transcript content and branch/session metadata when enrichment is enabled. Mitigation: explicit opt-in config, a branch-scoped temporary transcript packet, `Read`-only tool access, transcript path safety checks before packet creation, no raw ccrecall-owned logs, deterministic fallback, and a hard capability gate that blocks the feature if Claude Code persists importable transcripts for `--no-session-persistence` calls.
- Accepts that LLM output may be imperfect. Mitigation: strict schema, bounded fields, source UUID citations for factual sections, and deterministic context rendered below the LLM block.

## Architecture

### Existing code leverage

- `src/ccrecall/summarizer.py` remains the deterministic summary owner. `SUMMARY_VERSION`, `build_context_summary_json()`, `render_context_summary()`, and `compute_context_summary()` continue to define the baseline shape.
- `src/ccrecall/branch_ops.py:sync_branch()` continues to call `write_branch_summary()` during sync/import; it should not call Claude directly.
- `src/ccrecall/hooks/sync_current.py` already runs detached from `memory_sync.py`, resolves the current transcript with `get_session_file()`, and closes the DB connection before printing output. It is the right place to spawn a second detached enrichment command after successful sync, not to run the enrichment inline.
- `src/ccrecall/hooks/backfill_summaries.py` provides the deterministic summary batch loop; existing PID helpers in `config.py` provide the guard pattern the new worker should own directly.
- `src/ccrecall/config.py` provides `DEFAULT_SETTINGS`, `load_settings()`, `try_acquire_pid_file()`, `remove_pid_file()`, and per-process logging.
- `src/ccrecall/import_log_ops.py` and `import_log.file_path` provide historical transcript source paths.
- `src/ccrecall/search_hydrate.py` is the search-card hydration point that can prefer the LLM branch-resume title/latest-state preview for display without changing retrieval.
- `src/ccrecall/hooks/context_rendering.py` is the SessionStart rendering point that can combine valid enrichment with deterministic markdown.

### Data model

Add separate enrichment columns to `branches`. The enrichment describes the active branch for one session; it does not claim to summarize inactive forks, other sessions, or the project as a whole. The order below is logical; the physical schema must append these columns after the current trailing `embedding_version`, `embedding_model`, and `summary_version_at_embed` columns because the existing schema comments call out ALTER-append order and some code has historically depended on positional compatibility.

```sql
summary_enrichment_json TEXT,
summary_enrichment_version INTEGER DEFAULT 0,
summary_enrichment_source_hash TEXT,
summary_enrichment_status TEXT,
summary_enrichment_error TEXT,
summary_enrichment_updated_at DATETIME,
summary_source_hash TEXT
```

`summary_enrichment_error` stores a capped diagnostic string of at most 240 characters. Retryability is controlled by `summary_enrichment_status`, not by this diagnostic. The design requires no raw prompt/response logging.
`summary_enrichment_updated_at` records the last enrichment status update, including failures and skips, not only successful `ok` writes.

`summary_enrichment_version` is independent from `summary_version`. Deterministic `SUMMARY_VERSION` still means "the deterministic summary JSON/markdown are current." A new constant, for example `SUMMARY_ENRICHMENT_VERSION = 1`, means "the enrichment JSON matches the current LLM enrichment schema and prompt contract."

Recommended status values and retry policy:

```text
NULL                 Retryable: never attempted or intentionally reset; this is the canonical pending representation
ok                   Not retryable unless stale or --force: current enrichment is valid
capability_unverified Retryable after successful --check-capability: no valid capability sidecar exists
missing_source       Retryable after source restoration/re-import: transcript path no longer exists
unsafe_source_path   Not retryable: transcript path failed current-session or historical source validation
source_changed       Retryable after re-import: transcript file no longer matches the imported DB state
source_incomplete    Retryable after re-import: validated source files do not cover every branch message UUID
source_unverified    Retryable after re-import: only live-sync placeholder sources exist, with no hash/stat proof
unsupported_cli      Not retryable until Claude Code is upgraded: required CLI flags are unavailable
claude_unavailable   Retryable after install or PATH fix: claude binary/runtime unavailable
auth_required        Retryable after user action: Claude Code auth is missing or expired
rate_limited         Retryable: provider or subscription rate limit
budget_exceeded      Retryable only with --force
timeout              Retryable: Claude invocation exceeded the configured timeout
invalid_output       Retryable only with --force: Claude returned non-JSON or schema-invalid output
error                Retryable: unexpected worker error
```

Only `ok` with the current `SUMMARY_ENRICHMENT_VERSION`, non-null `summary_enrichment_source_hash`, non-null `summary_source_hash`, and matching hash values is renderable. All other statuses are diagnostic and deterministic fallback remains active.

`summary_source_hash` is the current deterministic fingerprint for the branch summary source. Maintain it during sync/backfill when branch content is already being computed, not on SessionStart/search hot paths. It should cover `leaf_uuid`, `summary_version`, `context_summary_json`, a hash of `aggregated_content`, `exchange_count`, `started_at`, `ended_at`, `files_modified`, `tool_counts`, `commits`, and `sessions.git_branch`. Including an `aggregated_content` hash is what makes middle-session content changes stale enrichment even when deterministic endpoint summaries do not change, while storing only the resulting hash keeps hot-path validation cheap. Do not include the ordered branch message UUID list on hot paths. Do not include original transcript source paths; they are provenance, not summary content, and path-only changes should not stale enrichment.

Use one canonical serialization helper in `summary_enrichment.py` for this hash across sync, deterministic backfill, and the LLM worker: normalize decoded JSON fields, sort object keys, preserve list order where order is meaningful, use explicit `null` for missing values, encode as UTF-8 JSON with fixed separators, and hash with SHA-256. Do not hand-roll separate hash assembly in each caller.

When enrichment succeeds, copy the current `summary_source_hash` into `summary_enrichment_source_hash`. Rendering and search only compare the two stored hashes; they do not recompute from large transcript fields.

The LLM worker must treat `summary_source_hash` as a compare-and-swap guard. Capture the expected `summary_source_hash` when building the packet. After Claude returns, write enrichment results or failure status only with an optimistic predicate such as `WHERE id = ? AND summary_source_hash = ?`. If the update affects zero rows, the branch changed while Claude was running; discard the result and leave the newer branch state untouched.

Existing rows may have current deterministic summaries but `summary_source_hash IS NULL` immediately after migration. The LLM enrichment worker must compute and persist `summary_source_hash` on demand before invoking Claude. Deterministic summary backfill/sync paths should also populate it going forward. No whole-DB rewrite is required at migration time.

Whenever branch summary inputs change, invalidate `summary_source_hash` before or alongside the branch update. If deterministic summary recomputation succeeds, write the new `summary_source_hash`. If deterministic summary recomputation fails or marks a content error, leave `summary_source_hash = NULL` so any old enrichment stops rendering because the stored hashes no longer match. This invalidation is independent of Claude enrichment success.

### Stored enrichment envelope

`STORED_ENRICHMENT_ENVELOPE` is the JSON persisted in `summary_enrichment_json`. The worker owns this envelope: after validating Claude's factual brief body, it adds `version`, `model`, and `generated_at`.

Persist only validated, bounded fields:

```json
{
  "version": 1,
  "model": "<worker-recorded configured model>",
  "generated_at": "<worker-recorded completion timestamp>",
  "title": {
    "text": "Short branch-resume title",
    "source_uuids": ["..."]
  },
  "where_we_left_off": {
    "text": "Latest concrete state at the end of this branch",
    "source_uuids": ["..."]
  },
  "how_we_got_here": {
    "text": "Short causal history explaining why the branch reached that state",
    "source_uuids": ["..."]
  },
  "key_decisions": [
    {
      "decision": "Decision made",
      "rationale": "Why it was chosen",
      "source_uuids": ["..."]
    }
  ],
  "attempted_paths": [
    {
      "text": "Approach that was tried",
      "outcome": "failed|abandoned|inconclusive",
      "why_stopped": "Why it did not continue",
      "source_uuids": ["..."]
    }
  ],
  "open_questions": [
    {"text": "Unresolved question", "source_uuids": ["..."]}
  ],
  "files_and_reasons": [
    {"path": "src/example.py", "reason": "Why it mattered", "source_uuids": ["..."]}
  ],
  "continuation_hints": [
    {"text": "Useful next step", "source_uuids": ["..."]}
  ],
  "confidence": "high|medium|low"
}
```

### Claude response body schema

`CLAUDE_RESPONSE_SCHEMA` is the schema passed to `claude --json-schema`. It has `additionalProperties: false`, requires exactly `title`, `where_we_left_off`, `how_we_got_here`, `key_decisions`, `attempted_paths`, `open_questions`, `files_and_reasons`, `continuation_hints`, and `confidence`, and applies the field shapes and caps below. It deliberately excludes `version`, `model`, and `generated_at`; the worker rejects those fields if Claude emits them. The worker constructs `STORED_ENRICHMENT_ENVELOPE` by adding its own three metadata fields to a validated response body.

Validation rules:

- The worker writes `version = SUMMARY_ENRICHMENT_VERSION`, the configured `llm_summary_model`, and its own completion timestamp after the Claude response body passes validation.
- Strings longer than the schema caps are invalid and must be rejected before persistence; rendering may still apply shorter display caps to already-valid stored fields.
- Lists must have small maximum lengths.
- `title`, `where_we_left_off`, `how_we_got_here`, and every list item must carry non-empty `source_uuids` that are UUID strings present in the branch message UUID set.
- `attempted_paths.outcome` must be one of `failed`, `abandoned`, or `inconclusive`; omit the item when the source does not establish one of those outcomes.
- `files_and_reasons.path` must either appear in deterministic `files_modified` or validate against branch content; otherwise reject the Claude output rather than persisting hidden invalid paths.
- Reject unknown top-level fields in the Claude response body in v1 so schema drift is visible in tests.

Concrete v1 caps:

```text
title: text max 120 characters; source_uuids max 5
model: worker-written configured model, max 120 characters
generated_at: worker-written ISO timestamp string, max 64 characters
where_we_left_off: text max 600 characters; source_uuids max 5
how_we_got_here: text max 600 characters; source_uuids max 5
key_decisions: max 4 items; decision max 180 characters; rationale max 240 characters; source_uuids max 5 per item
attempted_paths: max 3 items; text max 180 characters; why_stopped max 180 characters; source_uuids max 5 per item
open_questions: max 4 items; text max 180 characters; source_uuids max 5 per item
files_and_reasons: max 6 items; path max 300 characters; reason max 180 characters; source_uuids max 5 per item
continuation_hints: max 3 items; text max 180 characters; source_uuids max 5 per item
confidence: one of high, medium, low
```

These are validation caps, not display caps. Rendering must apply a separate total character budget: 2,400 characters for the primary selected session and 800 characters for each supplementary selected session. The primary renderer prioritizes title, latest state, continuation hints, causal history, decisions, attempted paths, then open questions. When a primary brief has an evidenced continuation hint, it must render at least one before lower-priority sections. The supplementary renderer shows title, latest state, and at most one continuation hint or open question. In both cases, the deterministic context remains below the LLM block and is never removed to make room for enrichment.

Source UUIDs are retained to validate every factual claim and support future source navigation, but are not rendered in the SessionStart brief in v1. The producing model uses them as evidence metadata; they are not useful user-facing resume prose. The brief is evidence-backed through validation rather than by exposing opaque UUIDs in the rendered text.

### Claude input packet

For each branch, the worker builds a temporary branch packet directory with owner-only permissions. Claude receives a path to that packet directory, not `--add-dir` access to the original transcript parent directory. The worker may include the original transcript path inside metadata for provenance, but Claude is granted read access only to the packet directory.

The packet directory contains:

- `branch-transcript.jsonl`: a branch-scoped JSONL file built by the worker from validated source transcript files for the session. It contains only messages whose UUIDs belong to the active branch and only fields needed for summarization, including each message UUID so Claude can cite `source_uuids` and validation can check them.
- `branch-outline.json`: an ordered, deterministic navigation map of branch exchanges. Each entry records exchange order, timestamp, participant/message UUIDs, a bounded user-intent preview, a bounded assistant/result preview, and relevant tool/file signals. It is not a second summary; it lets the Read-only model identify important middle-branch events before opening the detailed transcript.
- `branch-metadata.json`: session/branch metadata plus the original transcript path as provenance.
- `deterministic-summary.json`: the current deterministic `context_summary_json`.
- `allowed-uuids.txt`: one allowed branch message UUID per line.

At least one source transcript path is required in v1 and every source path used for packet construction must pass source validation. Implement one source resolver shared by current-session and historical/manual enrichment. It returns validated existing source files for the target session and then proves those files cover the branch message UUIDs before packet construction.

For the current-session path, `sync_current.get_session_file()` is only a hint that identifies the active session and one known source file. The resolver must mirror that function's directory walk under the active Claude projects directory: check each project directory for direct `<session_uuid>.jsonl` files, then check nested `subagents/` directories for files whose filename contains the session UUID. Apply the same symlink containment check that `get_session_file()` uses, and reject source paths that are themselves symlink files. Historical and current-session packet sources should follow the same file-safety rule; the only asymmetry is that historical imports cannot be checked against a stored projects-root boundary because the schema does not retain one.

For historical/manual runs, collect existing `import_log.file_path` entries whose `parsing.extract_session_uuid(path)` matches the target session UUID. Treat `import_log` as candidate provenance, not as a mandatory complete source set: stale missing duplicates should not poison enrichment if the remaining validated files cover the branch. Validate that each candidate path resolves to a regular file, is not itself a symlink, and still matches its import-log stat/hash record before including it. Do not require the basename to be exactly `<session_uuid>.jsonl`; `agent-<uuid>.jsonl` is valid. Do not reject solely because the path is outside `DEFAULT_PROJECTS_DIR`, since `ccrecall import --projects-dir ...` already supports non-default Claude projects roots and the schema does not retain that import root for later containment checks.

Historical/manual runs must verify source fidelity before packet construction. Compare each existing candidate file's current stat/hash against its `import_log.file_size`, `import_log.file_mtime`, and `import_log.file_hash` when available. Exclude candidates that no longer match. If no candidates remain and at least one existing candidate mismatched, write `summary_enrichment_status = 'source_changed'` until the user re-runs import/sync so branch rows and transcript content agree. If all candidates are excluded only because they are live-sync placeholders with `file_hash = NULL` and no usable stat proof, write `summary_enrichment_status = 'source_unverified'` until re-import records a verifiable source. If an import-log row has a `NULL` hash from live sync, use stat fields when present and otherwise exclude it from historical enrichment until re-import. DB-selected messages are useful for branch UUIDs and validation, but they are not an equivalent source for packet construction in this design.

When multiple source files exist for a session, parse all validated source files, filter entries to the active branch UUID set, deduplicate by message UUID, sort by timestamp, and write one `branch-transcript.jsonl` packet file. Use a stable tiebreaker for missing or identical timestamps: source path, then source line number, then UUID. If the same UUID appears in multiple source files with identical normalized content, keep one copy. If duplicate UUIDs disagree on normalized content, record `source_changed` and require re-import rather than guessing which copy matches the DB. Before writing the packet, compare the packet UUID set to the active branch message UUID set from the DB. If validated source files do not cover every branch UUID needed for the packet, record `source_incomplete` rather than summarizing a partial branch. Do not pick an arbitrary single source path for the session.

The packet directory contains copied transcript content and must be deleted in `finally` on every success and failure path. If cleanup fails, log only the packet path and error category, not packet contents. Because detached workers can crash or be killed before `finally` runs, packet directories must live under a dedicated owner-only parent directory and include a tiny manifest with PID and created-at timestamp. At worker start, reap stale packet directories using age and PID liveness checks before creating a new packet.

Run the Claude subprocess with `cwd` set to the packet directory or to a separate empty temporary directory that grants no useful default file access. `--add-dir` only adds allowed directories; it does not remove access to the process working directory. The invocation must therefore avoid launching from the repository/worktree or the original transcript directory.

`branch-transcript.jsonl` must preserve enough normalized evidence to distinguish user requests, assistant reasoning/prose, tool invocations, and tool results or errors when present in the source transcript. The packet format is an explicit worker contract, not an implementation-defined subset of raw transcript fields. Omit only fields that cannot support branch resumption or evidence validation.

The prompt includes:

```text
You are ccrecall-summary-enricher. Produce a factual continuation summary for one Claude Code conversation branch.

Inputs:
- Branch packet directory: <temporary packet directory>
- Branch outline path: <packet>/branch-outline.json
- Branch transcript path: <packet>/branch-transcript.jsonl
- Branch/session metadata path: <packet>/branch-metadata.json
- Branch message UUID allowlist path: <packet>/allowed-uuids.txt
- Deterministic summary path: <packet>/deterministic-summary.json

Instructions:
- This is a Branch Resume Brief for one active branch, not a whole-session or project-history summary.
- Read `branch-outline.json` and `deterministic-summary.json` first. Use the outline to locate relevant detailed transcript entries, especially the last exchanges and any middle-branch decision or failure points.
- Read the branch transcript packet files as needed to establish evidence; do not infer facts only from filenames, tool names, or metadata.
- Summarize only messages whose uuid appears in the branch message UUID allowlist.
- `title` must be a short, evidence-backed label for this branch, not a generic or speculative topic.
- `where_we_left_off` must describe the latest evidenced state, including blockers or verification status when known.
- `how_we_got_here` must explain the causal path to that state, not repeat the latest-state text.
- Include a key decision only when its rationale is evidenced. Include an attempted path only when it was evidenced as failed, abandoned, or inconclusive.
- When the branch ends with an evidenced unresolved action, blocker, or handoff, include at least one specific continuation hint. Do not add a generic next step when no such evidence exists.
- Prefer facts that help a future Claude Code session resume work. Do not invent decisions, rationale, failures, unresolved tasks, or generic next steps.
- Every factual section, including the title, and every list item must cite source_uuids from the allowlist.
- Return only the factual brief body matching the response schema. Do not emit `version`, `model`, or `generated_at`; the worker adds them after validation.
```

The temporary packet files are created with owner-only permissions. Passing branch data as files avoids stuffing long UUID lists or transcript excerpts into the command line and gives Claude explicit branch-scoped context without exposing the original transcript directory.

The branch/session metadata JSON contains:

```json
{
  "session_uuid": "...",
  "branch_id": 123,
  "leaf_uuid": "...",
  "project": "...",
  "cwd": "...",
  "git_branch": "...",
  "started_at": "...",
  "ended_at": "...",
  "exchange_count": 42,
  "files_modified": ["..."],
  "tool_counts": {"Read": 12},
  "commits": ["..."],
  "source_transcript_paths": ["..."]
}
```

### Claude invocation

Create two boundaries:

- `src/ccrecall/summary_enrichment.py`: lightweight status constants, stored-hash validity checks, JSON validation, and enriched markdown rendering. This module must be safe for `memory_context.py` to import: no `db.py`, no subprocess, no `fastembed`, no `sqlite_vec`, no hook entry imports.
- `src/ccrecall/llm_summarizer.py`: prompt construction, packet construction, Claude subprocess invocation, stdout parsing, and failure classification. This module is used only by the background worker/CLI path and must not be imported by inline hook entry points.

Recommended first invocation shape:

```python
argv = [
    "claude",
    "-p",
    "--safe-mode",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--append-system-prompt",
    SUMMARIZER_SYSTEM_PROMPT,
    "--tools",
    "Read",
    "--allowedTools",
    "Read",
    "--permission-mode",
    "dontAsk",
    "--add-dir",
    str(temp_packet_dir),
    "--output-format",
    "json",
    "--json-schema",
    json.dumps(CLAUDE_RESPONSE_SCHEMA),
    "--max-budget-usd",
    str(settings["llm_summary_max_budget_usd"]),
    "--model",
    settings["llm_summary_model"],
    "--effort",
    settings["llm_summary_effort"],
    "--no-session-persistence",
    prompt,
]
```

Use `subprocess.run(argv, capture_output=True, text=True, timeout=...)`, never `shell=True`. The worker records only error categories and short diagnostics in ccrecall-owned logs. It must not log `prompt`, `stdout`, or transcript content. Claude Code's own transcript persistence is outside ccrecall logging and is treated as a capability-gated feasibility requirement, not as logging.

Set `cwd=temp_packet_dir` or another empty owner-only temp directory in the `subprocess.run()` call. The cwd is part of the access-control design, not an incidental process option.

Before the first Claude call in any manual or auto-spawned worker run, verify the installed CLI supports and honors the no-session-persistence requirement well enough for this feature. The minimum implementation gate is: required flags are present, the invocation includes `--no-session-persistence`, and a documented local smoke test has proven it does not create importable transcripts on the target Claude Code version. If this cannot be established, do not invoke Claude; otherwise the enrichment call would create its own transcript containing the summarization prompt/packet references, defeating the privacy and import-loop assumptions.

Persist that proof as a small runtime sidecar under `~/.ccrecall/`, for example `claude-summary-capability.json`, containing the Claude Code version, checked timestamp, the security/persistence flag-shape hash, and whether no-session-persistence passed. The worker reads this sidecar before both manual and auto-spawned runs. A missing sidecar, stale CLI version, or mismatched current security/persistence flag-shape hash blocks invocation with `capability_unverified` and a concise diagnostic telling the user to run the documented capability check. A failed sidecar preserves the classified failure reason from the check, such as `unsupported_cli`, `claude_unavailable`, `auth_required`, `rate_limited`, or `budget_exceeded`.

The canonical user-facing CLI includes a read/write capability check:

```text
ccrecall backfill llm-summaries --check-capability
```

That command verifies required flags, runs the local no-session-persistence smoke test, writes the sidecar on success, and prints a human-readable pass/fail result without processing real conversation transcripts.

The smoke test should use the same **security and persistence** invocation shape as production, but with a synthetic packet containing no real transcript content. It should include `--safe-mode`, `--disable-slash-commands`, `--strict-mcp-config`, `--mcp-config '{"mcpServers":{}}'`, `--tools Read`, `--allowedTools Read`, `--permission-mode dontAsk`, `--add-dir <synthetic-packet-dir>`, `--output-format json`, `--json-schema`, and `--no-session-persistence`. `--model`, `--effort`, and `--max-budget-usd` are intentionally excluded: they do not affect session persistence. Store a hash of this security/persistence flag shape in the capability sidecar so changing a relevant flag invalidates the proof.

The smoke test should run from an isolated owner-only temporary cwd. It should snapshot `${CLAUDE_CONFIG_DIR:-~/.claude}/projects` before and after the call and fail if a new `*.jsonl` appears that `ccrecall` would consider importable: a direct session JSONL, a subagent JSONL, or any JSONL whose session UUID can be extracted by the parser/source resolver. Clean up the temporary cwd and any test artifacts in `finally`; if cleanup fails, report only paths and error categories.

If `llm_summaries_enabled` is true but the capability sidecar is missing, stale-for-version, or has a mismatched security/persistence flag-shape hash, auto-spawned current-session workers exit successfully after recording/logging `capability_unverified`; they do not call Claude. Manual backfill should print the same actionable message: run `ccrecall backfill llm-summaries --check-capability` first. If the sidecar exists but records a failed check, use that recorded status instead of collapsing every failure into `unsupported_cli`. A `budget_exceeded` recorded in the capability sidecar describes that synthetic check and recovers through a successful rerun of `--check-capability`; the branch-level `budget_exceeded` status describes a full enrichment call and recovers only through `--force`.

Classify Claude failures best-effort from subprocess outcomes. Missing binary maps to `claude_unavailable`; unsupported flags detected from a preflight or stderr usage output map to `unsupported_cli`; timeout maps to `timeout`; schema-invalid stdout maps to `invalid_output`; stderr/output strings that clearly indicate auth, rate limit, or budget map to `auth_required`, `rate_limited`, or `budget_exceeded`; ambiguous nonzero exits map to `error` with a capped diagnostic. The classifier must be conservative: prefer a broader retryable status over brittle parsing that would make a terminal claim from uncertain stderr text.

Default settings:

```json
{
  "llm_summaries_enabled": false,
  "llm_summary_model": "sonnet",
  "llm_summary_effort": "medium",
  "llm_summary_timeout_seconds": 180,
  "llm_summary_max_budget_usd": 1.00,
  "llm_summary_min_exchanges": 9
}
```

The model default is user-configurable rather than hard-coded into command construction. `sonnet` is the first default because this is a summarization/reasoning task where factual nuance and continuation quality matter more than minimum cost; users can explicitly select a cheaper model such as `haiku`. The configurable `$1.00` default budget preserves the complete-packet v1 approach. It is not a guaranteed maximum charge: the Claude CLI can spend beyond the threshold before terminating, so help and configuration documentation must disclose that behavior. The design does not use a named permanent Claude Code agent file or ephemeral custom agent because safe-mode disables custom-agent surfaces; the worker supplies the summarizer role through `SUMMARIZER_SYSTEM_PROMPT` plus the user prompt.

### Worker placement

Create `src/ccrecall/hooks/backfill_llm_summaries.py` as the worker module. It follows the same broad shape as `backfill_summaries.py` and `backfill_tool_content.py`:

- load settings and logging with process name `backfill-llm-summary`;
- acquire a PID guard at worker start for both manual and auto-spawned runs;
- select eligible branches;
- read DB state and transcript source paths;
- close or avoid write transactions before invoking Claude;
- reopen/write status and enrichment JSON after validation;
- commit between branches or small batches.

Manual CLI:

```text
ccrecall backfill llm-summaries [--days N] [--limit N] [--session UUID] [--force] [--check-capability]
```

`--days` filters by `branches.ended_at`, matching the user's mental model of recently active sessions.
`--check-capability` is mutually exclusive with selection flags (`--days`, `--limit`, `--session`, `--force`) and exits after writing/reporting the capability sidecar.
Manual `ccrecall backfill llm-summaries ...` is an explicit opt-in to process the selected sessions, even when `llm_summaries_enabled` is false. The config flag controls automatic current-session spawning only. Both manual and automatic paths still require the capability sidecar before invoking Claude.

The direct `ccrecall-llm-summaries` console script is an internal worker entry point for the detached current-session spawn. It must import only the lightweight config/DB modules it needs for this feature. `ccrecall backfill llm-summaries` is the canonical documented command and delegates to the same worker.

Current-session path:

- `sync_current.run()` keeps its existing sync behavior.
- After `sync_session()` returns and the DB connection is closed, if `new_messages > 0` and `llm_summaries_enabled` is true, spawn:

```text
ccrecall-llm-summaries --session <session_uuid> --limit 1
```

- The spawn must be detached and the worker must use a separate PID key from deterministic summary backfill and embedding backfill.
- If the worker PID is already held, the worker exits successfully without doing work. A later manual backfill or later Stop can recover.

### Eligibility

A branch is eligible when:

- `branches.is_active = 1`;
- deterministic `context_summary_json` exists;
- deterministic `summary_version = SUMMARY_VERSION`;
- `exchange_count >= llm_summary_min_exchanges`, unless `--force` is passed;
- enrichment is missing, stale by source fingerprint, currently recoverable from a non-`ok` status, or `--force` is passed. Recovery checks are status-specific: for example, `unsupported_cli` becomes recoverable when the capability sidecar is valid for the current Claude version, `budget_exceeded` becomes recoverable only with `--force`, and source-related statuses become recoverable when source validation/fidelity now passes after re-import;
- a transcript source path can be found from `import_log.file_path` for the session UUID or from current-session lookup;
- transcript path exists, passes source validation, and for historical runs still matches the imported source fingerprint/stat record.

### Rendering

Add a render helper that composes enrichment with deterministic markdown, for example:

```python
def render_enriched_context_summary(
    base_markdown: str,
    enrichment: dict | None,
    *,
    is_primary_session: bool,
    status: str | None,
    stored_source_hash: str | None,
    current_source_hash: str | None,
    stored_enrichment_version: int | None,
) -> str:
    if not valid_current_enrichment(
        enrichment,
        status=status,
        stored_source_hash=stored_source_hash,
        current_source_hash=current_source_hash,
        stored_enrichment_version=stored_enrichment_version,
    ):
        return base_markdown
    char_budget = 2400 if is_primary_session else 800
    return render_llm_block(enrichment, char_budget=char_budget) + "\n\n" + base_markdown
```

The LLM block is a bounded branch resume brief:

```markdown
### Branch Resume Brief

**Title:** <title.text>

**Where we left off:** <latest concrete state>

**How we got here:** <short causal history>

**Key decisions:** ...
**Attempted paths:** ...
**Open questions:** ...
**Files touched:** ...
**Continuation hints:** ...
```

The primary selected session is `sessions[0]`, which is also the Session Origin session. Its renderer applies the 2,400-character budget from the enrichment contract. It renders sections in the listed order and omits lower-priority sections when needed; it never truncates an item in a way that changes its meaning. Supplementary selected sessions use the 800-character title/latest-state-focused layout. `files_and_reasons` is rendered as the compact `Files touched` line when present, and is omitted before current state, continuation hints, causal history, decisions, attempted paths, or open questions.

`context_rendering.py` should use the lightweight `summary_enrichment.py` helper when selected session rows include valid enrichment. It passes `is_primary_session=(i == 0)` while iterating selected sessions, so the Session Origin session receives the primary budget and every later selected session receives the supplementary budget. Compose the LLM block with the cached deterministic `context_summary` markdown that `session_selection.py` already returns. Extend `session_selection.py` to select enrichment columns plus the stored `summary_source_hash`; do not require `context_summary_json` on the SessionStart path solely for rendering. Do not require `context_summary` to be overwritten with LLM content.

`search_hydrate.py` should select enrichment columns plus the stored `summary_source_hash`, use the lightweight `summary_enrichment.py` validity helper, and keep the existing `topic` field semantically unchanged for JSON compatibility. When enrichment is valid, add `display_title` from `summary_enrichment_json.title.text` and `summary_preview` from `summary_enrichment_json.where_we_left_off.text` as additive hydrated result fields. The card formatting boundary (`formatting.py`, called by `search_cli.py`) should prefer `display_title` over `topic` for human-readable output, render `summary_preview` as a second human-readable line, and include both as additive JSON fields. Existing JSON consumers that read `topic` continue to receive the deterministic topic.

Search ranking must not change in this version. Do not add enrichment text to `aggregated_content`, FTS triggers, chunks, or vector search.

## Implementation Preferences

- Use stdlib `subprocess.run` with an argument list and timeout.
- Use Pydantic or explicit Python validation at the LLM boundary; do not trust Claude output because `--json-schema` is an external CLI contract.
- Keep the LLM boundary in a new module rather than growing `summarizer.py`.
- Keep worker code synchronous to match existing SQLite/background worker style.
- Use `whenever` for new date/time values if adding timestamp formatting or parsing beyond SQLite `CURRENT_TIMESTAMP`.
- Use cyclopts for new CLI command registration in `src/ccrecall/cli/commands.py`.

## Replacement Targets

No existing code is being replaced. The deterministic middle-gap summary remains the fallback and control path.

## Migration

Add enrichment columns to `branches` via a new additive migration and bump `SCHEMA_VERSION`. Follow the existing additive migration style in `src/ccrecall/db.py`: tolerate duplicate-column races and ensure the columns exist even when a DB's `user_version` is already ahead of this code branch.

Update `SCHEMA_CORE` in `src/ccrecall/schema.py` so fresh installs get the enrichment columns immediately. Also update every existing `branches` table rebuild migration that creates or copies an explicit branches shape. In this codebase that means at least `_migrate_to_v1()` and `_migrate_to_v2()` in `src/ccrecall/db.py`: fresh installs start with `user_version = 0`, run the baseline schema, then still pass through those rebuild migrations. If the new columns are absent from the rebuild table definitions or copy lists, fresh installs can fail or silently drop the new columns during rebuilds.

Do not keep `_migrate_to_v1()` as `INSERT INTO branches_new SELECT * FROM branches` after adding enrichment columns. A real pre-v1 upgrade DB will not have those new columns, so `SELECT *` would no longer match the rebuilt table. Rewrite the copy as an explicit column projection that lists old columns and supplies defaults or NULLs for new enrichment columns. Apply the same explicit-column discipline to `_migrate_to_v2()` and any future branches rebuild.

Add an index on `summary_enrichment_version` only if selection/status queries need it; avoid adding indexes until the query shape is known.

Existing rows start with no enrichment and render deterministic summaries. No data rewrite is required.

## Convention Examples

### Detached hook helper pattern

**Source:** `src/ccrecall/hooks/memory_sync.py`

```python
kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
if sys.platform == "win32":
    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
else:
    kwargs["start_new_session"] = True
subprocess.Popen(
    ["ccrecall", "sync-current", "--input-file", tmp_path],
    **kwargs,
)
```

Use this shape for current-session enrichment spawning. Do not run Claude directly in the hook entry point.

### Summary backfill batch pattern

**Source:** `src/ccrecall/hooks/backfill_summaries.py`

```python
while True:
    cursor.execute(
        """
        SELECT id FROM branches
        WHERE summary_version IS NULL
           OR (summary_version < ? AND summary_version != ?)
        LIMIT ?
    """,
        (SUMMARY_VERSION, CONTENT_ERROR_VERSION, BATCH_SIZE),
    )
    rows = cursor.fetchall()
    if not rows:
        break
```

Use an analogous selector for enrichment eligibility, but do not mark deterministic `summary_version` on LLM failures.

### Settings merge pattern

**Source:** `src/ccrecall/config.py`

```python
DEFAULT_SETTINGS = {
    "auto_inject_context": True,
    "max_context_sessions": 2,
    "exclude_projects": [],
    "logging_enabled": True,
    "log_level": "INFO",
    "alert_snooze_hours": 24,
}
```

Add LLM settings here so `load_settings()` can merge user config without a separate config loader.

### Deterministic render contract

**Source:** `src/ccrecall/summarizer.py`

```python
summary_json = build_context_summary_json(branch_row, messages)
summary_md = render_context_summary(summary_json)
json_str = json.dumps(summary_json, ensure_ascii=False)
```

Keep this path deterministic. Enrichment composition should wrap this output, not replace how it is computed.

## Alternatives Considered

- **Replace deterministic summaries with LLM summaries:** rejected because it makes failures user-visible and weakens the hook-safe fallback.
- **Store LLM fields inside `context_summary_json`:** rejected for v1 because deterministic summary versioning would become ambiguous and deterministic refreshes could accidentally wipe or stale LLM fields.
- **Use Python Agent SDK:** deferred because it adds a dependency and is less clearly aligned with subscription/OAuth-based Claude Code auth.
- **Use `--bare`:** rejected as the default because installed help says it skips OAuth/keychain auth.
- **Use deterministic-only improvements:** useful as a future fallback improvement, but it does not test the main hypothesis that Claude can produce better recall UX.

## Test Strategy

### Required Test Types

Unit tests are required for schema validation, rendering composition and budgets, prompt construction, packet-outline construction, and status classification. Integration-style tests with mocked subprocess and temporary SQLite DBs are required for worker selection, migration, and CLI command behavior. Hook contract tests are required because current-session spawning touches hook-adjacent code. A manually reviewed evaluation corpus is required to assess output usefulness; unit tests cannot establish the factual quality of non-deterministic LLM output.

### Existing Tests to Adapt

- `tests/test_summarizer.py`: add enriched render tests while preserving deterministic render expectations.
- `tests/test_context_injection.py`: selected sessions with valid enrichment render an LLM block plus deterministic context; missing/stale enrichment renders deterministic-only context.
- `tests/test_search.py`: search cards prefer valid branch-resume title/latest-state preview for display.
- `tests/test_sync_hook.py`: current-session path can spawn enrichment without extra stdout and without importing heavy LLM/SDK dependencies into hook entry points.
- `tests/test_db.py`: migration idempotence and import-safety checks for new columns.

### New Test Coverage

- LLM response-body validator accepts a valid schema and rejects invalid/malicious/oversized outputs, including titles without valid active-branch message UUID citations and worker-owned envelope fields. Worker tests prove it adds version, configured model, and generation timestamp after validation. Covers FR#3, FR#16.
- Claude subprocess wrapper classifies missing binary, timeout, nonzero exit, invalid JSON, schema failure, and success. Covers FR#7, FR#8, FR#10.
- Invocation tests pass the configured `llm_summary_model` and `llm_summary_max_budget_usd` unchanged, including the `$1.00` default. Covers FR#18.
- Worker eligibility uses deterministic summary version, exchange threshold, transcript path existence, source containment, source fingerprint, retry status, and force behavior. Covers FR#2, FR#5, FR#9, FR#12, FR#13.
- Worker write tests prove stale post-Claude results are discarded when `summary_source_hash` changed while Claude was running. Covers FR#9, FR#12.
- Lightweight enrichment helper tests cover stored-hash validity checks and prove it imports without `db.py`, subprocess, sqlite-vec, or embedding dependencies. Covers FR#4, FR#12.
- Current-session spawn happens only when config is enabled and new messages were synced. Covers FR#6, FR#11.
- Rendering and search display degrade to deterministic fallback. Covers FR#1, FR#4.
- Rendering tests enforce the 2,400-character primary-selected-session and 800-character supplementary-session budgets, retain an evidenced continuation hint when one is present, and retain deterministic context below the resume brief. Covers FR#15, FR#17.
- Packet tests prove the transcript projection carries user requests, assistant prose, tool invocations, and result/error evidence when present, and that `branch-outline.json` preserves stable exchange order and UUID locators. Covers FR#15, FR#16.
- The manually reviewed evaluation corpus records source UUIDs for known latest-state, causal-history, decision-rationale, attempted-path, and unresolved-work facts. Review each generated brief for coverage, unsupported claims, and whether every citation entails the claim it accompanies before accepting a prompt/schema change. Covers AC#9.

### Tests to Remove

No tests to remove.

## Documentation Updates

- Update README or configuration docs with `llm_summaries_enabled` and related settings.
- Add CLI help text for `ccrecall backfill llm-summaries` explaining that branch-scoped transcript packet content, branch/session metadata, and source-path provenance are sent through Claude Code auth.
- Document the configurable `llm_summary_model` (default `sonnet`) and `llm_summary_max_budget_usd` (default `$1.00`), including that the latter is an upstream stop threshold rather than a guaranteed cost ceiling.
- README is the v1 plugin/user-facing documentation surface for SessionStart enrichment; no skill file changes are needed because this release adds no new user-invoked skill.
- Consider `ccrecall status` additions in a follow-up; do not require status surfacing for v1 unless implementation naturally exposes it.

## Impact

### Changed Files

<!-- Gap check 2026-08-07: 1 gap included — README.md (user-facing config/privacy contract) → T08. -->

- Modify `src/ccrecall/schema.py`: add enrichment columns to `SCHEMA_CORE`.
- Modify `src/ccrecall/db.py`: bump schema version, add additive migration, and update existing branches-table rebuild migrations so fresh installs preserve the new columns.
- Modify `src/ccrecall/config.py`: add opt-in LLM summary settings.
- Modify `src/ccrecall/branch_ops.py` and `src/ccrecall/embed_ops.py:write_branch_summary()`: invalidate/populate `summary_source_hash` when branch summary inputs change.
- Modify `src/ccrecall/hooks/backfill_summaries.py`: populate `summary_source_hash` during deterministic summary backfill.
- Modify `src/ccrecall/hooks/backfill_tool_content.py`: invalidate `summary_source_hash` and enrichment freshness wherever it rebuilds `aggregated_content` or resets `summary_version`.
- Create `src/ccrecall/llm_summary_db.py`: embedding-free DB connection and migration boundary for the direct LLM worker entry point.
- Create `src/ccrecall/summary_enrichment.py`: lightweight status constants, validation, stored-hash validity checks, and budgeted branch-resume rendering.
- Create `src/ccrecall/llm_summarizer.py`: explicit branch-packet and outline construction, summarizer prompt definition, subprocess wrapper, and error classification.
- Create `src/ccrecall/hooks/backfill_llm_summaries.py`: manual/current-session worker orchestration.
- Add stale packet reaping to the LLM summary worker before packet creation.
- Modify `src/ccrecall/cli/commands.py`: register `ccrecall backfill llm-summaries`.
- Modify `pyproject.toml`: add direct `ccrecall-llm-summaries` console script that imports the LLM worker without loading the full CLI command graph.
- Modify `src/ccrecall/hooks/sync_current.py`: after successful sync and closed DB connection, optionally spawn current-session enrichment.
- Modify `src/ccrecall/hooks/session_selection.py`: select enrichment columns and stored `summary_source_hash` for runtime composition with cached deterministic markdown.
- Modify `src/ccrecall/hooks/context_rendering.py`: render valid enrichment above deterministic context.
- Modify `src/ccrecall/search_hydrate.py`: keep deterministic `topic`, add `display_title` and `summary_preview` when enrichment is valid.
- Modify `src/ccrecall/search_cli.py` and `src/ccrecall/formatting.py`: prefer `display_title` in human-readable output and include `display_title`/`summary_preview` as additive JSON fields where card formatting is centralized.
- Add or modify tests listed in the Test Strategy.

### Behavioral Invariants

- Hook stdout remains `{"continue": true}` / `{}` only as currently required.
- `memory_sync.py` remains a small spawner and does not import DB, Claude, SDK, vec, or embedding code.
- `memory_context.py` does not probe Claude availability or load new heavy dependencies.
- Deterministic summary fields remain valid and sufficient for all existing recall/search behavior.
- Search ranking and vector embeddings are unchanged.
- Existing DBs keep all synced history; migration is additive.

### Blast Radius

The main blast radius is summary display and branch-row schema. The riskiest runtime path is current-session spawn from `sync_current.py`, but it remains detached from inline hook stdout and guarded by config and PID files. Search display changes are low-risk if ranking remains untouched. Schema changes affect every DB connection because migrations run through `get_connection()`.

## Open Questions

None.
