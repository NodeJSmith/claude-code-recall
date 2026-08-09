# Research Brief: Richer Session Summaries via Claude Code, Async with Deterministic Fallback

## Summary

Issue 32 is feasible, but the safest path is **not** to replace the deterministic summary path inline. The current system already has the right shape for this: summaries are cached on `branches`, stale summaries are detected by `summary_version`, and summary backfill already runs off the SessionStart hot path. The main change should be a new async enrichment path that writes richer LLM-derived fields while preserving the existing deterministic `context_summary` / `context_summary_json` contract for context injection and search cards.

Recommended validation path: prototype a small, PID-guarded async worker that selects long/current-version deterministic summaries, calls `claude -p` with a strict JSON schema and timeout, stores LLM output in a versioned extension field, and falls back to deterministic rendering on every error. Do this before committing to the Python Agent SDK, because the SDK adds a new dependency and its docs emphasize API-key auth for third-party integrations, while `claude -p` aligns better with the user's existing Claude Code login.

CodeGraph was requested first, but this worktree has no `.codegraph/` index, so findings below come from direct file reads plus targeted grep and focused Anthropic docs.

## Current Architecture

### Summary generation

- `src/ccrecall/summarizer.py` owns the current deterministic summary path.
  - `SUMMARY_VERSION = 6` gates staleness.
  - `build_exchange_pairs()` groups each user turn with following assistant turns and includes `tool_content`, with repeated tool markers collapsed.
  - `build_context_summary_json()` stores topic, first 2 exchanges, last 6 exchanges, and metadata.
  - `render_context_summary()` renders markdown for SessionStart injection.
  - `compute_context_summary()` fetches branch metadata/messages from SQLite and returns `(markdown, json_string)`.
- The current summary is endpoint-focused by design: short sessions render all exchanges; long sessions render "Where We Left Off", a middle gap based mostly on file basenames, and "Earlier in This Session".

### Storage and backfill

- `branches` has `context_summary TEXT`, `context_summary_json TEXT`, and `summary_version INTEGER DEFAULT 0` in `src/ccrecall/schema.py`.
- `src/ccrecall/embed_ops.py:write_branch_summary()` computes and writes deterministic summary fields during sync/import.
- `src/ccrecall/hooks/backfill_summaries.py` selects branches where `summary_version IS NULL OR summary_version < SUMMARY_VERSION`, skips `CONTENT_ERROR_VERSION = -1`, and marks content errors as `-1` to avoid infinite retry.
- `src/ccrecall/hooks/memory_setup.py` checks `_needs_backfill()` on SessionStart and spawns `ccrecall backfill summaries` in the background under a PID guard.

### Sync and hook hot path

- `src/ccrecall/hooks/memory_sync.py` is the actual Stop hook. It only writes hook input to a temp file, spawns `ccrecall sync-current --input-file ...`, and prints `{"continue": true}`.
- `src/ccrecall/hooks/sync_current.py` is detached from the inline Stop hook, PID-guarded, and performs current-session sync, summary writing, and bounded embedding work.
- `CLAUDE.md` and `pyproject.toml` both make hook performance constraints explicit: direct hook entry points avoid eager importing the heavy CLI surface; hook stdout must remain JSON only.
- Therefore, LLM summarization must not run in `memory_sync.py` or `memory_context.py`; it can run in detached `sync-current`, a new detached worker, or a manually invoked backfill.

### Context rendering and search hydration

- `src/ccrecall/hooks/session_selection.py` selects prior sessions and reads only `b.context_summary`; uncached sessions load messages for fallback rendering.
- `src/ccrecall/hooks/context_rendering.py` injects cached `context_summary` verbatim, or uses `_build_fallback_context()` through the deterministic summary builder.
- `src/ccrecall/search_hydrate.py` reads `context_summary_json` only for the `topic` on session cards; if absent, it falls back to the first user message.
- Search ranking currently does **not** embed or FTS-index the rendered summary. FTS uses `aggregated_content` from raw message text, file paths, commits, and tool content (`src/ccrecall/parsing.py`). Vector search embeds per-exchange chunks from raw exchange text (`src/ccrecall/embed_ops.py`, `src/ccrecall/search_vector.py`). Richer summaries would most directly improve context injection and search-card UX unless explicitly wired into indexing/embedding.

## Options With Pros/Cons

### Option A — `claude -p` async enrichment worker, deterministic fallback

How it works: Add a new off-hot-path summarization worker that selects eligible branches, builds a bounded transcript/metadata prompt, calls `claude -p --output-format json` (ideally with `--json-schema`), validates the result, and stores richer summary data. Keep deterministic summary generation as the baseline and render deterministic output whenever the LLM call is unavailable, slow, invalid, or disabled.

Pros:
- Best fit for the user's preferred auth model: `claude -p` uses the existing Claude Code installation and subscription login in normal mode. Anthropic docs describe it as the subprocess route for languages/tools that want Claude Code behavior.
- No new Python runtime dependency.
- Natural isolation boundary: subprocess timeout, stdout/stderr capture, exit-code handling, and PID-file concurrency are already familiar patterns in `memory_setup.py` / `memory_sync.py`.
- Can be implemented without touching the inline Stop hook.
- `--output-format json` / `--json-schema` gives a testable output contract.

Cons:
- Startup latency may be significant; must be measured on representative long sessions.
- Normal `claude -p` loads user/project Claude Code context unless constrained. `--bare` starts faster and is more deterministic but docs say bare mode does not read OAuth credentials and expects API-key-style auth, which conflicts with the stated auth preference.
- Subprocess behavior and output fields are an external CLI contract; failures include missing `claude`, not logged in, billing/rate limits, invalid flags, stderr warnings, malformed JSON, and timeouts.
- Care is needed to avoid recursive hook/plugin behavior if the summarizer runs inside a repo with ccrecall hooks enabled.

Effort: Medium. Most work is worker orchestration, schema/config/test coverage, and prompt/output validation rather than core data plumbing.

### Option B — Python Agent SDK worker

How it works: Add `claude-agent-sdk` as an optional dependency or extra, call `query()` from a background summarizer, and parse typed message/result objects. Use options to limit tools, set cwd, bound turns/cost, and request structured output.

Pros:
- More programmatic control than CLI: async iterator, typed-ish messages, options such as `max_turns`, `max_budget_usd`, `cwd`, `cli_path`, `disallowed_tools`, and output format.
- Easier to integrate with Python tests by mocking the SDK boundary.
- Avoids shell/subprocess JSON parsing ergonomics if adopted fully.

Cons:
- Adds a new dependency to a package that currently keeps dependencies tightly pinned and native-heavy dependencies explicit in `pyproject.toml`.
- Anthropic's Agent SDK overview includes a note that third-party developers should use API-key auth methods unless previously approved for claude.ai login/rate limits. That makes it less clearly aligned with "user's existing Claude Code auth" than `claude -p`.
- The SDK still communicates with a Claude Code process by default and has its own version/API churn risk.
- Using async SDK code may push more structural changes into a codebase that is otherwise mostly synchronous around SQLite workers.

Effort: Medium-to-large if made optional and robust; medium if accepted as a hard dependency.

### Option C — Hybrid deterministic + LLM fields in existing summary shape

How it works: Keep the deterministic metadata/exchange layout, but let an async LLM fill extra fields such as `llm_title`, `llm_summary`, `key_decisions`, `open_questions`, `files_and_reasons`, and `continuation_hints`. Render those fields above the existing deterministic "Where We Left Off" block when present.

Pros:
- Low compatibility risk: existing consumers still find `topic`, `first_exchanges`, `last_exchanges`, and `metadata`.
- Human UX improves even if LLM data is partial.
- Search cards can switch topic/title preference with a small change in `search_hydrate.py`.
- Easy fallback story: if no LLM field, render current deterministic summary exactly.

Cons:
- Mixed summary semantics need a schema/version story; `summary_version` currently represents deterministic summary logic only.
- If richer content is stored only inside `context_summary_json`, old renderers will ignore it until `render_context_summary()` changes.
- The LLM-generated summary can drift from deterministic raw transcript facts; validation and source-preserving metadata matter.

Effort: Medium; this is the recommended product shape for Option A's prototype.

### Option D — Deterministic-only improvement / lower-risk baseline

How it works: Improve `_build_gap_summary()` and rendering without LLMs: derive middle-session signals from tool content, files, commits, assistant/user keywords, pending questions, and maybe a compact chronological outline.

Pros:
- No auth, cost, latency, privacy, or dependency risks.
- Fully testable and reproducible.
- Can improve middle-session recall context where the current summary is weakest.

Cons:
- Likely ceiling is lower for "what happened and why" than LLM-generated summaries.
- Could become a pile of heuristics in `summarizer.py` unless carefully decomposed.
- Does not test the core hypothesis that Claude can produce materially better recall/search UX.

Effort: Small-to-medium. Good fallback and control option even if LLM summaries proceed.

## Risks and Unknowns

### Hook/path risks

- Inline hooks must keep stdout JSON-only and stay fast. LLM calls must not run in `memory_sync.py`, `memory_context.py`, or setup logic before printing hook output.
- `sync_current.py` is detached, but it already does DB work and bounded embeddings. Adding unbounded Claude calls there could make Stop recovery less predictable. A separate worker is cleaner than lengthening sync-current.
- PID semantics should be skip-not-queue, matching existing sync/backfill behavior.

### Schema compatibility

- Reusing `context_summary_json` for richer fields is backward-compatible if old keys remain, but it muddies `SUMMARY_VERSION` unless the LLM fields have their own version/status.
- A new column set such as `llm_summary_json`, `llm_summary_version`, `llm_summary_status`, `llm_summary_error`, `llm_summary_updated_at` is clearer but requires `SCHEMA_VERSION` migration and fixture updates.
- A middle path is adding optional fields under `context_summary_json["llm"]` plus a separate `summary_enrichment_version`; this avoids a second large JSON column but still needs status tracking if retries/backfill are expected.

### Failure modes

- `claude` binary missing or too old for needed flags.
- User not authenticated, billing/rate limit, subscription unavailable, org policy restrictions.
- JSON/schema failure despite prompt instructions.
- Timeouts or long-running background agent/subagent waits.
- Prompt input too large. Docs state piped stdin is capped at 10 MB; prompts should use a bounded transcript excerpt or temp file with explicit caps.
- DB contention if a summarizer holds a write transaction while waiting for Claude. The worker should read input, release the DB, call Claude, then reopen/write briefly.

### Privacy/user consent

- Transcript content is user-local and potentially sensitive. Calling Claude Code sends selected transcript content to Anthropic/provider under the user's Claude Code auth. This is a meaningful change from deterministic local summaries.
- The feature should be opt-in or at least controlled by explicit config, with clear naming such as `llm_summaries_enabled` and docs explaining what content is sent.
- Avoid writing raw prompts/responses to logs. Store only status/error categories and short diagnostics.

### Search-quality unknown

- Rich summaries improve SessionStart context directly, but search relevance won't improve unless the richer text is added to FTS content and/or embedded as chunks. That is a separate retrieval design decision.
- It is unknown whether LLM summaries should be embedded as branch-level/session chunks, included in `aggregated_content`, or only displayed. A prototype should measure UX before changing retrieval.

### External docs uncertainty

- Anthropic docs confirm `claude -p`, `--output-format json`, `--json-schema`, and Python Agent SDK APIs. Exact behavior under subscription login, bare mode, plugins/hooks, and current local Claude Code version should be verified on the target machine; docs may lead or lag installed versions.

## Recommended Path

1. **Prototype Option A + C:** a separate async enrichment worker using `claude -p`, deterministic fallback, strict timeout, strict output schema, and explicit opt-in config.
2. **Do not replace deterministic summaries.** Preserve `context_summary` and current `context_summary_json` keys. Render LLM enrichments only when present and valid.
3. **Start with long sessions only.** Gate by exchange count and maybe file/tool activity so the worker targets sessions where endpoint summaries lose the most information.
4. **Keep search indexing unchanged at first.** Use LLM summaries for context injection/search-card display first; only add retrieval use after measuring whether summaries are reliable and helpful.
5. **Defer Agent SDK adoption.** Revisit if the CLI subprocess prototype hits severe parsing/control limits or if API-key auth becomes acceptable.

## Validation / Testing

- Unit tests for LLM result validation: valid schema, missing fields, oversized fields, invalid JSON, refusal/error text, and deterministic fallback.
- Worker tests with mocked subprocess: success, nonzero exit, timeout, missing binary, malformed stdout, stderr warning, and PID skip.
- DB tests for migration/idempotence and status retry rules.
- Rendering tests in `tests/test_context_injection.py` / `tests/test_summarizer.py`: LLM block appears when present; deterministic block remains; no LLM data preserves current snapshots/expectations.
- Search tests in `tests/test_search.py` if card topic/title preference changes.
- Hook contract tests: `memory_sync.py`, `memory_context.py`, and `memory_setup.py` still print only JSON envelopes and do not import new heavy SDK dependencies.
- Manual benchmark: compare deterministic summary vs `claude -p` latency/cost on short, medium, and long real sessions; record timeout choice and suggested exchange-count threshold.

## Implementation Notes

- Add a boundary module rather than growing `summarizer.py`: e.g. `llm_summarizer.py` for prompt/schema/validation and `hooks/backfill_llm_summaries.py` or `hooks/enrich_summaries.py` for worker orchestration.
- Build prompts from `fetch_branch_messages()` / summary JSON with explicit caps. Do not feed unbounded transcript text.
- Never hold a DB transaction while `claude -p` is running.
- Prefer storing deterministic and LLM versions separately: deterministic `SUMMARY_VERSION` should remain about deterministic render compatibility; LLM enrichment needs its own version/status.
- Use `config.DEFAULT_SETTINGS` for opt-in settings and `load_settings()` merge behavior.
- Use `config.try_acquire_pid_file()` / `remove_pid_file()` for the worker.
- If using `claude -p`, prefer argument lists, not shell strings; pass prompt via stdin or temp file; capture stdout/stderr; enforce timeout; parse JSON; validate length and required fields.
- Consider a status/caveat surface later, analogous to embedding health, but do not probe Claude availability from the hook hot path.

## Sources

- Code: `src/ccrecall/summarizer.py`, `embed_ops.py`, `branch_ops.py`, `hooks/backfill_summaries.py`, `hooks/sync_current.py`, `hooks/memory_sync.py`, `hooks/memory_setup.py`, `hooks/context_rendering.py`, `hooks/session_selection.py`, `search_hydrate.py`, `schema.py`, `db.py`, `parsing.py`.
- Tests: `tests/test_summarizer.py`, `tests/test_context_injection.py`, `tests/test_search.py`, `tests/test_sync_hook.py`.
- Docs: Anthropic Claude Code CLI reference, headless/`claude -p` docs, and Python Agent SDK reference fetched from `docs.anthropic.com`.
