# ccrecall

**Conversation history and semantic search for Claude Code.**

ccrecall stores your Claude Code sessions in a local SQLite database so you can recall past conversations, search across them by keyword and meaning, and get automatic context on session start. Everything runs on your machine — no data leaves it.

> ccrecall is an independent, community project for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). It is not affiliated with, endorsed by, or sponsored by Anthropic.

## What it does

Every time a Claude Code session ends, the conversation is synced to `~/.ccrecall/conversations.db`. On your next session start, Claude automatically gets a summary of what you were last working on. You can also search past sessions by keyword or pull recent ones at any time.

## Install

ccrecall has two parts: a **Python package** (the `ccrecall` CLI plus the hook binaries) and a **Claude Code plugin** (the `/ccr-*` skills and the hook wiring). Install both.

**1. Install the package** — puts `ccrecall` and the hook commands on your PATH:

```bash
uv tool install ccrecall
```

(`pipx install ccrecall` or `pip install ccrecall` work too.)

**2. Enable the plugin** — ccrecall ships as a Claude Code plugin. The repo doubles as a single-plugin marketplace, so from inside Claude Code:

```
/plugin marketplace add NodeJSmith/claude-code-recall
/plugin install ccrecall@claude-code-recall
```

That registers the skills and wires the SessionStart / Stop / SessionEnd hooks (`hooks/hooks.json`) — both are auto-discovered from the plugin's directory layout. Reload with `/reload-plugins` if they don't appear immediately.

> Plugin skills are namespaced under the plugin name, so the skills below are invoked as `/ccrecall:ccr-recall`, `/ccrecall:ccr-resume`, and `/ccrecall:ccr-tokens`. The hook commands degrade gracefully — each is guarded by `command -v … || true`, so if the package isn't installed (or isn't yet on PATH) the hook is a silent no-op rather than a broken session.

## First-run setup

On your first session after installing, Claude will notice that `~/.ccrecall/config.json` doesn't exist and walk you through a brief onboarding. It asks a single question — **session context injection**: should Claude automatically recall what you were working on last session?

Your choice gets written to `~/.ccrecall/config.json`. You can edit that file directly at any time to change settings.

To skip the walkthrough and use recommended defaults immediately:

```bash
ccrecall write-config --defaults
```

## Semantic search

Search results are fused from two signals: keyword full-text search (FTS5 → FTS4 → LIKE fallback) and vector similarity from a locally-running embedding model. The two ranked lists are merged with Reciprocal Rank Fusion (RRF), so results that rank well in both signals appear first.

The embedding model is [jina-embeddings-v2-small-en](https://huggingface.co/jinaai/jina-embeddings-v2-small-en) (512-dim), running entirely on your machine via [fastembed](https://github.com/qdrant/fastembed). No data leaves your machine.

### Coverage

New sessions are embedded automatically as they sync (embed-on-write), so coverage builds forward on its own. Only **active-leaf** branches are embedded — at most one active leaf per session (maintained by sync/import), not its abandoned forks/retries. The flag isn't DB-enforced, but sync/import marks exactly one branch `is_active=1` per session. The search path only ever returns active leaves, so embedding inactive forks would just produce vectors that can never surface.

### Optional: seed historical conversations

Embedding runs on CPU via fastembed. jina-v2-small-en is light — a few milliseconds for a short summary, up to ~400ms for a long one — but seeding a large history (~2k active leaves) is still a bounded chunk of work, and a parallel run can thrash a small or shared box. It is therefore **opt-in** — it is *not* auto-spawned on SessionStart — so it never fires unbidden. Run it yourself when you want to seed:

```bash
ccrecall backfill embeddings              # all active leaves, all history
ccrecall backfill embeddings --days 14    # only the last 14 days
ccrecall backfill embeddings --limit 500  # cap this run at 500 branches
ccrecall backfill embeddings --threads 4  # use 4 inference threads (idle machine)
```

It runs at low scheduling priority (`nice`) and a single inference thread by default so it yields to interactive work. Tune the thread count with `--threads` (e.g. `--threads 4` on an idle workstation to finish faster). Progress prints to stderr (one line per batch); the run is resumable — re-running skips already-embedded branches.

### Flags

| Flag | Effect |
|------|--------|
| `--keyword-only` | Skip the embedding step entirely, use keyword search only |
| `--status` | Print diagnostic info (vec extension loaded, model name, embedded vs. total summarized (embeddable) branch count) and exit 0 |

### Runtime deps

The semantic search path requires three extra packages beyond the base install:

- `sqlite-vec` — SQLite extension for vector KNN queries
- `fastembed` — downloads and runs the embedding model (manages onnxruntime + tokenization)
- `numpy` — vector math (normalization)

These are included in the package dependencies. If fastembed fails to import (e.g. ABI mismatch on an unusual platform), search falls back silently to keyword-only mode.

### Degradation

Semantic fusion is automatically disabled when:
- The embedding model can't be loaded (e.g. a first-run download failed and no cached copy exists)
- `fastembed` cannot be imported
- `sqlite-vec` cannot be loaded on the connection (e.g. Python built without loadable extensions)

In all cases, search falls back to keyword-only and returns results normally. When this happens (or when embedding coverage is still catching up below ~95%), a recall appends a one-line caveat to its own results so you know they may be partial — surfaced only when you actually run a recall, never as a standalone nag. Use `ccrecall search --status` to check which path is active.

## When ccrecall speaks up

ccrecall is meant to be invisible — it runs in the background and Claude consumes its output. It will interrupt you, once, only when something is actually broken and only you can fix it:

- **It can't save your history** — the data directory or database is unwritable (disk full, permissions, corruption). Left unsaid, a working install would silently stop recording sessions.
- **Embeddings are persistently failing** — the vector extension or embedding model is unavailable, so semantic search is degraded until you fix the environment.

When either condition holds, the next session injects a short alert for Claude to relay to you in plain language. It's **told once, then goes quiet for ~24h** (tunable via `alert_snooze_hours` in the config file) even if still broken, and **clears itself the moment the condition resolves**. Coverage that's merely catching up is never surfaced this way — only genuine, unrecoverable failures earn the interruption.

## Skills

| Skill | Trigger | What it does |
|---|---|---|
| `/ccr-recall` | "what did we discuss", "continue where we left off", "search my conversations" | Lets Claude search or browse your past sessions on demand |
| `/ccr-resume` | "pick up where we left off after /clear", a stop, or an unanswered question | Reconstructs the prior session's intent from its transcript tail and surfaces any unresolved decision |
| `/ccr-tokens` | "analyze Claude token usage", "how much am I spending on Claude" | Full cost + workflow analytics report with an interactive HTML dashboard |

## Entry points

### Hooks (run automatically — don't call these manually)

These are wired by the plugin's `hooks/hooks.json` and fire on their respective Claude Code events.

| Entry point | Event | What it does |
|---|---|---|
| `ccrecall-setup` | SessionStart | Creates `~/.ccrecall/` if needed, opens the DB to apply any pending migrations, then spawns `ccrecall import` and `ccrecall backfill summaries` as background processes |
| `ccrecall-onboarding` | SessionStart (startup only) | One-time first-run onboarding. Injects setup instructions into Claude's context if `config.json` is missing or onboarding hasn't been completed. Silent no-op after that |
| `ccrecall-context` | SessionStart (startup + clear) | Injects a summary of your most recent session into Claude's context so it knows what you were working on. On `/clear`, reads a handoff file to link directly to the session you just cleared from |
| `ccrecall-clear-handoff` | SessionEnd (clear only) | Writes a small handoff file so the next session start knows which session to link to after a `/clear`. Without this, context injection falls back to a "most recent session" heuristic |
| `ccrecall-sync` | Stop | Syncs the current session to the DB in a detached background process. Runs on every session end |

> These are kept as separate console scripts (rather than `ccrecall hook …` subcommands) on purpose: hooks fire on every session boundary, and a direct entry point avoids eagerly importing the full CLI command surface on the hot path.

### Internal helpers (spawned by hooks — don't call these manually)

| Entry point | What it does |
|---|---|
| `ccrecall sync-current` | Syncs a single session file to the DB. Called by `ccrecall-sync` with the session ID from stdin |
| `ccrecall import` | Full import of all JSONL files in `~/.claude/projects/`. Skips files that haven't changed since last import (file hash check). Run on first install and whenever new sessions need backfilling |
| `ccrecall backfill summaries` | Generates context summaries for any DB branches that don't have one yet. Runs in the background after `ccrecall-setup` |
| `ccrecall write-config` | Writes `~/.ccrecall/config.json`. Called by Claude during onboarding to persist your settings choices. You can also call it directly — run `ccrecall write-config --help` for flags |

### Skill CLIs (called from skill files — can also be used directly)

These are the `ccrecall` subcommands the `/ccr-*` skills invoke. You can run them from the terminal too.

| Entry point | What it does |
|---|---|
| `ccrecall recent` | Prints recent sessions from the DB in markdown (default) or JSON. Used by `/ccr-recall` |
| `ccrecall search` | Searches sessions by keyword fused with vector similarity (FTS5 → FTS4 → LIKE fallback, RRF-fused with jina embeddings when available). Used by `/ccr-recall` |
| `ccrecall tail` | Reads the tail of a prior session's transcript to recover the last instruction and any unanswered question. Used by `/ccr-resume` |
| `ccrecall backfill embeddings` | Opt-in seeding of embeddings for historical active-leaf branches (jina-v2-small-en via fastembed). Not auto-spawned. Supports `--days N` / `--limit N` / `--threads N`; throttled via `nice` + a single inference thread by default. Resumable |
| `ccrecall tokens` | Parses JSONL files for token usage analytics — cost, cache hits, model mix, skill/agent/hook patterns. Populates analytics tables and builds `~/.ccrecall/dashboard.html`. Used by `/ccr-tokens` |

## Data flow

```
Session ends
  └─ ccrecall-sync (Stop hook)
       └─ ccrecall sync-current (background)
            └─ writes to ~/.ccrecall/conversations.db
            └─ embeds the active leaf via jina if model available (drops silently on failure)

/clear (SessionEnd)
  └─ ccrecall-clear-handoff
       └─ writes a handoff file naming the session being cleared
            (so the next SessionStart links to it instead of guessing)

Session starts
  └─ ccrecall-setup (SessionStart)
  │    └─ ccrecall import (background, first run / new files)
  │         └─ embeds each new active leaf via jina if model available
  │    └─ ccrecall backfill summaries (background, if summaries missing)
  │    └─ (embedding backfill is NOT auto-spawned — opt-in via ccrecall backfill embeddings)
  ├─ ccrecall-onboarding (SessionStart, startup only — one-time)
  └─ ccrecall-context (SessionStart, startup + clear)
       └─ injects last session summary into Claude's context
```

## Config file

`~/.ccrecall/config.json` — written by `ccrecall write-config` during onboarding:

```json
{
  "onboarding_completed": true,
  "onboarding_version": 1,
  "auto_inject_context": true
}
```

Onboarding sets `auto_inject_context`. The remaining settings are tunable by editing `config.json` directly:

| Key | Type | Default | Effect |
|---|---|---|---|
| `auto_inject_context` | bool | `true` | Inject a summary of your previous session at session start. |
| `max_context_sessions` | int | `2` | How many recent sessions to include in that injected context. |
| `exclude_projects` | list[str] | `[]` | Project names to skip when **storing** conversations — excluded projects are not imported or synced. Matched against the project's directory name. This is write-side only: it prevents new data from being indexed; it does not remove or hide conversations already stored before the project was excluded. |
| `logging_enabled` | bool | `true` | Write hook diagnostics (including swallowed hook exceptions) to `~/.ccrecall/ccrecall.log`. Set to `false` to suppress the log. |
| `log_level` | str | `"INFO"` | Logging verbosity when `logging_enabled` is true. Accepts standard Python level names: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

## Database

`~/.ccrecall/conversations.db` — SQLite, WAL mode. Tables:

- `sessions` — one row per conversation session
- `branches` — one row per conversation branch (rewinding creates new branches)
- `messages` — all messages, stored once per session regardless of branch
- `branch_messages` — join table linking messages to branches
- `import_log` — tracks which JSONL files have been imported and their hashes
- `branch_vec` — vec0 virtual table (sqlite-vec) storing 512-dim jina embeddings for each branch, used for KNN search
- `token_snapshots`, `turns`, `turn_tool_calls`, `session_metrics` — analytics tables populated by `ccrecall tokens`

## Development

```bash
uv sync                       # install package + dev dependencies
uv run pytest                 # run the test suite
uvx prek run --all-files      # run the lint/format/type hooks
```

## License

[MIT](LICENSE)
