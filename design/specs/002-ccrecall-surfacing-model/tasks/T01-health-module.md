---
task_id: "T01"
title: "Add health.py: probes, snooze ledger, embedding-status, alert builder"
status: "done"
depends_on: []
implements: ["FR#1", "FR#2", "FR#3", "FR#7", "FR#8", "FR#9", "FR#10", "FR#11", "FR#13", "FR#14", "AC#4", "AC#5", "AC#6", "AC#11", "AC#12"]
---

## Summary
Create the new `src/ccrecall/health.py` module — the single parse/format boundary for ccrecall's surfacing state. It owns: the two sidecar paths + schema, the active writability probe (filesystem marker + DB write-lock probe), the embedding-status sidecar read/write helpers, the snooze ledger read/write (atomic), and the `## ⚠` alert-block builder. Also add the snooze-window setting to `DEFAULT_SETTINGS` so it merges through `load_settings`. This is the foundation every other task calls; it ships with full unit tests. No wiring into hooks happens here (that's T03).

## Target Files
- create: `src/ccrecall/health.py`
- create: `tests/test_health.py`
- modify: `src/ccrecall/db.py`
- read: `src/ccrecall/hooks/write_config.py`
- read: `src/ccrecall/hooks/sync_current.py`
- read: `src/ccrecall/session_tail.py`
- read: `tests/test_db.py`

## Prompt
Build `src/ccrecall/health.py` per the design doc `## Architecture` (Tier 3) section. All imports at top of file (no lazy imports); use `X | None`; use `whenever` (`Instant`) for all timestamp math; no `from __future__ import annotations`.

Define these module-level constants (names illustrative — keep them grouped at the top):
- Sidecar paths under `RUNTIME_DIR` (from `db.py`): `embedding-status.json` and `alert-snooze.json`.
- A fixed marker path under `RUNTIME_DIR` for the filesystem probe.
- `RECALL_CAVEAT_COVERAGE_THRESHOLD = 0.95` (consumed by T04).
- Alert keys for the two classes, e.g. `ALERT_CANT_PERSIST` and `ALERT_EMBEDDINGS_FAILING`.

Implement these pure-where-possible functions:

1. **Writability probe** (FR#1, FR#2, FR#3):
   - Filesystem probe: `os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)`, write one byte, close, `unlink(missing_ok=True)`. **Do NOT use `O_EXCL`** — see Convention Examples in context.md (the probe must be idempotent and survive a stale marker). Any `OSError` → fault.
   - DB probe: accept an already-open `sqlite3.Connection` (the caller in T03 passes the one `memory_context` already opened). Run `BEGIN IMMEDIATE` then `ROLLBACK`. Catch `sqlite3.OperationalError`; if its message indicates "locked"/"busy" → treat as **success** (concurrency, not a fault); any other `sqlite3.Error` or `OSError` → fault. (WAL + busy_timeout are already applied by `apply_base_pragmas`.)
   - Return a result that distinguishes ok / fault-with-reason so the block builder can name the likely cause (permissions, disk full, read-only/corrupt). The DB-probe function must accept `conn | None` and report a fault distinctly when the connection itself is absent (the dir-unwritable case where T03 couldn't open the DB).

2. **Embedding-status sidecar** (FR#4/FR#5 are exercised by T02; here build the read/write/clear helpers they call): `read_embedding_status() -> dict | None`, `record_embedding_failure(reason: str)`, `clear_embedding_failure()`. Store reason + a `since` ISO timestamp (`Instant.now()`). Reads must tolerate missing/malformed file → return None.

3. **Snooze ledger** (FR#7, FR#8, FR#9, FR#10): `{ alert_key: last_fired_iso }`. Provide a function that, given the set of currently-active alert keys and the snooze window (seconds/hours from settings), returns the subset that should fire now (active AND not within the snooze window) and updates `last_fired` for those — written atomically (tempfile.mkstemp + `Path(tmp).replace`, with the `except: unlink; raise` cleanup — see Convention Examples). Auto-clear (FR#9): any alert key NOT currently active has its ledger entry dropped, so a later recurrence fires immediately. FR#10: if the ledger write fails because `RUNTIME_DIR` is unwritable, the function must still report the alert as "fire" (degrade to re-tell every session) rather than swallowing it.

4. **Alert-block builder** (FR#11, AC#12): given the active+un-snoozed alert keys (and the embedding-status reason / probe fault reason), build a single Markdown block. Mirror `format_pending_block(for_injection=True)` from `session_tail.py:231`: a `## ⚠` heading, then prose carrying the likely **cause**, a suggested **action**, and an explicit instruction to **surface it to the user and not hard-code prominence**. When multiple alerts are active, concatenate into ONE block (FR#13 is wired in T03, but the builder must accept a list and emit one block). A bare heading with no cause/action/relay prose is wrong.

5. **Settings** (FR#14): add a snooze-window key (e.g. `"alert_snooze_hours"`) to `DEFAULT_SETTINGS` in `db.py` with a sensible default (24). It then merges through the existing `load_settings` loop automatically — see Convention Examples. Update `tests/test_db.py` where it asserts `DEFAULT_SETTINGS` contents.

Write `tests/test_health.py` covering every function: probe ok/fault classification including the lock-timeout-is-success case and the conn-is-None case; embedding-status write/read/clear and malformed-file tolerance; snooze fire/suppress-within-window/fire-after-window/auto-clear-and-reset; atomic write leaves no `.tmp` orphan and tolerates concurrent writers (simulate two sequential writes, assert last-writer-wins, file valid JSON); block builder content asserts cause+action+relay prose present and a single block for multiple alerts; settings default present and overridable. Use `tmp_path` fixtures; monkeypatch `RUNTIME_DIR`/sidecar paths to the tmp dir. Run `uv run pytest tests/test_health.py` and confirm green.

## Focus
- `RUNTIME_DIR`, `CONFIG_PATH`, `PID_FILE_MODE`, `ensure_parent_dir`, `apply_base_pragmas` all live in `src/ccrecall/db.py` — import from there; don't redefine paths.
- The atomic-write idiom and the `O_CREAT|O_TRUNC` vs `O_EXCL` distinction are in context.md Convention Examples — follow them exactly. The blocking comb finding on the source design was precisely an `O_EXCL`/`O_TRUNC` mix-up; do not reintroduce it.
- `whenever` only for time. Match the **established codebase pattern** for delta math: `(Instant.now() - then).total("seconds")` (see `memory_context.py:222-223`) and compare against `snooze_hours * 3600` — do NOT assume an `.in_hours()` method exists (it may not be on the pinned `whenever` version). Parse stored ISO timestamps the same way the codebase already does (`Instant.parse_*` per the existing usage; confirm the exact parser against `memory_context.py`/`session_tail.py`). Do NOT import stdlib `datetime`.
- Distinguishing a SQLite "database is locked" / "database is busy" OperationalError from a real fault is by message substring — there is no dedicated exception subclass. Be tolerant in matching.
- Keep functions pure where possible (return values, no global mutation); the sidecars are the only state. This module must NOT import fastembed, onnxruntime, or sqlite_vec — it only ever reads the embedding-status sidecar, never probes capability (hot-path invariant).
- `tests/test_db.py` asserts the exact `DEFAULT_SETTINGS` dict — adding a key WILL break it; update that assertion in this task.

## Verify
- [ ] FR#1: `test_health.py` proves the filesystem probe returns ok on a writable dir and fault on an unwritable one, using `O_CREAT|O_TRUNC` (idempotent across a pre-existing marker).
- [ ] FR#2: the DB probe returns ok for a healthy conn, ok for a simulated lock/busy OperationalError, and fault for a read-only/corrupt error and for `conn=None`.
- [ ] FR#3: a hard fault from either probe is reported with a reason distinguishable from ok.
- [ ] FR#7: an alert that fired is suppressed when re-evaluated within the snooze window.
- [ ] FR#8: the snooze ledger is written atomically (tempfile+replace, cleanup on error) and stays valid JSON under back-to-back writes.
- [ ] FR#9: an alert key whose condition is no longer active has its ledger entry dropped, and a later recurrence fires immediately.
- [ ] FR#10: when the ledger write fails due to an unwritable runtime dir, the alert is still reported as "fire".
- [ ] FR#11: the block builder emits a `## ⚠` block containing cause, action, and a relay-not-hard-code instruction.
- [ ] FR#13: the block builder accepts a list of active alerts and emits exactly one combined block (the multi-alert concatenation the T03 injection relies on).
- [ ] FR#14: `DEFAULT_SETTINGS` includes the snooze-window key with a 24h default and it is overridable via config (test_db.py updated and green).
- [ ] AC#4: test proves fire-once → suppress-within-window → fire-after-window.
- [ ] AC#5: test proves auto-clear resets the snooze record when the condition clears.
- [ ] AC#6: test proves the dir-unwritable path reports fire every evaluation.
- [ ] AC#11: test proves the snooze duration changes when the config key is set; default applies when absent.
- [ ] AC#12: test proves a built block contains cause/action/relay prose, not a bare heading.
