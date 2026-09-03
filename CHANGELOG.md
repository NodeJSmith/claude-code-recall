# Changelog

## [0.24.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.24.0...v0.24.1) (2026-09-03)

### Hooks

* fix the SessionStart hook loading the full embedding stack (numpy/fastembed/onnxruntime) just to check background-job status, adding unnecessary startup latency (#200)

### Backfill

* fix summary backfill getting stuck retrying the same bad conversation branch forever instead of skipping it (#202)
* fix embeddings backfill getting stuck on one bad conversation branch, blocking every branch behind it from ever being embedded (#204)

### Bug Fixes

* fix a crash in task/teammate-notification detection when a transcript message's `text` field was present but null (#198)
* fix a false-positive "unresolved question" alert triggered by a system-generated notice that happened to quote a command tag (#198)
* fix import silently reporting success while a disk or database error caused it to skip every remaining file in a project (#198)
* fix a race between a manual backfill and the automatic sync that could hide a real "embeddings unavailable" alert (#198)
* correct PyPI package metadata to list only the platforms ccrecall actually supports (Linux, macOS) (#198)

## [0.24.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.23.0...v0.24.0) (2026-09-03)

### Search

* `search` and `search-messages` now accept the query positionally (`ccrecall search "auth bug"`), alongside the existing `--query`/`-q`; `--max-results` can also go up to 20 instead of 10 (#191)

### Sync

* fix an import crash where one malformed transcript file could permanently block every transcript sorting after it from ever being imported again (#194)
* fix crashes on transcripts containing a null message or a null text field, instead of producing garbage output (#193)
* fix conversation branch links silently failing to write, which could lose part of a conversation's history with no error (#188)

### Hooks

* fix the session-start hook crashing with no warning, instead of showing its usual alert, when `~/.ccrecall` is unwritable (#187)
* fix a stale "embeddings unavailable" alert getting cleared by an unrelated sync, hiding a real embedding-model problem (#186)
* fix the session-start summary sometimes showing the same exchanges twice, or a bogus "N earlier exchanges" gap (#189)
* fix the "unresolved decision" warning still appearing after you already moved on by running a slash command (#192)

### Performance Improvements

* sync no longer re-checks every already-synced message for missing tool content on every run, cutting redundant work on long sessions (#190)

### Bug Fixes

* fix `ccrecall recent --sort-order asc` returning the oldest sessions in the database instead of the most recent sessions in oldest-first order (#185)

## [0.23.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.22.1...v0.23.0) (2026-08-24)

### Breaking Changes

* the opt-in LLM summary enrichment subsystem is removed entirely: `ccrecall backfill llm-summaries`, the `ccrecall-llm-summaries` console script, and its six `llm_summary_*` config keys are gone, and `display_title`/`summary_preview` no longer appear in `ccrecall --json search`/`search-messages` output. Unknown keys left over in `config.json` are ignored, so stale config entries are harmless (#134)

### Bug Fixes

* surface previously-silent parsing, database, and hook failures to the per-process log files instead of failing silently (#154)
* fix a memory spike in sync-current that could freeze the machine (7.5 GB observed) by capping sync-path embedding at 4096 tokens, dropping the worst-case peak to ~4 GB (#166)
* fix `exclude_projects` failing to match hyphenated project names on the lossy project-name fallback path (#135)
* fix the next schema migration bricking every database that has ever produced embeddings, by loading the sqlite-vec extension before every migration instead of only the first one; also fix an infinite loop on a corrupted transcript, and a non-atomic handoff-file write that could lose the handoff on a fresh machine (#132)
* warn when a database's schema version is ahead of what the running code expects, instead of silently treating it as fully migrated (#167)

### Refactoring

* drop the redundant `--n` long-form alias from `search`/`search-messages`/`tail` (keep `-n`); rename `recent`'s `--n` to `--limit`; add `--verbose` to `search-messages` and `--db` to all `backfill` subcommands (#150)

## [0.22.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.22.0...v0.22.1) (2026-08-09)

### Bug Fixes

* fix false-positive capability check failures caused by unrelated, concurrent Claude Code sessions (#112)

## [0.22.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.21.1...v0.22.0) (2026-08-09)

### Features

* add opt-in Claude-powered Branch Resume Brief enrichment (#106)

## [0.21.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.21.0...v0.21.1) (2026-08-04)

### Bug Fixes

* recover scoped vector matches beyond the initial KNN window (#102)

### Performance Improvements

* cache confirmed-OK ingestion checks for faster repeated status audits (#105)

## 2026-08-24

- fix a memory spike in sync-current that could freeze the machine (7.5 GB observed) by capping sync-path embedding at 4096 tokens, dropping the worst-case peak to ~4 GB (#166)
- warn when a database's schema version is ahead of what the running code expects, instead of silently treating it as fully migrated (#167)

## 2026-08-19

- surface previously-silent parsing, database, and hook failures to the per-process log files instead of failing silently (#154)

## 2026-08-14

- fix a crash on upgrading pre-v4 databases caused by an index referencing a column added by a later migration (#136)

## 2026-08-09

- fix false-positive capability check failures caused by unrelated, concurrent Claude Code sessions (#112)

## 2026-08-08

- add opt-in Claude-powered Branch Resume Brief enrichment (#106)
- harden transcript discovery and capability-gated enrichment fallback (#106)

## 2026-08-04

- cache confirmed-OK ingestion checks for faster repeated status audits (#105)
- recover scoped vector matches beyond the initial KNN window (#102)

## [0.21.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.20.0...v0.21.0) (2026-08-03)

### Breaking Changes

* the deprecated `ccrecall stats` command is removed; its CLI wrapper never forwarded `--json`/`--days`/`--check-ingestion` (it always printed markdown regardless of the global `--json` flag), and `ccrecall status` already provides everything it did. Use `ccrecall status` instead (#97)

### Features

* add `--before`/`--after` date filters to `search`/`search-messages` (#99)

## [0.20.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.19.4...v0.20.0) (2026-07-30)

### Features

* add a consolidated ingestion status report (#93)

## [0.19.4](https://github.com/NodeJSmith/claude-code-recall/compare/v0.19.3...v0.19.4) (2026-07-28)

### Bug Fixes

* fix embedding inference memory usage that could kill the machine with OOM (#90)

## [0.19.3](https://github.com/NodeJSmith/claude-code-recall/compare/v0.19.2...v0.19.3) (2026-07-26)

### Bug Fixes

* fix `ccrecall tail` picking the wrong session in worktrees by falling back branch-aware (#88)

## [0.19.2](https://github.com/NodeJSmith/claude-code-recall/compare/v0.19.1...v0.19.2) (2026-07-24)

### Bug Fixes

* fix `ccrecall backfill tool-content` aborting the entire run on a transient database-lock error instead of retrying and skipping the stuck session; batch embedding inference for speed; make the backfill progress ETA reflect actual work remaining instead of branch count; and add a session-start alert when older sessions still need a tool-content backfill (#85)

## [0.19.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.19.0...v0.19.1) (2026-07-23)

### Bug Fixes

* fix pending-question detection producing false positives when Claude Code's answer-confirmation wording changed; now uses the tool result's own `is_error` field instead of matching text, and correctly treats a rejected or abandoned question as resolved once the user gives a new instruction (#79)

## [0.19.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.18.1...v0.19.0) (2026-07-23)

### Features

* make tool-use turns (not just prose) searchable via `search`/`search-messages`/`/ccr-recall` (#77)

## [0.18.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.18.0...v0.18.1) (2026-07-20)

### Bug Fixes

* fix transcript directory resolution to respect `CLAUDE_CONFIG_DIR` (#75)

## [0.18.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.17.1...v0.18.0) (2026-07-17)

### Features

* show tool call detail in `ccrecall tail` output instead of bare tool names (#73)

## [0.17.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.17.0...v0.17.1) (2026-07-14)

### Bug Fixes

* fix `ccrecall tail` selecting the wrong session by scoping selection to the current worktree's cwd (#71)

## [0.17.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.16.0...v0.17.0) (2026-07-13)

### Features

* add `-n`/`--n` aliases to `search`/`search-messages`, a `--lines` alias to `tail`, a `--full` flag on `tail` for untruncated output, structured JSON error envelopes across `tail`/`search`/`search-messages`/`recent`, JSON output keys documented in `--help`, and a `--progress-every` fix on `backfill embeddings` so small values actually fire (#69)

## [0.16.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.15.0...v0.16.0) (2026-07-11)

### Features

* `ccrecall tail` now falls back to a global search when the given selector isn't found in the current project (#64)

## [0.15.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.14.1...v0.15.0) (2026-07-10)

### Features

* fix import running out of memory and being slow on large transcript sets (#62)

## [0.14.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.14.0...v0.14.1) (2026-07-10)

### Bug Fixes

* fix `ccrecall import` crashing with `no such module: vec0` when re-importing a session that filters to zero messages but has existing embedded chunks from a prior run (#60)

## [0.14.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.13.4...v0.14.0) (2026-07-08)

### Features

* add session ID to supplementary context blocks (#57)

## [0.13.4](https://github.com/NodeJSmith/claude-code-recall/compare/v0.13.3...v0.13.4) (2026-07-06)

### Bug Fixes

* fix `ccrecall tail` ordering sessions by filesystem mtime instead of in-file event timestamps, which could surface the wrong prior session after a reboot (#56)

## [0.13.3](https://github.com/NodeJSmith/claude-code-recall/compare/v0.13.2...v0.13.3) (2026-06-30)

### Bug Fixes

* fix the clear-handoff hook not printing the required stdout envelope (#51)

## [0.13.2](https://github.com/NodeJSmith/claude-code-recall/compare/v0.13.1...v0.13.2) (2026-06-30)

### Bug Fixes

* fix import running out of memory on the first sync of a large project directory (#49)

## [0.13.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.13.0...v0.13.1) (2026-06-29)

### Bug Fixes

* fix incorrect "IN_PROGRESS" status labels; 57% of sessions were falsely labeled because the heuristic defaulted to in-progress for anything not matching a narrow completion pattern (#46)

## [0.13.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.12.0...v0.13.0) (2026-06-28)

### Features

* add proactive startup alerts and reactive search-result caveats for when embeddings or vector search are unhealthy (#44)
* show accurate per-branch embedding coverage in `ccrecall stats`/`--status` instead of an inflated estimate (#43)

### Bug Fixes

* fix embedding-complete tracking for branches with zero exchanges (#41)

## [0.12.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.11.1...v0.12.0) (2026-06-26)

### Features

* switch search to chunk-level embeddings (one vector per exchange, fused with keyword scoring) instead of one embedding per branch (#36)

### Bug Fixes

* fix token-cost analytics pricing Opus 4.7/4.8 at the wrong tier (#38)

## [0.11.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.11.0...v0.11.1) (2026-06-24)

### Documentation

* `/ccr-resume` now also detects an open question left in prose, not just a structured `AskUserQuestion`, and reinterprets its argument accordingly

## [0.11.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.10.0...v0.11.0) (2026-06-22)

### Features

* auto-migrate existing installs from `~/.claude-memory` to `~/.ccrecall` after the project rename (#25)

## 0.10.0 (2026-06-22)

Initial public release. ccrecall brings conversation history and semantic search to Claude Code, shipped both as a PyPI package (the `ccrecall` CLI plus hook entry points) and as a Claude Code plugin. Highlights:

- Per-session sync of transcripts to a local SQLite database.
- Start-of-session context injection summarizing your previous session.
- Fused keyword + vector search over past conversations, via `/ccr-recall`.
- Prior-session resume that recovers intent and unresolved decisions from the transcript tail, via `/ccr-resume`.
- Token-cost analytics with an interactive HTML dashboard, via `/ccr-tokens`.
