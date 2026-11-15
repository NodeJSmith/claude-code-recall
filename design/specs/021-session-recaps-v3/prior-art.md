# Prior Art: Session Recaps

**Date:** 2026-11-15
**Question:** How do comparable Claude Code history tools produce a short per-session summary: when is it generated, where is it cached, how do they handle staleness and failure, and does anyone reuse Claude Code's own `away_summary`?

## Findings

| Project | Generation trigger | Cache | Staleness | Failure handling |
|---|---|---|---|---|
| aichronicles (toabctl) | Explicit `aichronicles summarize --session` command, on demand | Stored in its SQLite store; "replays for free" | Not described | None described beyond the command failing |
| universal-session-viewer (tad-hq) | Go backend shells out to the Claude CLI | Analysis results cached | SHA-256 content hash invalidates the cache | Not described |
| engineering-notebook (prime-radiant-inc) | Explicit `summarize --all` / `--date` / `--project` batch command | SQLite | "summarize everything unsummarized" | None described; uses Claude Haiku via an API key |
| claude-code-log (daaain), PR #141 | None: renders the transcript's existing `away_summary` entries | n/a | n/a | n/a |
| hapi (tiann), PR #1089 | None: shows Claude Code's away recap in its chat view | n/a | n/a | n/a |

Claude Code itself (2.1.322, read from the unpacked CLI source) generates `away_summary` by forking the live session's prompt-cached context with one extra user prompt, one turn, tools denied, output capped at 400 characters, and appends it to the transcript as `{"type":"system","subtype":"away_summary","content":...}`. It fires only after a turn ends and the terminal has been out of focus for 3 minutes, needs at least 3 user prompts and 2 new ones since the last recap, and skips when the prompt cache has mostly expired, a draft is in the input box, background agents are pending, or the user is near a rate limit. A public feature request (anthropics/claude-code#61998) asked for recaps to be persisted to the JSONL for downstream parsing.

## What this means for the design

- Every peer that generates its own summary does it in an explicit batch or on-demand command, caches the result in its store, and carries no retry or backoff machinery. None runs generation on a hook path.
- Two peers reuse `away_summary` directly, so treating it as a first-class recap source has precedent.
- Content-hash or "unsummarized" selection is the common staleness model; none describes a debounce. Claude Code's own "at least 2 new prompts since the last recap" rule is the nearest precedent for not regenerating on every turn.
