# Design: Session Recaps (attempt 3)

**Date:** 2026-11-15
**Status:** ratified
**Mode:** sketch
**Prior art:** design/specs/021-session-recaps-v3/prior-art.md

## Summary

A session-restore picker (or any tool listing past sessions) needs one or two lines per session that jog the user's memory: what the session was for and where it stopped. Two earlier attempts built far more than that (the LLM summary enrichment subsystem of `design/specs/008-llm-summary-enrichment`, deleted in #134; `design/specs/010-session-recaps`, abandoned unmerged, see its `retrospective.md`). This attempt keeps only the recap.

Claude Code already writes such a recap into transcripts as `{"type":"system","subtype":"away_summary","content":...}`, but only when the terminal loses focus for 3 minutes after a turn: 69% of interactive sessions have one and 28% have a current one (`evaluation.md` population). This ledger calls it the harness recap. ccrecall will use that recap when it's current and generate its own otherwise. `evaluation.md` in this directory carries the probe evidence: recaps generated from ccrecall's own database are recognizable 34 times out of 35, and a prose-only input of the whole branch recaps a compacted session as well as its post-compaction slice (run 4).

The change:

- **Parse boundary (`parsing.py`, `models.py`).** The branch dict that `find_all_branches` returns gains the latest `away_summary` on the active chain (text with Claude Code's " (disable recaps in /config)" suffix stripped, and its timestamp). `active_chain`'s returned entries do not change.
- **Stored facts (`schema.py`, `db_base.py`, `branch_ops.py`).** The `away_summary` and `away_summary_at` columns go on `branches` (D2). One new `recaps` table holds generated recaps, one row per session: `session_id`, `text`, `as_of` (the last message the recap covers, D2), `error` and `attempted_at` (D5); `text` and `as_of` are null until a call succeeds.
- **Generation (`recaps.py` new; `hooks/backfill_recaps.py` new; `cli/commands.py`).** `ccrecall backfill recaps [--days N] [--limit N] [--idle-minutes N]` imports first (D9), selects sessions (D3, D4), builds the input from imported rows, makes one Claude Agent SDK call per session, and writes the result. `recaps.py` owns the D2 pick function (which `show-recaps` also calls), the selection predicate (D3, D4, D5), the input builder and its cap, the failure-class mapping, and the `recaps` table's reads and writes. It takes its own job lock and records its runs (D8: `run_history.py`, `config.py`, `findings.py`, `health.py`), logging to its own `backfill-recaps` process log. Generation never runs on a hook path or at read time.
- **Call isolation (`config.py`, `recaps.py`, `hooks/import_conversations.py`, D10).** Generation calls run from a fixed directory under `~/.ccrecall/`, and import skips the Claude Code project that directory maps to. The directory constant lives in `config.py`, which both `recaps.py` and import already depend on, so import never imports `recaps.py`.
- **Read surface (`cli/commands.py`, D6).** `ccrecall show-recaps <id>...`, a cache-only read that returns each session's best recap with its source and whether it's current. It never calls Claude.
- **Packaging (`pyproject.toml`, D7).** How the SDK is packaged and imported is D7.

Out of scope: SessionStart injection, search cards, shipping a timer or scheduler, model or prompt settings, any purge command (a `forget-project` command would be its own issue), and backfilling the new branch columns for sessions that never sync again.

## Posture

- **Failure handling:** fail loudly per session and keep the run going. A failed session records its error and is skipped for a cooldown; the run continues to the next session and finishes `partial`. No retries within a run and no backoff ladder. Evidence: `../010-session-recaps/retrospective.md`, #114; peers in `prior-art.md` carry no retry machinery.
- **Compatibility:** additive only for stored data (new table, new nullable columns, no removals or rewrites); the two new CLI commands (`backfill recaps`, `show-recaps`), `show-recaps`'s JSON output, and the `recaps-failed` and `recaps-killed` `doctor` finding ids become public contracts. Evidence: CLAUDE.md ("the conversations DB is a public contract now"); published on PyPI with release-please.
- **Scope:** minimal: one generation command and one cache-only read. Evidence: the one consumer is a restore picker; attempt 2's 26 requirements are the counterexample.
- **Rollout:** ships outright. It does nothing until the user runs or schedules the command, so it's opt-in without a flag (the SDK itself is installed for everyone, D7).
- **Hardening:** project defaults for a background job: per-process rotating log, a run-history record, and the `doctor` finding that run history already drives. Recap generation sends session content to Anthropic, the same party that already received every message in that transcript when the session ran, so no new recipient sees it; sessions in projects currently listed in `exclude_projects` are never sent, including ones imported before the exclusion (D4). Evidence: CLAUDE.md's Run history and Per-process logging sections; `import_conversations.import_project` and `sync_current` enforce exclusion only at write time.
- **Altitude:** `build`; one feature in one repo, built in one session.

## Decisions

### D1: Does the latest compaction (boundary time and summary) get stored for the input?

**Situation:** A post-compaction input (the compaction summary plus the turns after the boundary) would need two facts the database lacks. `parsing.active_chain` crosses a `compact_boundary` through `logicalParentUuid` and, when that hop resolves, drops the `isCompactSummary` entry from the chain, so the summary never reaches the database. `compact_boundary` is a `system` entry, and `message_ops.build_message_row` only stores user and assistant rows, so the boundary's timestamp isn't stored either. Today the database cannot tell a compacted session from an uncompacted one.

**Deciding factor:** recap quality and cost on compacted sessions, weighed against the schema and parse changes storing compaction facts needs.

| | A: Two nullable columns on `branches` | B: Keep the summary on the chain as a flagged message | C: A `compactions` table, one row per boundary | D: Store only the boundary time | E: Store nothing; the input is the whole branch |
|---|---|---|---|---|---|
| What's stored | `compact_summary`, `compact_boundary_at` for the latest compaction | The summary as a `messages` row with a new `is_compact_summary` flag | Every boundary with its summary | `compact_boundary_at` only; input is the turns after it, no summary | Nothing new |
| Effect on search and embeddings | None | The summary text joins `aggregated_content` (FTS) and an exchange chunk, duplicating content already indexed | None | None | None |
| `ACTIVE_CHAIN_VERSION` bump | No | Yes: the chain's membership changes, invalidating every cached ok verdict and triggering a repair pass | No | No | No |
| Fit | `branches` already holds derived per-branch facts (`files_modified`, `context_summary`) | Reuses existing message storage | Cleanest model, but only the latest row is ever read | Smallest | No new state |
| Recap quality (prose-only input, 8 compacted sessions, `evaluation.md` run 4) | Goal 7/8, next 7/8, recognizable 8/8 | Same input as A | Same input as A | Not probed (no evidence either way) | Goal 7/8, next 6/8, recognizable 8/8; no drift to earlier work |
| Cost per compacted session | ~0.7× of E | ~0.7× of E | ~0.7× of E | Between A and E (turns after the boundary, no summary text) | 1.00× ($0.04–0.10 per call, run 4) |
| Moving parts | One delta migration, one upsert change, two input paths, a null-column fallback | Flag column, chain change, every consumer must skip the flag | New table, its writes and pruning | One column | None; one input path |

**Recommendation:** E. With the prose-only input (Assumed), a compacted session's whole branch is 25–66k characters and recapped as well as the post-compaction slice, so the columns buy only about 30% lower cost on compacted sessions. A was preferred while the input included `tool_content`, where whole branches were several times larger and drifted to earlier work (`evaluation.md` run 1). Challenge Finding 9.
**Pick A instead if** compacted sessions grow far past today's sizes, since its saving grows with history length.
**Reversibility:** easy (adding A later is an additive migration plus a parse change)
**Ratified:** Chose storing nothing and sending the whole branch's prose over two nullable `branches` columns for a post-compaction input, after a head-to-head on 8 compacted sessions showed equal quality, to keep one input path with no schema or parse change for compaction, accepting about 40% more input cost on compacted sessions.

### D2: Where does Claude Code's `away_summary` live, and who decides which recap to show?

**Situation:** The `away_summary` entries are `system` entries on the active chain and are discarded today, like `compact_boundary`. The recap the read surface returns should be the harness one when it's current and the generated one otherwise.

**Deciding factor:** one writer per stored fact, so no two code paths have to agree about the same row (the retrospective's failure shape).

| | A: Columns on `branches`; the read picks | B: Sync writes it into `recaps` as `source = harness` | C: Read it from the transcript when needed |
|---|---|---|---|
| What's stored | `away_summary`, `away_summary_at` (latest on the chain) | A `recaps` row written by sync and import | Nothing |
| Writers of `recaps` | Only `backfill recaps` | Sync, import, and `backfill recaps` | Only `backfill recaps` |
| Harness recap visible without a backfill run | Yes | Yes | Yes, while the transcript exists |
| Works after Claude Code deletes old transcripts | Yes | Yes | No |
| Who decides which recap wins | One function used by both the read and backfill selection | Each writer has to respect the other's rows | One function, but it parses a transcript at read time |

**Recommendation:** A, because `recaps` keeps a single writer and the harness-or-generated choice lives in one function both the read and the selection call.
**Pick B instead if** you want every recap, whatever its source, in one table that's simple to query directly. **Pick C instead if** storing another column is worse than losing harness recaps for deleted transcripts.
**Reversibility:** easy
**The pick rule:** of the session's harness recap and generated recap, the one with the newer `as_of` (the time of the last message it covers; for the harness recap, its own timestamp) wins, and the harness recap wins a tie. The branch's last message means the last row the input builder includes: a user or assistant row that is not a notification and has non-empty content. A recap is current when it covers the branch's last message; this is the only definition of the last message and of current, used by D3, D4, D6 and the input. The read returns the winner, and `backfill recaps` never selects a session whose winner is current (D3's idle test and D5's cooldown narrow selection further). Both call the same function.

**Ratified:** Chose `away_summary`/`away_summary_at` columns on `branches` with one shared function picking the recap by newest `as_of`, over sync writing harness rows into `recaps` or reading transcripts at read time, to keep `recaps` single-writer and the pick a single rule, accepting two more `branches` columns.

### D3: When is a generated recap stale enough to regenerate?

**Situation:** A timer runs `backfill recaps` every ~20 minutes. A session still being worked on changes between runs. Regenerating on any change would call Claude for every active session every run; never regenerating would leave the recap describing the start of the session. The case that matters most is a session killed by a shutdown: its last recap should describe where it stopped.

**Deciding factor:** the restored session's recap reflects its final state, without a call per active session per run.

| | A: ≥2 new user turns, or ≥1 when idle ≥30 min | B: ≥2 new user turns | C: Any new message | D: Changed since the recap and idle ≥ `--idle-minutes` (default 30) |
|---|---|---|---|---|
| Calls for a session being actively worked on | About one per 2 user turns | About one per 2 user turns | One per run while it changes | None until it goes quiet; then one per break that follows new work |
| Session killed one turn after its last recap | Regenerated on the first run 30+ minutes after the last activity | Stays one turn behind for good | Regenerated next run | Regenerated once idle; a boot run passing `--idle-minutes 0` catches it immediately after a quick reboot |
| Precedent | Claude Code's own "2 new prompts since the last recap" rule, plus an idle catch-up | Claude Code's own rule | universal-session-viewer's content-hash invalidation | Claude Code's own recap fires only after the user has been away |
| Stored | User-turn count and last-message time the recap covers | User-turn count covered | Last message covered | Last-message time the recap covers |

Whether a recap is current is D2's rule; it is not part of this choice. "Changed since the recap" means the branch's last message (D2) is newer than the one the recap covers.

**Recommendation:** A, because it is Claude Code's own rule plus the one catch-up a shutdown needs.
**Pick B instead if** a recap one turn behind is fine for restore. **Pick C instead if** cost doesn't matter and you want every recap exact. **Pick D instead if** recaps are needed only for sessions that have stopped, and fewer calls matter more than a recap for a session still in progress.
**Reversibility:** easy (a selection predicate)
**Ratified:** Chose D (regenerate when the branch changed since its recap and has been idle at least `--idle-minutes`, default 30, with a boot-time run passing 0) over the turn-count rules and any-change, to spend calls only on sessions that have stopped, accepting that a session in progress has no generated recap until it goes quiet.

### D4: Which sessions does `backfill recaps` consider?

**Situation:** ccrecall indexes interactive sessions alongside headless ones (digests, bots, SDK calls). The `sessions` table doesn't record Claude Code's `entrypoint`, so it can't tell them apart. Most headless sessions have one or two user turns. The restore picker only cares about sessions from the last few days.

**Deciding factor:** spend calls only on sessions a person might restore.

| | A: ≥3 user turns, active within `--days` (default 7) | B: A plus a stored `entrypoint` filter (interactive only) | C: Every session with ≥3 user turns, all history |
|---|---|---|---|
| Headless sessions included | Only multi-turn ones (bots) | None | Only multi-turn ones |
| First run on an existing install | Last week's sessions | Last week's interactive sessions | Every session ever, thousands of calls |
| New stored data | None | `sessions.entrypoint` column, filled only for sessions synced after the change | None |
| Projects in the current `exclude_projects` | Never selected | Never selected | Never selected |
| Precedent | Claude Code's ≥3-prompt minimum; engineering-notebook's `--date` scoping | Claude Code's recap runs only in the interactive UI | engineering-notebook's `summarize --all` |

Exclusion matches by project name and, for every stored project, by the lossy key (`project_ops.key_could_match_excluded`), so selection errs toward excluding: exclusion at import time leaves already-stored rows in place, and stored rows don't record whether import used the lossy fallback. A user turn is a user row that is not a notification and has non-empty content. Candidates are taken newest last message first, and `--limit` caps calls per run under every option, with no default: the first run on this machine would select at most 131 sessions (`evaluation.md` population). A session whose transcript hasn't changed since the upgrade has null `away_summary` columns (import skips unchanged files, and backfilling the columns is out of scope), so the first run can generate a recap for a session that already has a current harness recap; the generated recap wins until the session next syncs and its harness columns fill, after which D2's rule picks between them.

**Recommendation:** A, because it covers what restore shows with no new stored data, and the week window bounds the first run.
**Pick B instead if** multi-turn headless sessions turn out to be a large share of the calls. **Pick C instead if** recaps should also serve old-history browsing.
**Reversibility:** easy
**Ratified:** Chose sessions with ≥3 user turns active within `--days` (default 7), excluding projects in the current `exclude_projects`, over an entrypoint filter or all history, to cover what restore shows with no new stored data and never send an excluded project's content, accepting that multi-turn headless sessions also get recaps.

### D5: How does a run handle failed calls?

**Situation:** A call can fail for one session (a malformed or empty answer, a timeout) or for all of them (not logged in, rate limited, `claude` missing). #114 was a retry loop with no bound. Under the posture, a failed session is recorded and skipped for a cooldown; what's open is how long, and what stops a run that fails on everything.

**Deciding factor:** a session that keeps failing can't spend a call every 20 minutes, and a run that can't succeed stops early.

| | A: 24 h cooldown per session; abort after 3 consecutive failures | B: Retry only when the session's input changes; abort after 3 | C: Cooldown only, no abort | D: A, with run-level failures split out (abort at once, no cooldown) |
|---|---|---|---|---|
| A session that always fails | One call a day | One call per stale event while it's active; none once it goes quiet | One call a day | One call a day |
| Not logged in, 30 eligible sessions | 3 calls, then the run ends `failed`, and those 3 sessions wait a day | 3 calls, then `failed` | 30 calls, each cooled down for a day | 1 call, then `failed`; nothing cools down, and the next run retries |
| Precedent | Embedding backfill aborts after 3 consecutive lock timeouts (CLAUDE.md, `backfill_embeddings`) | universal-session-viewer's hash-keyed cache | None | Claude Code skips its recap near a rate limit rather than failing it (`prior-art.md`), plus A's backstop |
| Stored | `error`, `attempted_at` on the session's `recaps` row | Same, plus the input it failed on | `error`, `attempted_at` | Same as A, for per-session failures only |
| New logic | None | Input hash | None | An error-kind mapping, which can misclassify |

**Recommendation:** A, because it bounds both cases with the abort rule the embedding backfill already uses.
**Pick B instead if** you'd rather a session that failed retry as soon as it changes. **Pick C instead if** you'd rather each run attempt everything.
**Reversibility:** easy
**Failure classes (option D):** a run-level failure (not logged in, rate limited, the bundled `claude` CLI failing to start; the build maps these from the SDK's exception types and error results, and records the mapping as a build-time call, as it does the per-call timeout, whose expiry is a per-session failure) ends the run `failed` at once and writes no cooldown, so the scheduler's next run is the retry. Any other failure is per-session: it records `error` and `attempted_at`, the session is skipped for 24 hours, and the run continues. Three consecutive per-session failures still abort the run, as a backstop for a run-level failure the mapping misses.

**Ratified:** Chose D, splitting failures into run-level (abort, no cooldown) and per-session (24-hour cooldown, run continues) with the 3-consecutive abort kept as a backstop, over one failure class with a cooldown on every failure (A, C) or input-keyed retries (B), so a lapsed login or rate limit recovers on the next run while a session that always fails costs one call a day, accepting an error-kind mapping that can misclassify, and that a refusal or apology from the model is stored as the recap until the session changes (no recap in any of the four probe runs refused; Claude Code's own recap has no validator either; challenge Finding 14).

### D6: What does the read surface look like? (named `show-recaps` at ratification)

**Situation:** The consumer already knows which session ids it's about to show (from Claude Code's own session files) and wants their recaps in one call, quickly, with no Claude call. `ccrecall recent --json` lists ccrecall's own idea of recent sessions, not the consumer's.

**Deciding factor:** one cheap call that answers "recap for these ids".

| | A: New `ccrecall show-recaps <id>...` command | B: A `recap` field on `recent --json` and `recent --session` | C: Both |
|---|---|---|---|
| Consumer's call | One call with its ids | One call per session, or filter `recent`'s list itself | Either |
| New public surface | One command and its JSON shape | One field on an existing shape | Both |
| Output per id | `session_id`, `text` (null when none), `source` (`claude_code` or `generated`), `current`, `as_of` | Same fields nested under `recap` | Both |
| Markdown (non-`--json`) output | One line per session | A line in each session block | Both |

Ids are full session UUIDs or the 8-character prefixes ccrecall prints elsewhere. A prefix that matches several sessions returns a row for each, like the `--session` prefix filter. An unknown id, or a session with no recap, returns a row with `text`, `source` and `as_of` null and `current: false`, not an error. `current` is D2's definition; the read never imports or calls Claude, so it is as fresh as the last import (D9 makes it fresh right after a recap run).

**Recommendation:** A, because it matches how the consumer already holds its ids and keeps `recent`'s output unchanged.
**Pick B instead if** a consumer would rather list recent sessions and their recaps from ccrecall alone. **Pick C instead if** both kinds of consumer exist.
**Reversibility:** hard (public CLI and JSON contract once released)
**Ratified:** Chose a new batch read command over a field on `recent`, named `ccrecall show-recaps <id>...` rather than `recap` (which reads as "make a recap") or `recaps` (which shares a word with `backfill recaps`), to give the consumer one cache-only call for the ids it holds, accepting a new public command and JSON shape.

### D7: How is the Claude Agent SDK packaged and imported?

**Situation:** `claude-agent-sdk` (0.2.182) bundles a ~330 MB Claude Code binary and takes about 0.6 s to import. The CLI eager-imports every command module, and the no-lazy-imports rule (house-lint HSL002) applies, with a precedent for a defended exception in `db.py`. Hooks don't import the CLI, so they're unaffected either way.

**Deciding factor:** what installs that don't use recaps should pay, in install size and CLI startup, against one install path with no optional-dependency handling.

| | A: Optional extra; one defended lazy import in the generator | B: Optional extra; guarded top-level import | C: Base dependency; top-level import | D: Optional extra; separate `ccrecall-recaps` console script |
|---|---|---|---|---|
| Install size without recaps | Unchanged | Unchanged | +~330 MB for every user | Unchanged |
| Startup cost on every `ccrecall` command | None | +~0.6 s when the extra is installed | +~0.6 s | None |
| Lazy-import rule | One `ignore-next[HSL002]` with a reason, like `db.py` | Followed | Followed | Followed |
| Where the command lives | `ccrecall backfill recaps` | `ccrecall backfill recaps` | `ccrecall backfill recaps` | A separate binary outside the `backfill` family |
| Extra not installed | The command exits 1 with "install ccrecall[recaps]" | Same | n/a | Same |

**Recommendation:** A, because it is the only option where both installs without recaps and every other command are untouched, and the repo already has this exception pattern.
**Pick D instead if** a lint exception is worse than a second entry point. **Pick B instead if** 0.6 s on every command is acceptable for installs with the extra. **Pick C instead if** one install path with no optional-dependency handling matters more than install size and CLI startup.
**Reversibility:** easy
**Ratified:** Chose making `claude-agent-sdk` a base dependency with a normal top-level import over an optional extra (lazy, guarded, or separate script), to keep one install path with no missing-extra handling or lint exception, accepting ~330 MB more per install and ~0.6 s more on every `ccrecall` CLI invocation (including the detached `sync-current`; hook entry points are unaffected).

### D8: Does `backfill recaps` record run history?

**Situation:** Run history covers import, repair, sync and the embedding backfill (`run_history.RUN_KINDS`), and `doctor` turns a failed run into a `<kind>-failed` finding. `backfill summaries` and `backfill tool-content` record nothing. Recaps run unattended on a timer, and their common failure (an expired login) is otherwise visible only in a log file.

**Deciding factor:** an unattended failure shows up somewhere the user already looks.

**Recommendation:** add a `recaps` run kind with its own job key, recording through `run_history.record_run` like the embedding backfill. `doctor` then reports the generated `recaps-failed` and `recaps-killed` findings (new public finding ids, plus `KIND_LABELS`, `KIND_FIXES`, `KIND_LOGS` and a `FIX_*` constant). A `partial` run (some session failed) counts as failed there, as `run_history.failed_run` already treats it, so one failed session shows `recaps-failed` until the next run finishes clean; that session is cooling down, so the next run normally does. The kind is not in `LONG_RUNNING_KINDS`: a run is bounded by `--limit` and takes minutes, so no `-running` or `-stuck` finding. No SessionStart alert.
**Pick "no run history" instead if** you're fine checking the log file when recaps look stale.
**Reversibility:** easy
**Ratified:** Chose a `recaps` run-history kind with its own job key over log-only, so `doctor` reports `recaps-failed` for an unattended failure, accepting one more kind in run history and no SessionStart alert.

### D9: Does `backfill recaps` import before it selects?

**Situation:** Whether a session is idle (D3) and whether a recap is current (D2, D6) are both judged from the last imported message. Sync runs only when a turn ends (the Stop hook) and import at SessionStart, so the database trails the transcript. The trailing is worst in the restore case: a session killed mid-turn has its last turn unimported, and the boot-time run (`--idle-minutes 0`) runs before the next SessionStart import. A long turn also syncs nothing until it ends, so it can look idle.

**Deciding factor:** liveness and currency judged against what the transcript actually holds, with no second source of truth.

| | A: Import first, then select | B: Accept the lag | C: Read transcript metadata (mtime) for liveness |
|---|---|---|---|
| Killed mid-turn session on the boot run | Its last turn is imported before selection | Recap one turn behind until a later import | Idle judged correctly; input still misses the last turn |
| Long turn looking idle | Only a single tool call silent for longer than `--idle-minutes` | Any turn longer than `--idle-minutes` | Avoided |
| Moving parts | One call to the existing import path at the start of the run | None | A second source beside imported rows |
| Cost | Steady-state import skips unchanged files by size and mtime (CLAUDE.md "Import locking") | None | One stat per session |

**Recommendation:** A, because it removes the lag at its source using the import path that already exists.
**Pick B instead if** a recap one turn behind is acceptable. **Pick C instead if** importing on every recap run costs too much.
**Reversibility:** easy
**Ratified:** Chose running the existing import at the start of every `backfill recaps` run (proceeding without waiting when another process holds the import job lock, since that import is already catching up) over accepting the lag or reading transcript metadata, so selection and `current` see each transcript's latest entries, accepting an import scan per run and that a single tool call silent longer than `--idle-minutes` can still look idle.

### D10: How are generation calls kept out of the import if `--no-session-persistence` stops working? (challenge Finding 13)

**Situation:** Each SDK call is a headless Claude Code session. `--no-session-persistence` keeps it from writing a transcript, and a test pins the argument, not what the CLI does with it. If a release stops honoring the flag, every call leaves a transcript holding its whole input, ccrecall imports it, and those copies duplicate other sessions' content into search. The probe produced 146 of them, and no purge command exists.

**Deciding factor:** a regression must not leave a state the user can only clean up by hand.

| | A: Run calls from a dedicated cwd that import always skips | B: Accept |
|---|---|---|
| Damage from a flag regression | Stray transcripts land in one Claude Code project directory nothing imports | Imported copies need a hand purge |
| New logic | One fixed directory and one import skip rule | None |

**Recommendation:** A, because the accepted state can only be undone by editing the database by hand, and the guard is one constant and one skip rule.
**Pick B instead if** you'd rather catch a regression through the pinned test and release notes.
**Reversibility:** easy
**Ratified:** Chose running every generation call with `cwd` set to one fixed directory under `~/.ccrecall/`, with import always skipping the Claude Code project that directory maps to (a test pins the skip), over relying on the flag alone, so a regression leaves stray files nothing reads, accepting one more skip rule in import. `sync_current` needs no skip: generation calls pass `setting_sources=[]`, so no hooks fire inside them.

## Assumed

- Generation input comes from imported rows only, never transcript files: the `content` of the branch's user and assistant rows, in timestamp order, across any compaction boundary (D1). Notification rows (`is_notification`, i.e. `<task-notification>` subagent reports) and `tool_content` are left out: the recap prompt asks for the goal, the current task and the next action, which live in the prose, while notifications are reports the assistant restates and tool markers are inputs with no results (17 of 1,661 sessions active in the 14 days to 2026-11-15 end on an assistant turn with no prose, `evaluation.md` population). Rows with empty content are skipped. There are no per-message caps. Because D2's "last message" is the last row this input includes, a late notification never triggers a regeneration from the same input. Evidence: `evaluation.md` runs 1–2 (inputs that included `tool_content`); `../010-session-recaps/evaluation.md`; `evaluation.md` runs 3 and 4 (prose-only input matched the reference on goal and next step in every probed notification-heavy session, at 30% of the unshaped input's cost, and the whole branch of a compacted session recapped as well as its post-compaction slice).
- The whole input is capped at 400,000 characters (about 100k tokens, well inside the model's context window). Over the cap, it keeps the first user turn and then the most recent rows that fit, with a marker where the middle was dropped. The cap never triggers on current data: across all 9,088 stored sessions the largest prose-only branch is 137k characters (p99 52k, p95 29k). A test must pin the whole-branch input (including a compacted branch) and the cap. Evidence: measured against `~/.ccrecall/conversations.db` on 2026-11-15; challenge Finding 8.
- A recap's covered-through time is read in the same read as its input and written with its text, so messages synced during the call are never counted as covered. A failed regeneration records `error` and `attempted_at` and never clears or replaces an existing `text`; a success clears `error`. A test must pin both. Evidence: D3's stored last-message time and D5's stored `error`/`attempted_at` share the session's `recaps` row; challenge Finding 10.
- The generation call uses `claude-sonnet-5-5`, Claude Code's recap prompt verbatim ("The user stepped away and is coming back. Recap in under 40 words, 1-2 plain sentences, no markdown. Lead with the overall goal and current task, then the one next action. Skip root-cause narrative, fix internals, secondary to-dos, and em-dash tangents."), the system prompt "You are the assistant from the Claude Code session in the transcript below, talking to the same user. Answer only with the recap text.", one turn, `tools=[]`, `setting_sources=[]`, `strict_mcp_config=True`, and output truncated to 400 characters. Evidence: `evaluation.md`; Claude Code 2.1.322 source (prompt text and 400-character cap).
- The input is one user message: the rows rendered as `[user]` or `[assistant]` on a line of its own followed by the row's content, rows separated by a blank line, no timestamps, wrapped in `<transcript>` … `</transcript>`, then a blank line and the recap prompt. When the cap drops the middle, a `[... middle of the session omitted ...]` line stands in its place. Evidence: the format every probe run used (`evaluation.md` runs 1–4).
- Every generation call passes `--no-session-persistence` through the SDK's `extra_args`, so it leaves no transcript for ccrecall to import. A test must pin this. Evidence: `claude --help` ("Disable session persistence ... only works with --print"); the probe's 146 imported copies (`evaluation.md`).
- The call doesn't set `ANTHROPIC_API_KEY`, so it authenticates with the user's Claude Code login. Evidence: the probe ran this way; the CLI prefers an API key when one is set.
- Recaps are keyed per session. `branches` is `UNIQUE(session_id)` with one active row per session. Evidence: CLAUDE.md invariant 4; `branch_ops.upsert_branch`.
- Claude Code's `away_summary` text may end with " (disable recaps in /config)" on a session's first three recaps; the parse boundary strips it. Evidence: Claude Code 2.1.322 source; observed in transcripts.
- Adding fields to the branch dict without changing `active_chain`'s returned entries needs no `ACTIVE_CHAIN_VERSION` bump. Evidence: `parsing.active_chain` docstring; `ingestion_status.ACTIVE_CHAIN_VERSION`.
- New columns go in as additive, unconditional delta migrations like v3/v4/v8 (`SCHEMA_VERSION` 9 → 10), and the `recaps` table as `CREATE TABLE IF NOT EXISTS`. Evidence: `db_base._migrate_to_v8`; CLAUDE.md invariant 4.
- Nothing in this change runs on a hook's hot path. The hook entry points (`ccrecall-setup`, `ccrecall-sync`, `ccrecall-context`, `ccrecall-clear-handoff`, `ccrecall-warm-model`) and the modules they import never import `recaps.py`; `hooks/backfill_recaps.py` is a helper run only from the CLI, like `hooks/backfill_embeddings.py`. Evidence: CLAUDE.md invariants 2 and 3 and its `hooks/` description.

## Build

- [x] Implementation and tests committed
- [x] Docs: README.md's "no data leaves it" (opening paragraph) and "ccrecall is local-only. Nothing it stores or derives is sent anywhere." (the config section's closing note), and CLAUDE.md's frontmatter `data-sensitivity`, which says transcripts are "never transmitted", all say that `backfill recaps` sends session content to Anthropic, the same party that received it when the session ran, and that excluded projects are never sent
- [ ] Ship-time challenge

**Calls made during the build:**
- Failure-class mapping (D5): run-level is `CLINotFoundError`/`CLIConnectionError` (`claude_unavailable`), an assistant error of `authentication_failed`/`billing_error` or HTTP 401/402/403 (`claude_auth`), `rate_limit` or 429 (`rate_limited`), and `server_error` or any 5xx (`api_unavailable`). Everything else is per-session, including `invalid_request`, a malformed or empty answer, and a crash with no status. An API outage hits every session the same way a rate limit does, so it's run-level rather than a day of cooldowns.
- Per-call timeout: 300 s, and its expiry is a per-session failure. The largest prose-only input is 137k characters, so a normal call finishes well inside it.
- New run-history reason codes `claude_unavailable`, `claude_auth`, `rate_limited`, `api_unavailable` and `repeated_failures`, with prose in `REASON_PROSE`, so `doctor`'s `recaps-failed` message says why the run stopped.
- `recaps.session_id` is `sessions.id` with a foreign key, not the UUID: it matches every other table's key, and `show-recaps` resolves UUIDs and prefixes through `sessions`.
- `models.py` is unchanged. The `away_summary` entry is read from the raw entry dict in `parsing.latest_away_summary`, with `isinstance` guards. Giving `TranscriptEntry` a typed top-level `content` would reject valid lines from other entry types that carry a different `content` shape.
- `_migrate_to_v10` also runs inside the version gate after the v1/v2/v9 rebuilds, because the v1/v2 `branches` rebuilds drop columns they don't name. The migration matrix caught this, and its genuine-schema seeder now covers v9.
- D9's import reuses `import_conversations.import_locked`, renamed from `_import_locked`, under the import job lock with the recap run's logger. Its progress output goes to stderr so `--json` stdout stays one object. An import that fails some projects doesn't stop the recap run, since import records its own run.
- Exit codes: 0 when every attempted session succeeded, or when another recap run holds the lock; 1 for `partial` or `failed`, matching `ccrecall import`.
- The `--days` window and the idle test use the branch's last message (D2). `branches.ended_at`, widened by a day, only prefilters the SQL.
- `show-recaps` echoes an unknown id as the row's `session_id`, and its markdown output is one line per session: the 8-character id, the text, and its source and currency.
- The generation-call exception is `recaps.RecapCallError`. Its `run_reason` names the run-level reason, and is None for a per-session failure.
