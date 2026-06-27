"""ccrecall health-state probes, sidecar helpers, snooze ledger, and alert-block builder.

Parse/format boundary for all health-state sidecars:
  - embedding-status.json  (written by the embedding process; read here)
  - alert-snooze.json      (written by SessionStart hooks; read here)

Design: design/specs/002-ccrecall-surfacing-model/design.md § Architecture Tier 3.

Hot-path invariant: this module MUST NOT import fastembed, onnxruntime, or
sqlite_vec — it only ever reads the embedding-status sidecar, never probes
embedding capability inline.
"""

import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from whenever import Instant

from ccrecall.db import PID_FILE_MODE, RUNTIME_DIR, ensure_parent_dir
from ccrecall.models import LOGGER_NAME

# ── Sidecar paths ──────────────────────────────────────────────────────────────
# These are module-level constants so T03 and the embedding process can import
# the canonical paths without re-deriving them.
EMBEDDING_STATUS_PATH = RUNTIME_DIR / "embedding-status.json"
ALERT_SNOOZE_PATH = RUNTIME_DIR / "alert-snooze.json"

# Fixed marker path for the filesystem writability probe (private; inject via
# probe_filesystem(marker_path=...) in tests).
_PROBE_MARKER_PATH = RUNTIME_DIR / ".write-probe"

# ── Named constants ────────────────────────────────────────────────────────────
# Reactive-caveat threshold consumed by T04 (recall path).
# Coverage at or above this fraction suppresses the recall caveat (FR#15).
RECALL_CAVEAT_COVERAGE_THRESHOLD = 0.95

# Alert key constants — one per proactive alert class.
ALERT_CANT_PERSIST = "cant_persist"
ALERT_EMBEDDINGS_FAILING = "embeddings_failing"


# ── Probe result ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ProbeResult:
    """Writability probe outcome: ok or fault with a human-readable reason.

    Frozen so probe results are safe to pass around without accidental mutation.
    """

    ok: bool
    reason: str = ""

    @classmethod
    def success(cls) -> "ProbeResult":
        return cls(ok=True)

    @classmethod
    def fault(cls, reason: str) -> "ProbeResult":
        return cls(ok=False, reason=reason)


# ── Writability probes ─────────────────────────────────────────────────────────


def probe_filesystem(marker_path: Path = _PROBE_MARKER_PATH) -> ProbeResult:
    """Active filesystem writability probe (FR#1).

    Uses O_CREAT|O_TRUNC (deliberately NOT O_EXCL) so the probe is idempotent
    and survives a stale marker left by a crash between write and unlink.  Any
    OSError → fault with the error message as reason.
    """
    try:
        fd = os.open(str(marker_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PID_FILE_MODE)
        try:
            os.write(fd, b"\x00")
        finally:
            os.close(fd)
        marker_path.unlink(missing_ok=True)
        return ProbeResult.success()
    except OSError as exc:
        return ProbeResult.fault(str(exc))


def probe_db(conn: sqlite3.Connection | None) -> ProbeResult:
    """Active DB writability probe via BEGIN IMMEDIATE / ROLLBACK (FR#2).

    Accepts the already-open connection from memory_context (or None when the
    caller couldn't open it — the dir-unwritable case).

    A busy/locked OperationalError is treated as success (normal concurrency,
    not a fault — WAL + busy_timeout are already applied by apply_base_pragmas).
    Any other sqlite3.Error or OSError → fault.
    conn=None → fault (runtime dir may be unwritable, preventing DB open).
    """
    if conn is None:
        return ProbeResult.fault("no database connection (runtime directory may be unwritable)")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return ProbeResult.success()
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            # Lock contention from another concurrent session — not a fault.
            return ProbeResult.success()
        return ProbeResult.fault(f"database error: {exc}")
    except (sqlite3.Error, OSError) as exc:
        return ProbeResult.fault(f"database error: {exc}")


# ── Embedding-status sidecar ───────────────────────────────────────────────────


def read_embedding_status(path: Path = EMBEDDING_STATUS_PATH) -> dict | None:
    """Read the embedding-capability-failure sidecar (FR#6).

    Returns the parsed dict on success, None if missing or malformed.
    Tolerates all read/parse errors so the SessionStart hook path is never broken.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write ``data`` as JSON to ``path`` (tempfile + replace + cleanup).

    The single in-module implementation of the write_config.py atomic-write pattern,
    shared by both sidecars. Ensures the parent dir exists, then writes via a temp
    file in the same directory and replaces in one step so a concurrent reader never
    sees a partial file. Removes the temp file on any error before re-raising.
    """
    ensure_parent_dir(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        Path(tmp).replace(path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def record_embedding_failure(reason: str, path: Path = EMBEDDING_STATUS_PATH) -> None:
    """Write an embedding-capability-failure record to the sidecar (FR#4).

    Written by the detached embedding process on structural failure.
    Uses an atomic write to avoid partial reads by the SessionStart hook.
    """
    _atomic_write_json(path, {"reason": reason, "since": Instant.now().format_iso()})


def clear_embedding_failure(path: Path = EMBEDDING_STATUS_PATH) -> None:
    """Remove the embedding-capability-failure sidecar (FR#5).

    Called by the embedding process on a successful embed run.
    No-op if the file is already absent.
    """
    path.unlink(missing_ok=True)


# ── Snooze ledger ──────────────────────────────────────────────────────────────


def _read_snooze_ledger(path: Path) -> dict:
    """Read the snooze ledger, tolerating missing or malformed files."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_snooze_ledger(path: Path, ledger: dict) -> None:
    """Write the snooze ledger atomically (FR#8).

    Delegates to _atomic_write_json (tempfile + replace + cleanup + ensure_parent_dir);
    path-first to match that helper's signature.
    Raises on failure — callers that need FR#10 degradation catch externally.
    """
    _atomic_write_json(path, ledger)


def evaluate_alerts(
    active_keys: set[str],
    snooze_hours: float,
    snooze_path: Path = ALERT_SNOOZE_PATH,
) -> list[str]:
    """Return the alert keys that should fire now, and update the snooze ledger.

    For each key in active_keys:
    - Not in ledger OR last_fired older than snooze_hours → fire, update ledger.
    - last_fired within snooze_hours → suppress, keep ledger record.

    Auto-clear (FR#9): any key NOT in active_keys is dropped from the ledger so a
    later recurrence fires immediately rather than being held by a stale record.

    FR#10: if the ledger write fails (runtime dir unwritable), the fire list is
    still returned — degrading to re-tell every session is preferable to swallowing
    the most severe alert class.
    """
    snooze_seconds = snooze_hours * 3600
    now = Instant.now()

    ledger = _read_snooze_ledger(snooze_path)

    keys_to_fire: list[str] = []
    new_ledger: dict[str, str] = {}

    for key in active_keys:
        last_fired_iso = ledger.get(key)
        if last_fired_iso is None:
            # Never fired (or auto-cleared from a prior resolution) — fire now.
            keys_to_fire.append(key)
            new_ledger[key] = now.format_iso()
            continue

        try:
            then = Instant.parse_iso(last_fired_iso)
            age_seconds = (now - then).total("seconds")
        except Exception:
            # Any parse failure — ValueError (bad ISO string) or TypeError (non-string
            # stored in the ledger) — is treated as expired so the alert fires again.
            keys_to_fire.append(key)
            new_ledger[key] = now.format_iso()
            continue

        if age_seconds >= snooze_seconds:
            keys_to_fire.append(key)
            new_ledger[key] = now.format_iso()
        else:
            # Within snooze window — suppress but keep the existing record.
            new_ledger[key] = last_fired_iso

    # Auto-clear: keys not in active_keys are simply absent from new_ledger.

    try:
        _write_snooze_ledger(snooze_path, new_ledger)
    except Exception:
        # FR#10: write failure (e.g. dir unwritable) must not suppress the alert.
        logging.getLogger(LOGGER_NAME).exception("snooze ledger write failed; degrading to re-fire every session")

    return keys_to_fire


# ── Alert-block builder ────────────────────────────────────────────────────────

# Per-alert prose: (intro, default_cause, action).
# Mirrors format_pending_block(for_injection=True): ## ⚠ heading + prose with
# cause + action + explicit relay instruction (severity-as-intent, not hard-coded
# loudness — the assistant decides how prominently to raise it).
_ALERT_PROSE: dict[str, tuple[str, str, str]] = {
    ALERT_CANT_PERSIST: (
        "ccrecall cannot write to its runtime directory — history and context will not be saved this session.",
        "disk full, permission denied, or directory unavailable",
        "check disk space and permissions on ~/.ccrecall, then restart the session",
    ),
    ALERT_EMBEDDINGS_FAILING: (
        "ccrecall's embedding pipeline is failing — semantic search is unavailable or degraded.",
        "the vector extension (sqlite-vec) or embedding model is unavailable",
        "verify sqlite-vec is installed and the embedding model is accessible, then restart the session",
    ),
}

_RELAY_INSTRUCTION = "Surface this to the user in prose; do not hard-code how prominently to raise it."


def build_alert_block(
    keys_to_fire: list[str],
    fault_reason: str = "",
    embedding_reason: str = "",
) -> str:
    """Build a single Markdown alert block for all active, un-snoozed alerts (FR#11, FR#13).

    Returns "" when keys_to_fire is empty.

    Mirrors format_pending_block(for_injection=True): ## ⚠ heading followed by
    prose paragraphs — each carrying a likely cause, suggested action, and an
    explicit instruction to relay to the user without hard-coding prominence.
    Multiple alerts are concatenated into ONE block (FR#13).

    Lines are joined with a blank line ("\\n\\n") — deliberately paragraph-separated
    rather than the single-newline join in format_pending_block (whose lines are a
    heading plus a bullet list). Here each alert is a full prose paragraph, so a
    paragraph break renders correctly when the assistant relays it.
    """
    if not keys_to_fire:
        return ""

    lines: list[str] = ["## ⚠ ccrecall Alert"]

    custom_causes = {
        ALERT_CANT_PERSIST: fault_reason,
        ALERT_EMBEDDINGS_FAILING: embedding_reason,
    }

    for key in keys_to_fire:
        if key not in _ALERT_PROSE:
            continue
        intro, default_cause, action = _ALERT_PROSE[key]
        cause = custom_causes.get(key) or default_cause
        lines.append(f"{intro} Likely cause: {cause}. Suggested action: {action}. {_RELAY_INSTRUCTION}")

    return "\n\n".join(lines)
