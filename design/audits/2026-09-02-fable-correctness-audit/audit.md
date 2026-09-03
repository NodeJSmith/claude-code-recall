# Correctness Audit — ccrecall (full codebase)

**Date:** 2026-09-02
**Driver:** Reasoning-driven adversarial review (Fable 5), same shape as the hassette 2026-09-02 backend-core audit: main-model deep pass on the ingestion/sync core, five parallel audit arms over the remaining subsystems, every finding verified against code with file:line and a concrete failure scenario. Cross-checked against the open issue backlog (#22–#168) so nothing already tracked is re-filed.
**Prior audits:** 2026-08-17 structural health audit (fully executed, waves 1–4 landed); 2026-08-23/24 correctness review (#157–#165). This audit covers what those left: interleaving/crash-window analysis, invariant verification, and the seams between subsystems.

## TL;DR

The core protocols are in good shape — the embedding watermark protocol (clear-first/set-last, its cap-quality predicate, the order invariants in chunk writes), the migration gate's concurrent-upgrade handling, FTS5 query sanitization, path safety in transcript discovery, and the KNN retry ladder all survived adversarial analysis clean. The real findings cluster at three seams:

1. **The hook hot path has silently regressed** (F1): the SessionStart setup hook now eager-loads numpy/fastembed/onnxruntime/sqlite_vec via an import chain that arrived with a cleanup PR — the exact violation the hooks-as-console-scripts architecture exists to prevent.
2. **The unattended pipelines have no containment** (F2–F4, F10): one poison transcript line wedges all future imports permanently; two probe-confirmed malformed shapes (`"message": null`, `"text": null`) get through validation and crash exactly the functions the parse boundary claims to guard; and one bad row outside the summary backfill's narrow exception taxonomy wedges it permanently too — respawned and re-aborted every session, exit 0, no traceback.
3. **The PID-file mechanism is unsound in four independent ways** (F5): PID reuse after reboot turns stale markers into permanent false "alive" (silently disabling sync forever), the spawner re-implements the empty-marker race the config implementation was explicitly hardened against, probe-then-unlink can reap a live contender's marker, and the import guard is acquired by the spawner but released by anyone.

## Findings

### F1 — HIGH: SessionStart setup hook eager-loads the full embedding stack (invariant 2 violation)

`memory_setup.py:24` imports `ccrecall.hooks.import_conversations` (needed only for its `PID_KEY` constant) → `import_conversations.py:18` imports `ccrecall.db_vec` → `db_vec.py:13,16` imports `sqlite_vec` and `ccrecall.embeddings` (numpy + fastembed + onnxruntime).

Empirically confirmed: importing `ccrecall.hooks.memory_setup` loads all four heavy modules — ~504–574 ms vs ~130 ms for `memory_context`. `ccrecall-setup` runs on **every SessionStart**, so every session start pays the tax the entire hook architecture was built to avoid. #137's db.py split fixed the *old* chain; this one arrived via `import_conversations`'s vec-cascade cleanup import — a regression, not a leftover.

**Fix direction:** move the `PID_KEY` constants to `config.py` (or inline the strings). **Encode the lesson structurally** (this is the second time this invariant broke silently): a test that imports each hook entry module in a subprocess and asserts none of numpy/fastembed/onnxruntime/sqlite_vec is in `sys.modules` — the existing AST test guards only `health.py`.

### F2 — HIGH: One poison transcript line wedges the entire import run, permanently, with no log

`hooks/import_conversations.py:241-253` (per-file loop) and `_run` have no try/except around `import_session`. Any exception importing one file propagates: the connection context manager rolls back the project's uncommitted work and the detached process dies with an unlogged traceback. Because the crash precedes that file's `import_log` upsert, every future import re-hits the same file and re-crashes — **all alphabetically-later projects never import again**. Contrast `sync_current.py:231`, which contains per-session exceptions correctly. F3 gives two concrete trigger shapes.

**Fix direction:** per-session try/except that logs and skips (mirroring the sync path), plus a top-level catch/excepthook routing to the rotating log.

### F3 — MED-HIGH (probe-confirmed): two malformed shapes pass validation and crash the guarded path

- `{"type":"user","uuid":"u1","message":null}` passes `is_valid_entry` (`models.py:68` allows `message: EntryMessage | None`), then `parsing.py:295` does `entry.get("message", {})` → the key is present with `None` → `AttributeError`. Same dereference at `message_ops.py:23` and `session_tail.py:77` (crashes `ccrecall tail` too). `models.py:46`'s own docstring says non-dict `message` "is what crashed compute_branch_metadata" — the guard misses the null case.
- `{"type":"text","text":null}` inside a content list: `content.py:107` collects `None`, `"\n".join(texts)` raises `TypeError` — despite CLAUDE.md documenting `extract_text_content` as "Never raises on malformed input."

Neither shape is in the live corpus today (grep-verified) — latent, but this boundary exists precisely for upstream drift, and either one triggers F2's permanent wedge.

**Fix direction:** `entry.get("message") or {}` at the three sites (or reject null message in the validator); `item.get("text") or ""`.

### F4 — MED: upstream schema drift has no tripwire — a renamed field silently empties the corpus

Two asymmetric dark paths: (a) entries whose `uuid` is missing/falsy are dropped with no log and no counter (`parsing.py:107,130` — only JSON-decode failures increment `skipped`); if Claude Code renames `uuid`, every import "succeeds" with zero branches and zero warnings. (b) Validation rejections log per-line at INFO — systematic drift floods a 1 MB rotating log and still reports a "successful" empty import, with no WARNING aggregate like decode errors get.

**Fix direction:** count validation-rejected and uuid-less user/assistant entries into the per-file WARNING summary; add a "N entries parsed, 0 usable" tripwire. (New angle adjacent to #168, which covers interruption, not shape drift.)

### F5 — MED: the PID-file mechanism is unsound four independent ways

1. **PID reuse → permanent false "alive"** (`config.py:131-137`): markers survive reboot; after a WSL distro restart the PID space restarts low, so a recycled PID (or another user's process → `PermissionError` → also "alive", `config.py:152-154`) makes `try_acquire_pid_file` return False forever. Sync/import/backfill silently never run again, with only a DEBUG "skip" line as evidence.
2. **Spawner kept the race the config was hardened against** (`hooks/memory_setup.py:47-77`): O_CREAT|O_EXCL create → `Popen` → write child PID. `config.py:110-116`'s docstring names this exact empty-marker window as the reap-into-two-holders hazard; the link-based fix landed only in config.py. A concurrent SessionStart reads the empty marker, gets `int('') → ValueError`, classifies it stale, unlinks the **live** marker, and spawns a second concurrent import / model load.
3. **Probe-then-unlink reap race** (`config.py:145-151`, same shape in memory_setup): contender A probes a dead PID; before A's `unlink`, contender B reaped and linked a fresh live marker; A unlinks B's marker → two holders → doubled onnxruntime peaks (extends #163 via same-key contention).
4. **Asymmetric acquire/release on import** (`cli/commands.py:104` vs `import_conversations.py:285-287`): the CLI import command never acquires the marker but `run()`'s `finally` unconditionally removes it — a manual `ccrecall import` both runs concurrently with a hook-spawned one and deletes its marker on exit.

**Fix direction:** one mechanism, not four patches — `flock` on a lock file (kernel-released on process death) eliminates 1–3 outright; acquisition moved inside `run()` (skip-not-queue, exit 0) fixes 4 for both callers.

### F6 — MED (probe-confirmed): injected context duplicates exchanges when `exchange_count` disagrees with the stored messages

`build_context_summary_json` (summarizer.py:224) decides the first/last split by `len(exchanges)` — derived from **stored** messages — while `render_context_summary` (summarizer.py:337) picks the layout by `exchange_count` — derived from the **raw transcript** by `compute_branch_metadata`. The two disagree whenever a user turn is counted but never inserted, and that is common: slash-command turns (content stripped to empty by the `<command-name>` cleanup — confirmed non-`isMeta` in real transcripts, so they are counted) and image-only turns (no text, no tool_content → `build_message_row` returns None).

Probe-confirmed: with `exchange_count=9` and 7 stored exchanges, the JSON puts all 7 in `last_exchanges` (≤ 8 rule) but the renderer takes the long-session branch (9 > 8) and renders the first two exchanges **twice** — once in "Where We Left Off", again in "Earlier in This Session" — plus a phantom `[... N earlier exchanges ...]` line when the difference exceeds the overlap.

**Fix direction:** make both sides key off the same count (pass `len(exchanges)` through the JSON, or store it in metadata as the render key); separately consider whether command-only turns should count as exchanges at all.

### F7 — MED: `memory_context` crashes on the exact fault it exists to report

`memory_context.py:76-77` calls `load_settings()`/`setup_logging()` unguarded; `setup_logging` (`config.py:258-267`) opens the log file eagerly, so an unwritable/ENOSPC `~/.ccrecall` raises before stdin is read — the hook dies with no stdout envelope, and `proactive_alert_block`'s `ALERT_CANT_PERSIST` path (deliberately evaluated "before every early-return gate") never runs. `memory_setup.py:133-145` and `clear_handoff.py:23-32` both guard this exact failure with a fallback logger; the alert deliverer is the one hook missing the guard.

**Fix direction:** copy the setup/clear-handoff guard pattern.

### F8 — MED: `recent --sort-order asc` returns the oldest sessions in the DB, not recent sessions oldest-first

`recent_chats.py:51-53`: `ORDER BY b.ended_at {order} LIMIT ?` sorts before limiting, so `asc` selects from the wrong end of history — `ccrecall recent -n 3 --sort-order asc` on a year-old DB returns three year-old sessions presented as recent context. The `asc` path has zero test coverage. Semantics bug, not naming (adjacent to #146 but distinct).

**Fix direction:** select `desc LIMIT n`, reverse in Python for `asc`.

### F9 — MED (conditional): legacy DBs missing enumerated `branches` columns can never be opened again

`db_base.py:120-136`: `_migrate_to_v1`'s INSERT…SELECT names `tool_counts`, `context_summary_json`, `summary_version_at_embed`, `embedding_model`, … — a pre-v1 DB whose `branches` predates any of them gets "no such column" → ROLLBACK → raise, on **every** subsequent open. Hooks swallow the exception and print `{}`, so recall goes dark with only log evidence. The codebase asserts this population exists — `db.py:63` `has_tool_counts` ("absent on old DBs") still guards two read paths — but those guards are unreachable on a DB current code can't open. Either such DBs are real (v1 needs per-column existence handling) or they aren't (`has_tool_counts` is dead code); it can't be both. Distinct from #159 (drifted constraints) — this is unopenable-vs-drifted.

### F10 — MED: one bad row permanently wedges the auto-spawned summary backfill, silently

`hooks/backfill_summaries.py:72` catches only `(ValueError, TypeError, KeyError)` per row; anything else (`IndexError`, `AttributeError` on malformed branch data) falls to the batch-level `except Exception` (`:81-86`) → commit + return. `run()` returns None so the process **exits 0**; the log line is `logger.error(..., e)` with no traceback (contra logging.md's `exception()` rule). Selection has no exclusion set and no stuck guard (this is the one backfill *not* ported to `run_batch_loop`), so the next SessionStart's `_needs_backfill` check re-spawns it, it hits the same row, and aborts again — every session, forever, with everything behind that row never summarized. The three backfills each define a *different* content-error taxonomy (embeddings: `ValueError/OverflowError/UnicodeError`; tool-content: `JSONDecodeError/ValueError/TypeError/KeyError`; summaries: `ValueError/TypeError/KeyError`) — none includes `IndexError`.

**Fix direction:** invert the taxonomy (infra = `sqlite3.Error`/`OSError` aborts; any other per-row exception marks the sentinel and moves on), use `logger.exception`, and port to `run_batch_loop`.

### F11 — MED-LOW: every sync re-extracts tool content for every already-stored message

`message_ops.py:124-149` (`update_missing_tool_content`) runs `extract_text_content` plus a no-op `UPDATE … WHERE tool_content IS NULL` for **every existing message on every sync**. The repair is only ever needed for pre-v4 rows, and the partial index `idx_messages_tool_content_null` makes the pending set one cheap query (`pending_tool_content_uuids` already exists in `import_log_ops.py:24`). For a long session syncing on every Stop hook, this is O(session length) redundant extraction per Stop, forever. Extends the #160 theme (dead work on the ingest path) with a distinct mechanism.

**Fix direction:** query the pending set first; skip the loop when empty.

### F12 — LOW-MED: pending-question false positive when the user moved on via a slash command

`tail_pending.py:114-116`: after a rejected AskUserQuestion, only a later `typed_instruction` cancels pendingness — but a slash-command turn strips to empty, so it doesn't count. The next SessionStart injects "⚠ Unresolved Decision From Prior Session / do not act on the work it gates" even though the user resolved it by moving on.

**Fix direction:** treat a command-wrapper user entry (or subsequent main-chain assistant activity after a non-error user turn) as "moved on".

### F13 — LOW cluster (each verified, small blast radius)

- **Non-deterministic exchange ordering:** `fetch_branch_messages` / `compute_context_summary` order by `m.timestamp` alone; same-second ties have unspecified order, so exchange pairing can flip between syncs → `content_hash` churn → gratuitous re-embeds. Add `m.id` as tiebreaker (db_vec.py:112, summarizer.py:425, parsing.py:346).
- **Tool-marker collapsing skips MCP tools:** `summarizer.py:63` `_TOOL_MARKER_RE` uses `\w+`, which fails on hyphenated names (`mcp__home-assistant__…`) — runs of MCP calls never collapse, causing exactly the prose dilution the collapser was built to prevent.
- **LIKE rung doesn't escape `%`/`_`:** `search_query.py:160` interpolates raw terms while the scope filters four lines away use `escape_like`. Over-matching only, fallback rung only.
- **Tail selector ambiguity resolved silently:** `tail_resolve.py:113-141` substring-anywhere match, first-by-recency wins, no warning on multiple matches. Prefer prefix match + a stderr note.
- **`extract_commits` quoting:** `content.py:191` captures `it` from `-m "it's done"` and misses heredoc-style commit messages entirely (the dominant Claude Code commit shape); also substring-matches `git commit` anywhere in a command. Metadata quality only.
- **Handoff stale-guard bypass:** `session_selection.py:192` — a handoff JSON missing `timestamp` skips the 30-second stale check entirely, while a *bad* timestamp is correctly treated as invalid. Treat missing like bad.
- **`load_vec=True` isn't a guarantee:** `db.py:145` discards `ensure_vec`'s return; callers get a vec-less connection with only a log WARNING. Downstream re-guards make this degraded-not-broken today.
- **Clear-handoff/other clean-area notes:** snooze-ledger read-modify-write races across concurrent session starts are benign (duplicate alert paragraph, last-writer-wins) — noted, no action.

### F14 — LOW cluster (backfill/CLI surface)

- **`ccrecall status --check-ingestion` opens a writable, silently-migrating connection:** `status.py:120` uses `get_connection` (applies schema deltas unconditionally) from a command documented as read-only-plus-cache-writes — a status invocation can rewrite the DB schema, which is exactly the surface #161 worries about (extends #161).
- **Status over-counts backfillable sessions:** `tool_content_status.py:34-36` counts a session with no `import_log` rows as backfillable, but `backfill_tool_content.build_filepath_index` only indexes sessions that have import_log rows with surviving files — the run reports it MISSING, so `--status` and the run's skip counts never reconcile.
- **Manual `ccrecall backfill summaries` exits 0 on failure:** `backfill_summaries.py:89-91` logs and returns None on DB-connect failure; `cli/commands.py:133` discards it. Embeddings and tool-content honor an `EXIT_ABORT` contract for exactly this reason; a cron/systemd invocation of summaries can never observe failure.
- **Global `--json` silently ignored by `tail`:** `cli/__init__.py:83-84` injects `ctx` only into commands declaring it; `cmd_tail` doesn't, yet accepts the flag and emits markdown with no warning (extends #146).
- **`--progress-every` lacks the `Number(gte=1)` validator its sibling flags have** (`cli/commands.py:220-222`, `:281-283`): 0 or negative degrades to a progress line per item (extends #146).
- **Embeddings `--limit` counts only successes:** `backfill_embeddings.py:188/192` — `--limit N` can process arbitrarily more than N branches when errors cluster, and `:261`'s completion `remaining` counts freshly-errored branches as still pending. Defensible semantics, but undocumented and inconsistent with tool-content's mid-batch break.

## Finding → issue map (filed 2026-09-02)

| Finding | Severity | Issue |
|---|---|---|
| F1 — setup hook eager-loads embedding stack (+ tripwire test) | HIGH | [#169](https://github.com/NodeJSmith/claude-code-recall/issues/169) |
| F2 — import has no per-file exception containment | HIGH | [#170](https://github.com/NodeJSmith/claude-code-recall/issues/170) |
| F3 — `message: null` / `text: null` pass validation then crash | MED-HIGH | [#171](https://github.com/NodeJSmith/claude-code-recall/issues/171) |
| F4 — no tripwire for transcript-shape drift | MED | [#172](https://github.com/NodeJSmith/claude-code-recall/issues/172) |
| F5 — PID-file mechanism unsound four ways → flock redesign | MED | [#173](https://github.com/NodeJSmith/claude-code-recall/issues/173) |
| F6 — context summary duplicates exchanges on count divergence | MED | [#174](https://github.com/NodeJSmith/claude-code-recall/issues/174) |
| F7 — memory_context dies unenveloped on unwritable data dir | MED | [#175](https://github.com/NodeJSmith/claude-code-recall/issues/175) |
| F8 — `recent --sort-order asc` returns oldest sessions | MED | [#176](https://github.com/NodeJSmith/claude-code-recall/issues/176) |
| F9 — pre-v1 DBs crash-loop the v1 migration forever | MED | [#177](https://github.com/NodeJSmith/claude-code-recall/issues/177) |
| F10 — summary backfill wedges forever on one bad row | MED | [#178](https://github.com/NodeJSmith/claude-code-recall/issues/178) |
| F11 — per-sync re-extraction of tool content for all messages | MED-LOW | [#179](https://github.com/NodeJSmith/claude-code-recall/issues/179) |
| F12 — stale "Unresolved Decision" after slash-command move-on | LOW-MED | [#180](https://github.com/NodeJSmith/claude-code-recall/issues/180) |
| F13 — core/retrieval LOW cluster (7 items) | LOW | [#181](https://github.com/NodeJSmith/claude-code-recall/issues/181) |
| F14 — backfill/status LOW cluster (3 items) | LOW | [#182](https://github.com/NodeJSmith/claude-code-recall/issues/182) |
| F14 — `--json tail`, `--progress-every` validator | LOW | comment on [#146](https://github.com/NodeJSmith/claude-code-recall/issues/146) |

## Observations (not defects)

- Track B `score_raw = 1.0 - dist` (`search_vector.py:269`) goes negative routinely (L2 on unit vectors spans [0,2]); display normalization hides it, but JSON consumers may misread negative raw scores.
- Min-max normalization guarantees the bottom search card always reads 0.00 even when genuinely relevant — trains readers to over-discount it.
- A long v1/v2 rebuild inside `BEGIN IMMEDIATE` can exceed the 5 s busy timeout and crash concurrent openers for its duration — one-time, self-healing.
- `sort_session_files` re-parses every sibling file the import will parse again — perf only.
- CLAUDE.md's invariant-4 rationale for retaining `is_active` ("guard against pre-existing inactive rows") is vestigial: `_migrate_to_v1` deletes all inactive rows before the rebuild, so none survive. The filters are harmless; the stated reason is stale.

## Clean areas (verified, not assumed)

- **Embedding watermark protocol** (`embed_ops.py`): clear-first/set-last ordering, vector-before-bookkeeping in `write_chunk_embedding`, the cap-quality predicate's subtle `ed["cap_tokens"]`-not-`cap_limit` distinction, idempotent watermark repair, prune-only-missing-indices, sync-vs-backfill cap tiers with raw-text content hashing (no ping-pong) — all consistent under crash-window analysis.
- **Memory budget** (`embeddings.py`): `_plan_embed_batches` attention-area math correct (longest-first, budget honored, every index exactly once); `cap_for_embedding` terminates and is idempotent at the margin boundary.
- **Migration gate** (`db_base.py`): double-checked `user_version` under `BEGIN IMMEDIATE`; additive v3–v8 correctly ordered before reshapes; deferred `_prepare_vec` honors invariant 3.
- **`get_connection`** context manager: commit/rollback/close correct on all paths.
- **FTS5 sanitization** (probed live): operator soup, unicode, quotes — inert, never a syntax error. RRF fusion, bm25 sign handling, KNN adaptive retry ceilings, session dedup, score normalization: correct.
- **Path safety** (`transcript_sources.py`, probed): traversal and glob metacharacters neither raise nor escape `projects_dir`; symlinks surfaced, not followed; cycle-safe walks.
- **`extract_tool_strings`/`build_tool_use_marker`** (probed): total on malformed input; depth/item/field caps enforced.
- **Hook stdout purity**: every inspected path in the wired hooks prints only the envelope (sole gap is F7's crash-before-output).
- **`sync_current.py`**: PID self-acquire/release correct on all exit paths; per-session exception containment; UUID validation blocks traversal.
- **`session_selection.py`**: `is_active = 1` everywhere; worktree-normalized handoff matching; cwd-mismatch preserves the handoff for its rightful claimant.
- **health.py**: hot-path invariant honored (empirically import-clean); atomic sidecar writes; lock-contention-as-success in `probe_db`.
- Upsert/dedup core: `upsert_branch` vs `UNIQUE(session_id)` interplay safe post-v1 (all rows active, `find_all_branches` returns exactly one active branch); import_log NULL-hash asymmetry preserved; tool-content repair self-heals.
- **Backfill machinery**: `run_batch_loop` stuck detection sound for both consumers (deterministic selection, exclusion set chunked under the 900-param limit, no re-select-forever path — the empty-exchange-branch livelock hypothesis was chased and closed by the trivially-true watermark stamp); cap_tokens upgrade logic has no evasion and no ping-pong; savepoint/transaction boundaries correct and correctly different per backfill; `try_acquire_pid_file` itself is exemplary (F5 is about the callers that bypass it); ETA/progress arithmetic division-guarded; ingestion fingerprint TOCTOU direction conservative.

## Cross-checked, not re-reported

#45 (tail mtime wrong-file pick), #53, #66–#68, #101, #108–#109, #116–#117, #127, #131, #146, #157 (v2 orphan-DELETE data loss), #158 (INSERT OR IGNORE masking), #159 (branches DDL drift — confirmed still present: `schema.py:68` `UNIQUE(session_id, leaf_uuid)` vs `db_base.py:117,173` `UNIQUE(session_id)`), #160 (dead tool_summary), #161 (hook/CLI version skew), #163 (sync∥backfill concurrency), #164 (sidecar clear), #165, #168 (partial import). #166/#167 verified landed on main.
