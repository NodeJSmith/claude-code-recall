# ccrecall

**Conversation history and semantic search for Claude Code.**

ccrecall stores your Claude Code sessions in a local SQLite database so you can recall past conversations, search across them by keyword and meaning, and get automatic context on session start. Storage, sync, summaries, and semantic search all run entirely on your machine — no data leaves it.

> ccrecall is an independent, community project for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). It is not affiliated with, endorsed by, or sponsored by Anthropic.

## What it does

Every time Claude finishes a response, the conversation is synced to `~/.ccrecall/conversations.db`. On your next session start, Claude automatically gets a summary of what you were last working on. You can also search past sessions by keyword or pull recent ones at any time.

## Install

**Supported platforms:** Linux (including WSL2) and macOS. Windows is not supported.

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

> Plugin skills are namespaced under the plugin name, so the skills below are invoked as `/ccrecall:ccr-recall` and `/ccrecall:ccr-resume`. The hook commands degrade gracefully — each is guarded by `command -v … || true`, so if the package isn't installed (or isn't yet on PATH) the hook is a silent no-op rather than a broken session.

## First-run setup

No manual setup is needed. ccrecall uses built-in defaults unless you create `~/.ccrecall/config.json` with overrides.

## Checking on ccrecall

- **`ccrecall status`** is the glance: a verdict (healthy, working on it, or needs attention) and a line of numbers, such as `7,514 sessions · 98% embedded · last sync 2 min ago`. It exits 0 for any verdict, and 1 only when the database is missing or unreadable.
- **`ccrecall doctor`** is the diagnosis. It reads without changing anything and lists every finding under what to do about it (needs action, working on it, nothing to do), each with how to fix it. `ccrecall doctor --list <finding-id>` lists the sessions behind a finding. It exits 0 when healthy or working on it, 1 when something needs attention, and 2 when the database can't be read at all, so scripts can gate on it. `doctor --list` exits 0 whatever the verdict.

Each checks every session against its transcript, so they take a few seconds, and longer on a large database that isn't in the OS cache.

Queued embedding work counts as overdue once no full backfill has succeeded in `backfill_stale_hours` and a waiting session ended longer ago than that. Set it a little above how often your backfill runs: with a nightly job, 36.

### JSON output

`status` and `doctor` both take the global `--json` flag and print the same shape: `verdict`, `findings` (each with `id`, `group`, `message`, `count`, `fix`, `listable`) and `summary` (`sessions`, `embedded_pct`, `last_sync`). These values are stable; scripts can match on them.

- **`verdict`**: `healthy`, `working`, or `needs-attention`. It is the worst `group` present.
- **`group`**: `action` (needs action), `working` (working on it), or `none` (nothing to do). A finding's `id` is stable, but its `group` can change with circumstances. For example, `embedding-backlog` is `working` while a backfill drains it and `action` once it is overdue, and `<kind>-failed` is `working` when the cause was a busy database.
- **`id`**: one of `no-database`, `schema-behind`, `schema-ahead`, `not-writable`, `embedding-unavailable`, `embedding-backlog`, `fixable-sessions`, `unfixable-sessions`, `sessions-in-progress`, `deleted-transcripts`, `draft-sessions`, `unembeddable-sessions`, `tool-content-pending`, `skipped-by-design`, `duplicate-active-branch`, `check-failed`, and per background run `<kind>-failed` (`import`, `repair`, `sync`, `backfill`), `<kind>-killed` (`import`, `sync`, `backfill`), and `<kind>-running` / `<kind>-stuck` (`import`, `backfill`). `check-failed` can appear more than once, one per check that raised.
- **`fix`**: a command, a sentence of instructions, or `null` when there's nothing to do. Don't execute it blindly.
- **`summary`**: `sessions`, `embedded_pct`, and `last_sync` are each `null` when unknown (for example, `sessions` before a pending schema upgrade has run). With no database, `sessions` is `0`.
- **`sessions`**: present only with `doctor --list <id>`, on that finding, when the finding is present and `listable`. A missing array means the finding isn't there or names no sessions.

## What runs when

Everything below runs in the background, started by a Claude Code hook, except the two backfills, which run only when you start them. Each background job holds a lock while it runs, so a second copy skips instead of running twice. Logs go to `~/.ccrecall/ccrecall-<process>.log`.

| Work | When it runs | How you'd know it failed |
|---|---|---|
| **Sync** the current session (`ccrecall sync-current`) | Every time Claude finishes a response (the Stop hook) | `doctor` reports `sync-failed` or `sync-killed`. Log: `ccrecall-sync.log` |
| **Embed** that session's exchanges | Right after its sync, in the same process. Long exchanges are embedded at draft quality (capped at 4,096 tokens). Skipped while an embedding backfill is running | A SessionStart alert if embedding keeps failing, and `doctor`'s `embedding-unavailable`. Skipped sessions show as `embedding-backlog` |
| **Import and repair** (`ccrecall import --repair-gaps`) | SessionStart, when there's no database yet or a session has synced since the last import, which in practice is most session starts. Runs at low priority. Never embeds | `doctor` reports `import-failed`, `repair-failed`, `import-killed` or `import-stuck`, and `fixable-sessions` for gaps it hasn't repaired. Log: `ccrecall-import.log` |
| **Summaries** (`ccrecall backfill summaries`) | SessionStart, when a session has no summary or an outdated one | Only its log, `ccrecall-backfill-summary.log` |
| **Model warm-up** (`ccrecall-warm-model`) | Every SessionStart. Downloads the embedding model the first time, then returns at once | Its log, `ccrecall-warm-model.log`. If the model stays unavailable, sync's embedding fails and raises the alert above |
| **Embedding backfill** (`ccrecall backfill embeddings`) | Only when you run it. It embeds imported history, which sync never embedded, and upgrades draft-quality vectors to full quality | `doctor` reports `backfill-failed`, `backfill-killed` or `backfill-stuck`, and `embedding-backlog` turns into needs-action once it's overdue (see `backfill_stale_hours`). Log: `ccrecall-backfill-embed.log` |
| **Tool-content backfill** (`ccrecall backfill tool-content`) | Only when you run it. A one-time pass over sessions synced before ccrecall indexed tool calls | A SessionStart alert while sessions are left, and `doctor`'s `tool-content-pending`. Log: `ccrecall-backfill-tool-content.log` |

`ccrecall backfill schedule write` doesn't schedule anything. It records that you have set up your own scheduled embedding backfill (cron, a systemd timer), which silences the draft-quality alert. `backfill schedule clear` removes that record and `backfill schedule status` shows it, along with how many draft-quality vectors remain.

## When ccrecall speaks up

ccrecall is meant to be invisible — it runs in the background and Claude consumes its output. It will interrupt you, once, only when something needs you:

- **It can't save your history** — the data directory or database is unwritable (disk full, permissions, corruption). Left unsaid, a working install would silently stop recording sessions.
- **Embeddings are persistently failing** — the vector extension or embedding model is unavailable, so semantic search is degraded until you fix the environment.
- **Your install is older than your database** — another, newer ccrecall has already migrated the database, so imports and sync may fail until you upgrade. `ccrecall` commands also print this as a one-line warning on stderr.
- **Older sessions' tool calls aren't searchable** — sessions synced before ccrecall indexed tool calls haven't been backfilled. Fix: `ccrecall backfill tool-content`.
- **Some conversations have draft-quality embeddings** — long exchanges embedded at sync time were capped at 4,096 tokens, so search can miss their later parts. Fix: `ccrecall backfill embeddings`. Once you run it on a schedule, `ccrecall backfill schedule write` silences this alert; `ccrecall backfill embeddings --dismiss` silences it without a schedule.

When any of these holds, the next session injects a short alert for Claude to relay to you in plain language. It's **told once, then goes quiet for ~24h** (tunable via `alert_snooze_hours` in the config file) even if still broken, and **clears itself the moment the condition resolves**. Sessions that are merely waiting to be embedded don't alert: a recall adds a one-line caveat to its results instead, and `ccrecall status` shows the coverage.

## What isn't stored

- **Excluded projects.** Projects listed in `exclude_projects` are never imported or synced. Conversations stored before you excluded a project stay stored.
- **Subagent transcripts.** Claude Code writes each subagent's conversation to its own file under `<session-id>/subagents/`, which ccrecall doesn't read. The parent session is stored, including the prompt it gave each subagent (as part of the tool call), but not the subagent's own turns.

### Deleted transcripts

Claude Code deletes old transcripts on its own schedule (its `cleanupPeriodDays` setting). Once a session is stored, deleting its transcript doesn't remove it from ccrecall: its messages stay stored and searchable, and `doctor` lists it under `deleted-transcripts` with nothing to do. What you lose is anything that reads the transcript itself: the session can't be re-imported or repaired, and `/ccr-resume` can't read its tail.

## Semantic search

Search results are fused from two signals: keyword full-text search (FTS5 → FTS4 → LIKE fallback) and vector similarity from a locally-running embedding model. The two ranked lists are merged with Reciprocal Rank Fusion (RRF), so results that rank well in both signals appear first.

The embedding model is [jina-embeddings-v2-small-en](https://huggingface.co/jinaai/jina-embeddings-v2-small-en) (512-dim), running entirely on your machine via [fastembed](https://github.com/qdrant/fastembed). No data leaves your machine on this path.

## Embedding coverage and backfill

### Coverage

New sessions are embedded automatically as they sync (embed-on-write), so coverage builds forward on its own. Long exchanges are embedded at draft quality on this path (capped at 4,096 tokens, to bound memory), and history brought in by `ccrecall import` isn't embedded at all; a backfill covers both. Only **active-leaf** branches are embedded — at most one active leaf per session (maintained by sync/import), not its abandoned forks/retries. The flag isn't DB-enforced, but sync/import marks exactly one branch `is_active=1` per session. The search path only ever returns active leaves, so embedding inactive forks would just produce vectors that can never surface.

### Optional: seed historical conversations

Embedding runs on CPU via fastembed. jina-v2-small-en is light — a few milliseconds for a short summary, up to ~400ms for a long one — but seeding a large history (~2k active leaves) is still a bounded chunk of work, and a parallel run can thrash a small or shared box. It is therefore **opt-in** — it is *not* auto-spawned on SessionStart — so it never fires unbidden. Run it yourself when you want to seed:

```bash
ccrecall backfill embeddings              # all active leaves, all history
ccrecall backfill embeddings --days 14    # only the last 14 days
ccrecall backfill embeddings --limit 500  # cap this run at 500 branches
ccrecall backfill embeddings --threads 4  # use 4 inference threads (idle machine)
```

It runs at low scheduling priority (`nice`) and a single inference thread by default so it yields to interactive work. Tune the thread count with `--threads` (e.g. `--threads 4` on an idle workstation to finish faster). Progress prints to stderr (one line per batch); the run is resumable — re-running skips already-embedded branches. Each run also starts by checking that branches marked as embedded really have all their exchanges, and picks up any that don't.

### Flags

| Flag | Effect |
|------|--------|
| `--keyword-only` | Skip the embedding step entirely, use keyword search only |

To see embedding coverage and whether semantic search is available, run `ccrecall status` or `ccrecall doctor`.

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

In all cases, search falls back to keyword-only and returns results normally. When this happens (or when embedding coverage is still catching up below ~95%), a recall appends a one-line caveat to its own results so you know they may be partial — surfaced only when you actually run a recall, never as a standalone nag. `ccrecall doctor` reports `embedding-unavailable` when semantic search is off.

## Skills

| Skill | Trigger | What it does |
|---|---|---|
| `/ccr-recall` | "what did we discuss", "continue where we left off", "search my conversations" | Lets Claude search or browse your past sessions on demand |
| `/ccr-resume` | "pick up where we left off after /clear", a stop, or an unanswered question | Reconstructs the prior session's intent from its transcript tail and surfaces any unresolved decision |

## Entry points

### Hooks (run automatically — don't call these manually)

These are wired by the plugin's `hooks/hooks.json` and fire on their respective Claude Code events.

| Entry point | Event | What it does |
|---|---|---|
| `ccrecall-setup` | SessionStart | Creates `~/.ccrecall/` if needed and starts background work: `ccrecall import --repair-gaps` when there's no database yet or a session has synced since the last import, `ccrecall backfill summaries` when a session's summary is missing or outdated, and `ccrecall-warm-model` every time. See [What runs when](#what-runs-when) |
| `ccrecall-context` | SessionStart (startup + clear) | Injects a summary of your most recent session into Claude's context so it knows what you were working on. On `/clear`, reads a handoff file to link directly to the session you just cleared from |
| `ccrecall-clear-handoff` | SessionEnd (clear only) | Writes a small handoff file so the next session start knows which session to link to after a `/clear`. Without this, context injection falls back to a "most recent session" heuristic |
| `ccrecall-sync` | Stop | Syncs the current session to the DB in a detached background process. Runs every time Claude finishes a response |

> These are kept as separate console scripts (rather than `ccrecall hook …` subcommands) on purpose: hooks fire on every session boundary, and a direct entry point avoids eagerly importing the full CLI command surface on the hot path.

### Internal helper (spawned by a hook — don't call it manually)

| Entry point | What it does |
|---|---|
| `ccrecall sync-current` | Syncs a single session file to the DB. Called by `ccrecall-sync` with the session ID from stdin |

### Background commands you can also run by hand

The hooks start these on their own (see [What runs when](#what-runs-when)), but running one yourself is safe, and it's what `ccrecall doctor` tells you to do when one is needed. If the same job is already running, a manual run skips and exits 0.

| Entry point | What it does |
|---|---|
| `ccrecall import` | Full import of all JSONL files in `~/.claude/projects/`. Skips files that haven't changed since last import (size and modified time, then file hash). With `--repair-gaps`, it also re-imports sessions whose stored history is missing messages from the transcript. `doctor`'s fix for `no-database` and `fixable-sessions` |
| `ccrecall backfill summaries` | Generates context summaries for any DB branches that don't have a current one |

### Skill CLIs (called from skill files — can also be used directly)

These are the `ccrecall` subcommands the `/ccr-*` skills invoke. You can run them from the terminal too.

| Entry point | What it does |
|---|---|
| `ccrecall recent` | Prints recent sessions from the DB in markdown (default) or JSON. Used by `/ccr-recall` |
| `ccrecall search` | Searches sessions by keyword fused with vector similarity (FTS5 → FTS4 → LIKE fallback, RRF-fused with jina embeddings when available). Used by `/ccr-recall` |
| `ccrecall tail` | Reads the tail of a prior session's transcript to recover the last instruction and any unanswered question. Used by `/ccr-resume` |
| `ccrecall backfill embeddings` | Opt-in seeding of embeddings for historical active-leaf branches (jina-v2-small-en via fastembed). Not auto-spawned. Supports `--days N` / `--limit N` / `--threads N`; throttled via `nice` + a single inference thread by default. Resumable |
| `ccrecall backfill tool-content` | Opt-in re-parse of already-synced sessions' JSONL files to populate `messages.tool_content` for rows synced before tool-content extraction existed. Not auto-spawned. Supports `--days N` / `--limit N`; resets `embedding_version` on touched branches so `backfill embeddings` re-embeds them. Resumable |

## Data flow

```
Claude finishes a response
  └─ ccrecall-sync (Stop hook)
       └─ ccrecall sync-current (background)
             └─ writes to ~/.ccrecall/conversations.db
             └─ embeds the active leaf via jina at draft quality
                  (skipped while an embedding backfill runs; a failure is recorded for the SessionStart alert)

/clear (SessionEnd)
  └─ ccrecall-clear-handoff
       └─ writes a handoff file naming the session being cleared
            (so the next SessionStart links to it instead of guessing)

Session starts
  └─ ccrecall-setup (SessionStart)
  │    └─ ccrecall import --repair-gaps (background, if no DB yet or a session synced since the last import)
  │         └─ never embeds; imported history waits for an embedding backfill
  │    └─ ccrecall backfill summaries (background, if summaries missing or outdated)
  │    └─ ccrecall-warm-model (background, every time)
  │    └─ (embedding backfill is NOT auto-spawned — opt-in via ccrecall backfill embeddings)
  └─ ccrecall-context (SessionStart, startup + clear)
       └─ injects deterministic context
```

## Config file

`~/.ccrecall/config.json` is an optional user-created override file. ccrecall uses these defaults when a key is absent:

| Key | Type | Default | Effect |
|---|---|---|---|
| `auto_inject_context` | bool | `true` | Inject a summary of your previous session at session start. |
| `max_context_sessions` | int | `2` | How many recent sessions to include in that injected context. |
| `exclude_projects` | list[str] | `[]` | Project names to skip when **storing** conversations — excluded projects are not imported or synced. Matched against the project's directory name. This is write-side only: it prevents new data from being indexed; it does not remove or hide conversations already stored before the project was excluded. |
| `logging_enabled` | bool | `true` | Write hook diagnostics (including swallowed hook exceptions) to per-process files such as `~/.ccrecall/ccrecall-sync.log`. Set to `false` to suppress logging. |
| `log_level` | str | `"INFO"` | Logging verbosity when `logging_enabled` is true. Accepts standard Python level names: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `alert_snooze_hours` | int | `24` | After surfacing a SessionStart alert (see [When ccrecall speaks up](#when-ccrecall-speaks-up)), suppress the same alert for this many hours. |
| `backfill_stale_hours` | number | `72` | How long `ccrecall doctor` lets queued embedding work wait, and a running import or backfill run, before calling it overdue or stuck. |

Example config:

```json
{
  "auto_inject_context": true,
  "max_context_sessions": 2,
  "exclude_projects": ["work-secret"]
}
```

> ccrecall is local-only. Nothing it stores or derives is sent anywhere.

## Database

`~/.ccrecall/conversations.db` — SQLite, WAL mode. Tables:

- `sessions` — one row per conversation session
- `branches` — one row per conversation branch (rewinding creates new branches)
- `messages` — all messages, stored once per session regardless of branch
- `branch_messages` — join table linking messages to branches
- `import_log` — tracks which JSONL files have been imported and their hashes
- `chunks` — one row per exchange (user turn + following assistant turns), carrying bounded display text and embedding bookkeeping
- `chunk_vec` — sqlite-vec virtual table storing 512-dim jina embeddings keyed by chunk rowid, used for KNN search

Two small files sit beside it, both safe to delete: `conversations.db.runs.json` records the newest and the last successful import, gap repair, sync and embedding backfill, and `conversations.db.unrepairable.json` lists sessions whose transcript is itself missing messages, so `doctor` doesn't offer a repair that can't help. Each can have a `.lock` file beside it, also safe to delete.

## Development

```bash
uv sync                       # install package + dev dependencies
uv run pytest                 # run the test suite
uvx prek run --all-files      # run the lint/format/type hooks
```

## License

[MIT](LICENSE)
