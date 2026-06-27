# Context: ccrecall Surfacing Model (self-heal / reactive / proactive)

## Problem & Motivation
ccrecall is meant to be invisible — it runs in background hooks and Claude consumes its output. The acute felt failure: the user gets **no proactive notice when embeddings are missing or failing** (sqlite-vec won't load, the fastembed model can't download, the pipeline errors out). Semantic recall silently degrades and she only finds out if she happens to run a recall. A rarer but worse instance of the same shape: a working install can **silently stop persisting anything** (disk full, permission change, DB corruption) — the hooks swallow the error into a log that's off by default, so ccrecall can't even tell itself it failed. This work gives both conditions a coherent, low-noise surfacing path instead of a fifth bespoke nudge.

## Visual Artifacts
None.

## Key Decisions
1. **Three tiers, by invasiveness.** Tier 1 self-heal (existing ~7 modes — do not regress, no code). Tier 3 proactive interrupt (the core build). Tier 2 reactive caveat (small).
2. **Proactive tier = two alert classes sharing one told-once-snooze mechanism:** (a) *embeddings failing*, (b) *cannot persist data*.
3. **Split detection by failure type to protect the hot path.** Write-failure is detected **actively** at SessionStart (cheap: fixed-path marker write+unlink, plus `BEGIN IMMEDIATE`/`ROLLBACK` on the already-open DB conn). Embedding-failure is detected **passively** — the detached embedding process (which already loads vec + model) records capability failures to a sidecar; SessionStart only *reads* it, so it never imports vec/fastembed on the ~440ms hook path.
4. **Told once, then snoozed ~24h (wall-clock), auto-clears on resolution.** Session-count snooze was rejected — multi-pane/orchestrator-child usage collapses it in minutes. Tracked in an atomic `alert-snooze.json` sidecar; one config key overrides the window.
5. **Two separate sidecars, one writer-class each** (`embedding-status.json` ← embedding process; `alert-snooze.json` ← SessionStart hooks) to avoid cross-concern read-modify-write races under concurrency.
6. **Severity-as-intent, not hard-coded loudness.** The injected block mirrors `format_pending_block(for_injection=True)`: a `## ⚠` heading + prose intent (cause + action + "surface this; don't hard-code prominence"); the assistant decides how loudly to raise it.
7. **`memory_context.main` requires restructuring, not a prepend** — alerts must fire even when `sessions` is empty or the DB connection itself fails (today both short-circuit to `_emit_empty()` before any alert is built).
8. **Low coverage stays reactive/silent** — surfaced only inside a recall the user runs (caveat below a 0.95 coverage threshold or when embeddings are unavailable), never a proactive nag.

## Constraints & Anti-Patterns
- **Never load vec/fastembed/onnxruntime on the SessionStart hook path.** Embedding-failure is read from a sidecar, never probed inline. (Hot-path ~440ms invariant, CLAUDE.md.)
- **A busy/locked DB timeout is NOT a persist failure.** Never raise the write-failure alert from lock contention — distinguish it from `SQLITE_READONLY`/`CORRUPT`/`FULL`/`OSError`.
- **No per-session repetition** of a proactive alert (the alert-fatigue anti-pattern), except the dir-unwritable degradation where the snooze itself can't persist.
- **All new surfacing logic is defensively wrapped** — a failure in a probe, sidecar read, block builder, or caveat must degrade to "no alert / no caveat" and never break a hook or a recall (the `_pending_question_block` / `log_hook_exception` precedent).
- **Hook stdout** emits only `{"continue": true}` / `{}`; alerts go in `additionalContext`.
- **Non-goals (do NOT build):** a general severity×delivery registry / migrating the 4 existing surfaces onto a shared spine; retrying the `-1` CONTENT_ERROR sentinel; a user-facing doctor/stats health UI as primary surface; proactively nagging low coverage; configurable severity floors / mute lists / per-alert dismissal commands.
- **House rules:** no `from __future__ import annotations`, no lazy imports (imports at top), `X | None` not `Optional[X]`, `whenever` for all date/time (24h snooze math), setuptools.

## Design Doc References
- `## Problem` — the felt pain (no proactive notice of failed embeddings) and the silent-data-loss case.
- `## Architecture` — the three tiers, the active-probe vs passive-record split, the sidecar schema, the `memory_context.main` restructuring, severity-as-intent.
- `## Edge Cases` — lock-timeout-vs-fault, concurrent sidecar writers, dir-unwritable-blocks-its-own-snooze, stale embedding-status, onboarding interplay.
- `## Key Constraints` — the hard prohibitions (hot path, busy-timeout, no per-session repeat, defensive wrap).
- `## Impact → Changed Files` / `### Behavioral Invariants` — the modify/create inventory and the ~7 self-heal behaviors + hook invariants that must not regress.

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
The **writability probe must NOT use `O_EXCL`** — it uses `O_CREAT | O_TRUNC` so it is idempotent and survives a stale marker left by a crash between write and unlink.

### Defensive hook guard (every new surfacing path must follow this)
**Source:** `src/ccrecall/hooks/memory_context.py:84` (`_pending_question_block`)
```python
    except Exception:
        # Deliberately broad: this optional warning must never break the
        # SessionStart hook or drop the main context injection.
        logging.getLogger(LOGGER_NAME).exception("pending-question block failed")
        return ""
```

### Settings merge over defaults (how the snooze key is added)
**Source:** `src/ccrecall/db.py:299`
```python
def load_settings() -> dict:
    settings = DEFAULT_SETTINGS.copy()
    config = load_config()
    for key in DEFAULT_SETTINGS:
        if key in config:
            settings[key] = config[key]
    return settings
```
