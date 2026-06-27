# Design: ccrecall Surfacing Model (self-heal / reactive / proactive)

**Date:** 2026-06-27
**Status:** approved
**Scope-mode:** reduce
**Research:** prior-art survey (background-dev-tool alerting) + codebase failure-mode audit, summarized in `~/.claude/projects/-home-jessica-source-claude-code-recall/memory/next-steps-plan.md`

## Problem

ccrecall is meant to be invisible — it runs in background hooks, Claude consumes its output, and the user (Jessica) wants to forget it exists. The acute, *felt* failure is that **she has no proactive notice when embeddings are missing or failing.** Semantic recall silently degrades — the sqlite-vec extension fails to load, the fastembed model can't download, or the embedding pipeline errors out — and nothing tells her. She only discovers it if she happens to run a recall and notices poor results. By then the gap may be weeks old.

A second, latent instance of the same shape is worse but rarer: a previously-working install can **silently stop persisting anything** (disk full, `~/.ccrecall` permission change, DB corruption). The hooks swallow these errors into a log that is off by default (`logging_enabled=False`), so ccrecall cannot even tell *itself* a failure recurred, let alone tell the user. A working install can lose history indefinitely with zero signal.

Underneath both: ccrecall has no coherent model for *what* to surface, *when*, and *how loud*. It has four ad-hoc injection surfaces (onboarding nudge, prior-session context, pending-question warning, migration notice), each with bespoke show/hide logic, no severity concept, and no shared mechanism. Adding a fifth bespoke nudge for embeddings would compound that — and risk the alert-fatigue anti-pattern (a per-session coverage nag the user tunes out).

## Goals

- The user gets **proactive notice at session start** when the embedding pipeline is persistently broken (not merely catching up) — directly addressing the felt pain.
- The user gets **proactive notice at session start** when ccrecall persistently cannot persist data — the silent-history-loss case.
- Neither notice nags: each is told **once**, then suppressed for ~24h, and **auto-clears** the moment the condition resolves. No per-session repetition, no alert fatigue.
- The notices reach the user through ccrecall's only channel (SessionStart context injection) **without colliding** with the existing surfaces, and without ccrecall hard-coding how loudly the assistant must raise them.
- Lower-stakes embedding state (coverage merely catching up) stays **silent and reactive** — surfaced only inside a recall the user actually runs, never as a proactive nag.
- The hot-path hook cost invariant (~440ms, no eager vec/fastembed import — see CLAUDE.md) is preserved.
- The ~7 existing self-healing behaviors are documented as invariants and not regressed.

## Non-Goals

- **A general severity×delivery alert registry** that the four existing surfaces migrate onto. The model below is compatible with that future, but this change does not refactor onboarding/migration/pending-question/context onto a shared spine. (Deferred — that's the Expand version.)
- **Retrying the `-1` CONTENT_ERROR sentinel.** Chunks/summaries marked `embedding_version = -1` / `summary_version = -1` are excluded forever (`IS NOT -1`); adding a periodic/version-bump retry is a separate low-priority self-heal follow-up. It degrades one branch's coverage, not the system. Tracked in Open Questions / follow-up, not built here.
- **A user-facing `doctor`/`stats` health UI as the primary surface.** Those remain diagnostic-only; the design assumes the user will never proactively run them.
- **Proactively nagging about low coverage.** Coverage that is climbing is self-healing; it stays reactive.
- **Configurable severity floors, mute lists, or per-alert dismissal commands.** Dismissal = fix the underlying problem (or wait out the 24h snooze). Only one tunable is added (snooze window), with a default tuned so the user never touches it.

## User Scenarios

### Jessica: ccrecall user who wants to forget it exists
- **Goal:** be told — once, unobtrusively — only when something is actually broken and only she can fix it.
- **Context:** starts Claude Code sessions throughout the day across multiple panes / worktrees / machines.

#### Embedding pipeline is broken
1. **Starts a session after sqlite-vec stopped loading (or the model download is blocked).**
   - Sees: a single `## ⚠` block at the top of the injected context telling the assistant that ccrecall's embedding pipeline is failing, why (e.g. "vector extension unavailable" / "embedding model unavailable"), and the likely fix — and instructing the assistant to relay it to her in prose.
   - Decides: whether to fix now or later.
   - Then: the assistant raises it conversationally. For the next ~24h, subsequent session starts do **not** repeat it.
2. **Fixes the environment; starts another session.**
   - Sees: nothing. The condition cleared, so the alert is gone — no "resolved" message, just silence.

#### History is silently not persisting
1. **Starts a session while the disk is full / `~/.ccrecall` is unwritable.**
   - Sees: a single `## ⚠` block telling the assistant ccrecall could not write to its store this session, that history/embeddings may not be saved, and the likely cause (disk space, permissions) — and to relay it.
   - Decides: free space / fix permissions.
   - Then: told once; if still broken in a later session and the snooze persisted, suppressed until the window lapses; if the fault prevents persisting the snooze itself, re-told each session (acceptable for the most severe state).

#### Embeddings are merely catching up (no proactive notice — reactive only)
1. **Runs `/ccr-recall` while backfill is at 70%.**
   - Sees: results, plus a one-line caveat that some history isn't embedded yet so results may be partial.
   - Then: no session-start nag now or later about this — it self-heals.

## Functional Requirements

- **FR#1** At SessionStart, ccrecall performs an active writability probe of its runtime directory (`~/.ccrecall`) by writing and deleting a fixed-path marker file.
- **FR#2** At SessionStart, ccrecall performs an active writability probe of the conversations DB on the connection already opened for context selection, using a write-lock acquisition that mutates no data, and treats a busy/locked timeout as success (not a fault).
- **FR#3** When either writability probe fails with a hard error (permission denied, disk full, read-only, corruption, or other OS/SQLite error that is not a lock timeout), ccrecall raises a "cannot persist data" proactive alert.
- **FR#4** The detached embedding process (current-session sync and backfill) records an embedding-capability-failure status to a sidecar file when embedding cannot proceed for a structural reason (vector extension unavailable, embedding model unavailable, or repeated embedding errors), including a machine-readable reason.
- **FR#5** The detached embedding process clears the recorded embedding-capability-failure status once embedding succeeds.
- **FR#6** At SessionStart, ccrecall reads the embedding-capability-failure status from the sidecar (without loading the vector extension or embedding model on the hook path) and, if a failure is recorded, raises an "embeddings failing" proactive alert.
- **FR#7** Each proactive alert is told at most once per snooze window (default ~24h, wall-clock): after it fires, it is suppressed until the window lapses, even if the condition persists across sessions.
- **FR#8** The snooze window is tracked per alert key in a sidecar file written atomically (temp file + atomic replace), safe under concurrent SessionStart processes.
- **FR#9** A proactive alert auto-clears: when its underlying condition no longer holds, no alert is surfaced and its snooze record is reset, so a future recurrence surfaces immediately rather than being suppressed by a stale record.
- **FR#10** When the runtime directory is itself unwritable (so the snooze record cannot be persisted), the "cannot persist data" alert is surfaced every session until the fault clears.
- **FR#11** A surfaced proactive alert is injected as a Markdown block carrying severity + intent + likely cause + suggested action, instructing the assistant to relay it to the user in prose and not to hard-code prominence — mirroring the existing pending-question block.
- **FR#12** When a proactive alert is surfaced, it appears ahead of the prior-session context and the pending-question block in the single SessionStart injection (highest-attention position).
- **FR#13** When more than one proactive alert is active in the same session, they are combined into a single injected block (one message at a time), not multiple separate injections.
- **FR#14** The snooze window length is overridable via a `config.json` key merged through `load_settings`, with a default that requires no user action.
- **FR#15** The recall path (`/ccr-recall` / the search command) appends a one-line caveat to its results when, computed at query time, either embeddings are unavailable (degraded to keyword) or branch-grain coverage (`embedded / total`) is below a named threshold constant (default 0.95); at or above the threshold on an embeddings-available install, no caveat is appended.
- **FR#16** The proactive-alert and reactive-caveat logic never raises out of a hook or command — any failure in surfacing degrades to "no alert" / "no caveat" and never blocks session start or a recall.

## Edge Cases

- **Lock-timeout vs. real fault (FR#2):** a `BEGIN IMMEDIATE` that times out because another session holds the write lock is normal concurrency, not a persist failure — must be distinguished from `SQLITE_READONLY`/`CORRUPT`/`FULL`/`OSError`. Misclassifying it would fire false alerts during heavy multi-pane use.
- **Concurrent SessionStart writers to the snooze sidecar:** multiple panes start at once. Atomic replace makes each write all-or-nothing; last-writer-wins on a snooze timestamp is acceptable (advisory data — a lost update re-fires or re-suppresses at most once).
- **Two sidecar concerns, two writer classes:** embedding-capability status is written only by the detached embedding process; the snooze ledger is written only by SessionStart hooks. Keeping them in **separate files** avoids interleaved read-modify-write of one file by two different concerns.
- **Runtime dir unwritable blocks its own snooze record:** covered by FR#10 — degrade to re-tell-every-session rather than silently dropping the most severe alert.
- **Stale embedding-failure record:** if the embedding process died before clearing the status but embeddings actually work now, the next successful embed clears it (FR#5); if no embed runs, the status could linger. Mitigation: SessionStart treats the recorded reason as advisory and the next embedding attempt is authoritative.
- **Onboarding not yet complete:** a write failure is plausibly *why* onboarding can't save config; the writability probe should still run and surface, since it explains the blockage. (Embedding alerts are moot pre-onboarding — embedding doesn't run until configured.)
- **Coverage exactly mid-backfill:** must not trip the proactive "embeddings failing" alert — that alert keys off *capability/structural failure recorded by the embedding process*, not off a coverage percentage.
- **Marker file left behind by a crash between write and unlink:** fixed path, `O_TRUNC` on next write, `unlink(missing_ok=True)` — a leftover marker is harmless and overwritten.

## Acceptance Criteria

- **AC#1** With `~/.ccrecall` made unwritable (chmod) on an otherwise-healthy install, starting a session injects a single `## ⚠` "cannot persist data" block ahead of any prior-session context. (FR#1, FR#3, FR#12)
- **AC#2** With the DB file made read-only, starting a session injects the "cannot persist data" block; with a second pane started concurrently holding a write lock, no false alert fires from the lock contention alone. (FR#2, FR#3, edge: lock-timeout)
- **AC#3** With the vector extension forced unavailable, after the embedding process runs once and records the failure, the next session injects a single `## ⚠` "embeddings failing" block naming the reason. (FR#4, FR#6)
- **AC#4** After a proactive alert fires, starting another session within the snooze window injects no proactive block; starting one after the window lapses (or after clearing+recurrence) injects it again. (FR#7, FR#8, FR#9)
- **AC#5** After the underlying condition is fixed, the next session injects no proactive block and the snooze record for that key is reset. (FR#5, FR#9)
- **AC#6** With the runtime dir unwritable so the snooze cannot persist, consecutive sessions each surface the "cannot persist data" alert. (FR#10)
- **AC#7** With both a write failure and an embedding failure active, a single combined injected block is produced, not two. (FR#13)
- **AC#8** Running a recall while embeddings are unavailable, or while coverage is below the threshold constant (default 0.95), appends a one-line caveat to the results; running it on an embeddings-available install at/above the threshold appends no caveat. (FR#15)
- **AC#9** A forced exception inside the probe / sidecar read / caveat path leaves session start and recall functioning normally with no injected alert / no caveat. (FR#16)
- **AC#10** The SessionStart hook path does not import or load fastembed/onnxruntime/sqlite-vec to evaluate the embedding alert (verified by import inspection / timing). (FR#6, hot-path invariant)
- **AC#11** Setting the snooze-window key in `config.json` changes the suppression duration; absent the key, the default applies. (FR#14)
- **AC#12** A surfaced proactive block contains, beyond the `## ⚠` heading, prose carrying the likely cause, a suggested action, and an explicit instruction to relay it to the user without hard-coding prominence — a bare heading with no intent/cause/action prose is a failure. (FR#11)

## Key Constraints

- **Do not load the vector extension or fastembed/onnxruntime on the SessionStart hook path.** The embedding alert must be evaluated by *reading a sidecar*, not by probing capability inline. Violating this regresses the ~440ms hot-path invariant (CLAUDE.md).
- **A busy/locked DB timeout is not a persist failure.** Never raise the write-failure alert from lock contention.
- **No per-session repetition.** A proactive alert that re-injects every session is the alert-fatigue anti-pattern this design exists to avoid (except the FR#10 dir-unwritable degradation).
- **ccrecall supplies severity + intent; the assistant decides prominence.** Do not hard-code an exact user-facing sentence the assistant must emit — inject intent like the pending-question block does.
- **Surfacing failures must never break a hook or a recall.** All new logic is wrapped defensively (the `_pending_question_block` / `log_hook_exception` precedent).
- **No `from __future__ import annotations`, no lazy imports, `X | None` not `Optional`, `whenever` for time, setuptools.** (CLAUDE.md / global rules.)

## Dependencies and Assumptions

- **SessionStart hook injection** is the only user channel (`hooks/memory_context.py`). Assumes the harness continues to honor `hookSpecificOutput.additionalContext`.
- **The detached embedding process** (`hooks/sync_current.py`, `hooks/backfill_embeddings.py`) already loads vec + model and already knows when embedding fails — it is the authoritative embedding-failure detector.
- **`~/.ccrecall/` already hosts sidecars** (config.json, the DB, `.pid-*` sentinels, clear-handoff.json), so adding small JSON sidecars beside them is established practice.
- **The atomic-write convention** (`tempfile.mkstemp(dir=...)` + `Path(tmp).replace(target)`) exists in `write_config.py` and `legacy.py` and is reused verbatim.
- Assumes `whenever` for the 24h wall-clock snooze math (stdlib datetime is prohibited).

## Architecture

The model has three tiers; this change builds tiers 2 and 3 and documents tier 1. They are presented below in order of *invasiveness* (tier 1 = no code, tier 3 = the core build, tier 2 = small), not numeric order — tier 3 is described before tier 2 deliberately.

**Tier 1 — Self-heal (document, don't regress).** The ~7 existing auto-recovering modes (PID/temp reaping in `memory_setup.py`, cold-model warm in `warm_model.py`, NULL-hash re-import in `import_conversations.py`, the content-change heal clause + version-bump re-embed in `session_ops.py`, mid-batch crash leaving rows eligible in `backfill_embeddings.py`) are listed as **Behavioral Invariants** below. No code change; they are the reason most failures need no alert.

**Tier 3 — Proactive interrupt (the core build).** Two alert classes share one mechanism:

- A new small module (e.g. `src/ccrecall/health.py`) owns: the sidecar paths/schema, the writability probe, the snooze ledger read/write, and the alert-block builder. Pure functions where possible; one parse/format boundary for the sidecars.
- **Write-failure detection (active, on the hook path):**
  - *Filesystem probe* — `os.open(marker, O_WRONLY|O_CREAT|O_TRUNC, mode)`, write one byte, close, `unlink(missing_ok=True)` against a fixed path under `RUNTIME_DIR`. Reuses the `O_CREAT` idiom from the PID guard (`memory_setup.py:66`).
  - *DB probe* — on the connection `memory_context` already opens, `BEGIN IMMEDIATE` then `ROLLBACK`. Catch `sqlite3.OperationalError`; if it is "database is locked"/"busy" → treat as success; any other `sqlite3.Error`/`OSError` → fault. (WAL + `busy_timeout` already applied by `apply_base_pragmas`.)
- **Embedding-failure detection (passive, off the hook path):** the detached embedding process writes `embedding-status.json` (reason + `since` timestamp via `whenever`) on structural failure and removes/clears it on success. SessionStart only *reads* it.
- **Snooze ledger (`alert-snooze.json`):** `{ alert_key: last_fired_iso }`, written atomically by the SessionStart hook. On each session, for each active alert key: if `now - last_fired < snooze_window` → suppress; else → include and update `last_fired`. Auto-clear (FR#9): when an alert key's condition is not active, drop its ledger entry. Two separate sidecars (`embedding-status.json` written by the embedding process, `alert-snooze.json` written by the hook) — one writer-class each — avoid cross-concern races.
- **Injection integration (`memory_context.main`) — requires restructuring, not just a prepend.** Today `main()` does `if not sessions: _emit_empty(); return` and wraps the whole DB path in `except Exception: _emit_empty()`. Both short-circuit *before* any alert could be built — but a write-failure alert must fire precisely in the cases that trip them (no prior sessions; `get_db_connection()` itself failing because the dir/WAL is unwritable). So the proactive evaluation must move **earlier and out from under the DB path**: run the filesystem probe and read the embedding-status sidecar **before** attempting the DB connection; attempt the DB probe inside its own guard so its failure becomes an alert rather than an empty emit; then assemble output as `directive + proactive + origin + pending + context`, emitting the proactive block **even when `sessions` is empty or the DB is inaccessible** (in which case origin/pending/context are simply absent). If multiple alerts are active, the builder concatenates them into one block (FR#13). The block mirrors `format_pending_block(for_injection=True)`: a `## ⚠` heading, a prose intent line carrying severity + likely cause + suggested action + an explicit "surface this to the user; do not hard-code how loudly" instruction, no hard-coded user sentence. If `auto_inject_context` is off or onboarding incomplete, the writability alert may still surface (it explains why onboarding can't save) — gate decided in implementation, but the probe itself is cheap and unconditional.

**Tier 2 — Reactive caveat (small).** The recall/search path, after assembling results, computes embedding availability at query time (`vec_available` / `branch_embedding_coverage`, already loaded there since search uses vec) and appends one caveat line when embeddings are unavailable or coverage is below a named threshold constant (default 0.95 — defined alongside the other surfacing constants in `health.py`). No new persisted state; independent of tier 3.

**Severity-as-intent.** There is no enum/registry in this Reduce build — "severity" is expressed as *which builder* produced the block and the intent prose inside it, exactly as the pending-question block already does. A future Expand step can formalize a tier→delivery policy and migrate the four legacy surfaces; this design is forward-compatible but does not build it.

## Replacement Targets

No existing code is being replaced. This is additive: a new `health.py` module, new sidecar files, new calls inserted into `memory_context.main`, the detached embedding process, and the recall path. The four existing injection surfaces are untouched (their future consolidation is an explicit Non-goal).

## Migration

No schema or data-model change. The new state lives in two small JSON sidecars under `~/.ccrecall/` created on first write; their absence is the valid "healthy / nothing recorded" state, so there is no migration for existing installs — a pre-existing install simply has no sidecars until a condition first occurs. The conversations DB schema and `token_schema` are not touched (deliberately — the DB may be the thing that is broken).

## Convention Examples

### Severity-as-intent injection block (the template to mirror)
**Source:** `src/ccrecall/session_tail.py:231`
```python
def format_pending_block(payload: dict, *, for_injection: bool = False) -> str:
    """Render a pending-question payload for the CLI (plain) or the hook (markdown)."""
    lines: list[str] = []
    if for_injection:
        lines.append("## ⚠ Unresolved Decision From Prior Session")
        lines.append(
            "The previous session stopped at an AskUserQuestion the user never answered "
            "(rejected, interrupted, or left open — not resolved). Surface it and let the "
            "user decide; do not act on the work it gates or answer it yourself."
        )
        ...
```

### Atomic file write (reuse verbatim for both sidecars — incl. the cleanup block)
**Source:** `src/ccrecall/hooks/write_config.py:42`
```python
fd, tmp_path = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".tmp")
try:
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(config, indent=2) + "\n")
    Path(tmp_path).replace(CONFIG_PATH)
except Exception:
    Path(tmp_path).unlink(missing_ok=True)
    raise
```
The `except` cleanup is not optional — without it any write failure orphans a `.tmp` file in the runtime dir.

### Atomic O_CREAT|O_EXCL exclusive claim — for the PID/lock sentinels, NOT the probe
**Source:** `src/ccrecall/hooks/sync_current.py:137`
```python
lock_fd = os.open(str(pid_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, PID_FILE_MODE)
```
This is the *exclusive-claim* idiom (fails if the file exists) used by the concurrency guards. **The writability probe must NOT use `O_EXCL`** — it uses `O_CREAT | O_TRUNC` so it is idempotent and survives a stale marker left by a crash between write and unlink (see Architecture / Edge Cases). Listed here as the contrasting convention so an implementer doesn't reach for `O_EXCL` on the probe.

### Defensive hook guard (every new surfacing path must follow this)
**Source:** `src/ccrecall/hooks/memory_context.py:84` (`_pending_question_block`)
```python
    except Exception:
        # Deliberately broad: this optional warning must never break the
        # SessionStart hook or drop the main context injection. Log best-effort
        # (no-op unless logging_enabled) so the failure isn't silently lost.
        logging.getLogger(LOGGER_NAME).exception("pending-question block failed")
        return ""
```

### Settings merge over defaults (how the snooze key is added)
**Source:** `src/ccrecall/db.py:299`
```python
def load_settings() -> dict:
    """Return settings with config.json overrides merged on top of defaults."""
    settings = DEFAULT_SETTINGS.copy()
    config = load_config()
    for key in DEFAULT_SETTINGS:
        if key in config:
            settings[key] = config[key]
    return settings
```

## Alternatives Considered

- **Do nothing / keep logging-only.** Rejected: the felt pain is precisely the absence of any signal; `logging_enabled` defaults off and the user never reads the log.
- **Proactively nag low coverage every session.** Rejected as the alert-fatigue anti-pattern; coverage self-heals, so a climbing number is not actionable. Reactive caveat covers the on-demand case.
- **Active capability probe for embeddings on the hook path** (load vec + model at SessionStart). Rejected: violates the ~440ms hot-path invariant. The detached embedding process is already the authoritative detector — passive recording is both cheaper and more accurate.
- **Fire write-failure immediately every session with no persisted state** (the maximal-simplicity option). Rejected by the explicit cadence choice: told-once-then-snooze respects attention; the small snooze ledger is the price.
- **Session-count snooze instead of wall-clock.** Rejected: multi-pane / orchestrator-child usage collapses a session-count window in minutes; 24h wall-clock is robust.
- **One combined sidecar for status + snooze.** Rejected for concurrency: two writer classes (embedding process vs. SessionStart hooks) sharing one file invites lost updates; two single-concern files are simpler to reason about.
- **Full severity×delivery registry now (Expand).** Deferred: out of Reduce scope; the model is forward-compatible.

## Test Strategy

### Existing Tests to Adapt
- `tests/test_session_ops.py` and any memory_context/SessionStart hook tests that assert the exact injected context string will need updating to tolerate (or assert) a leading proactive block when a fault is injected. Audit `tests/` for assertions on `additionalContext` ordering before changing `memory_context.main`.
- Tests asserting `DEFAULT_SETTINGS` contents must add the new snooze-window key.

### New Test Coverage
- Writability probe: dir unwritable, DB read-only, DB lock-timeout-is-not-a-fault, healthy pass. (FR#1, FR#2, FR#3 / AC#1, AC#2)
- Embedding-status sidecar: write-on-failure, clear-on-success, hook reads it without importing vec/fastembed. (FR#4, FR#5, FR#6 / AC#3, AC#10)
- Snooze ledger: fire-once, suppress-within-window, re-fire-after-window, auto-clear-and-reset, atomic write under simulated concurrency. (FR#7–FR#9 / AC#4, AC#5)
- Dir-unwritable degradation re-fires every session. (FR#10 / AC#6)
- Combined block when both alerts active; ordering ahead of pending/context. (FR#12, FR#13 / AC#7)
- Snooze window overridable via config. (FR#14 / AC#11)
- Reactive caveat present when degraded/partial, absent when healthy. (FR#15 / AC#8)
- Defensive degradation: forced exception → no alert/caveat, session/recall still work. (FR#16 / AC#9)

### Tests to Remove
No tests to remove.

## Documentation Updates
- **README** — add a short "When ccrecall speaks up" note: it stays silent unless embeddings are broken or it can't save, and it tells you once.
- **CLAUDE.md** — note the new `health.py` parse/format boundary for sidecars and the rule that embedding-failure is read from a sidecar (never probed) on the hook path, under the "Two invariants to preserve" / Architecture section.
- **CHANGELOG / release notes** — `feat:` entry (handled by release-please via Conventional Commit).
- **config.json documentation** (wherever settings keys are documented, e.g. README/onboarding) — document the new snooze-window key and its default.
- CLI help text: no new commands; the recall caveat needs no help-text change.

## Impact

### Changed Files
- `src/ccrecall/health.py` — **create** — sidecar schema/paths, writability probe, snooze ledger, alert-block builder (the new parse/format boundary for health state).
- `src/ccrecall/hooks/memory_context.py` — **modify** — restructure `main()` so the proactive evaluation runs before the DB path and emits even when `sessions` is empty or the DB connection fails (the current `if not sessions: _emit_empty()` early return and the broad `except: _emit_empty()` both pre-empt alert assembly today); then prepend the combined proactive block ahead of origin/pending/context; defensive-wrapped.
- `src/ccrecall/hooks/sync_current.py` — **modify** — record/clear embedding-capability-failure status to the sidecar at its existing failure/success points.
- `src/ccrecall/hooks/backfill_embeddings.py` — **modify** — same record/clear at its EXIT_ABORT (capability) and success points.
- `src/ccrecall/db.py` — **modify** — add the snooze-window key to `DEFAULT_SETTINGS`; possibly host shared sidecar-path constants beside `RUNTIME_DIR`/`CONFIG_PATH`.
- recall/search path (`src/ccrecall/search_conversations.py` and/or the `/ccr-recall` command in `cli/commands.py`) — **modify** — append the reactive coverage/availability caveat at query time.
- `tests/…` — **create/modify** — coverage per Test Strategy.
- `README.md`, `CLAUDE.md` — **modify** — docs per Documentation Updates.

### Behavioral Invariants
- **Hook stdout** prints only `{"continue": true}` / `{}` (CLAUDE.md invariant 1) — the proactive block goes in `additionalContext`, never to bare stdout.
- **Hook hot path** stays import-light — no vec/fastembed/onnxruntime on the SessionStart path (CLAUDE.md invariant 2 / AC#10).
- The **~7 self-heal behaviors** must keep working: PID/temp reaping, cold-model warm, NULL-hash re-import, content-change heal clause, version-bump re-embed, mid-batch-crash-leaves-row-eligible. New code must not alter these paths.
- Existing surfaces (onboarding, migration, pending-question, prior-context) keep their current behavior; the proactive block is additive and ordered ahead of them.
- The conversations DB schema (a public contract) is not modified.

### Blast Radius
- `memory_context.main` is on every SessionStart — the highest-traffic path. A regression here affects every session; hence the defensive wrapper and the ordering-tolerant tests are mandatory, not optional.
- The detached embedding process changes touch the embedding write path; must not perturb the existing watermark/heal logic.
- The recall caveat changes user-visible recall output; keep it to a single appended line.

## Open Questions

- None blocking. Follow-up (explicitly deferred, not part of this build): add a periodic / version-bump retry for `-1` CONTENT_ERROR chunks/summaries so a one-time content error eventually heals.
