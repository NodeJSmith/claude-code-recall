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
from dataclasses import dataclass
from pathlib import Path

from whenever import Instant

from ccrecall.config import PID_FILE_MODE, RUNTIME_DIR, atomic_write_json
from ccrecall.models import LOGGER_NAME

# ── Sidecar paths ──────────────────────────────────────────────────────────────
# Module-level so they are the single canonical location for each sidecar. Callers
# don't import these directly — they go through the functions below.
#
# Every function below that takes a path argument (probe_filesystem, read/record/
# clear_embedding_failure, evaluate_alerts) resolves it as `param: Path | None =
# None` followed by `if param is None: param = THIS_CONSTANT` INSIDE the function
# body — never as `param: Path = THIS_CONSTANT` in the signature. A Python default
# argument value is evaluated exactly once, when `def` runs, and that one object is
# reused on every call forever; a signature default would silently freeze in the
# real path at import time, so `monkeypatch.setattr(health, "EMBEDDING_STATUS_PATH",
# tmp)` (see tests/conftest.py's `_isolated_runtime_dir` fixture) would have no
# effect on a caller that omits the argument. Resolving inside the body instead does
# a fresh global lookup on every call, so the monkeypatch is seen (#151). Do not
# "simplify" any of these back to a signature default — that reintroduces the bug.
EMBEDDING_STATUS_PATH = RUNTIME_DIR / "embedding-status.json"
ALERT_SNOOZE_PATH = RUNTIME_DIR / "alert-snooze.json"
BACKFILL_SCHEDULE_PATH = RUNTIME_DIR / "backfill-schedule.json"

# Fixed marker path for the filesystem writability probe (private; inject via
# probe_filesystem(marker_path=...) in tests).
_PROBE_MARKER_PATH = RUNTIME_DIR / ".write-probe"

# ── Named constants ────────────────────────────────────────────────────────────
# Reactive-caveat threshold consumed by the recall path. Coverage at or above this
# fraction suppresses the recall caveat.
RECALL_CAVEAT_COVERAGE_THRESHOLD = 0.95

SECONDS_PER_HOUR = 3600

# Alert key constants — one per proactive alert class.
ALERT_CANT_PERSIST = "cant_persist"
ALERT_EMBEDDINGS_FAILING = "embeddings_failing"
ALERT_TOOL_CONTENT_INCOMPLETE = "tool_content_incomplete"
ALERT_DRAFT_QUALITY_VECTORS = "draft_quality_vectors"

# Deliberate duplication of embeddings.MODEL_TOKEN_LIMIT: health.py must never
# import from embeddings.py (see module docstring's hot-path invariant), so this
# is a plain int literal kept in sync by tests/test_health.py's cross-check.
FULL_QUALITY_TOKEN_LIMIT = 8192

# Embedding-capability failure reason codes (the sub-protocol the detached embedding
# process writes into the embedding-status sidecar; the SessionStart hook reads them back). Shared here
# so the writer (backfill/sync) and any reader agree on the exact strings.
REASON_VEC_UNAVAILABLE = "vec_unavailable"
REASON_MODEL_UNAVAILABLE = "model_unavailable"


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


def probe_filesystem(marker_path: Path | None = None) -> ProbeResult:
    """Active filesystem writability probe.

    Uses O_CREAT|O_TRUNC (deliberately NOT O_EXCL) so the probe is idempotent
    and survives a stale marker left by a crash between write and unlink.  Any
    OSError → fault with the error message as reason.

    ``marker_path`` resolves to ``_PROBE_MARKER_PATH`` at call time — see the
    "Sidecar paths" comment above for why this isn't a signature default.
    """
    if marker_path is None:
        marker_path = _PROBE_MARKER_PATH
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
    """Active DB writability probe via BEGIN IMMEDIATE / ROLLBACK.

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


def read_embedding_status(path: Path | None = None) -> dict | None:
    """Read the embedding-capability-failure sidecar.

    Returns the parsed dict on success, None if missing or malformed.
    Tolerates all read/parse errors so the SessionStart hook path is never broken.

    ``path`` resolves to ``EMBEDDING_STATUS_PATH`` at call time — see the
    "Sidecar paths" comment above for why this isn't a signature default.
    """
    if path is None:
        path = EMBEDDING_STATUS_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def record_embedding_failure(reason: str, path: Path | None = None) -> None:
    """Write an embedding-capability-failure record to the sidecar.

    Written by the detached embedding process on structural failure.
    Uses an atomic write to avoid partial reads by the SessionStart hook.

    ``path`` resolves to ``EMBEDDING_STATUS_PATH`` at call time — see the
    "Sidecar paths" comment above for why this isn't a signature default.
    """
    if path is None:
        path = EMBEDDING_STATUS_PATH
    atomic_write_json(path, {"reason": reason, "since": Instant.now().format_iso()})


def clear_embedding_failure(path: Path | None = None, reasons: set[str | None] | None = None) -> None:
    """Remove the embedding-capability-failure sidecar.

    Called by the embedding process on a successful embed run (backfill) or a
    successful structural check (sync-current). No-op if the sidecar is
    already absent.

    ``reasons`` scopes the clear to records whose "reason" field is in the
    given set — a no-op (record left in place) when the current record's
    reason is outside that scope. Default (None) clears unconditionally,
    regardless of reason — this is what backfill wants, since it re-verifies
    both the vec extension and the model.

    This scoping exists because sync-current's vec-ok clear only proves
    sqlite-vec is queryable, not that the embedding model itself is healthy —
    a REASON_MODEL_UNAVAILABLE record written by backfill must survive a
    vec-ok clear from sync-current, since sync-current never re-verified the
    model (#164). sync-current passes ``reasons={None, REASON_VEC_UNAVAILABLE}``
    so it only clears reasons its own check can actually vouch for; None is
    included so a record with no/malformed "reason" field (which should never
    happen given record_embedding_failure always writes one, but costs
    nothing to tolerate) doesn't get stuck forever.

    Deliberately does NOT touch the snooze ledger — the ledger is written only
    by SessionStart hooks (design/specs/002-ccrecall-surfacing-model/design.md
    § Edge Cases, "Two sidecar concerns, two writer classes"), to keep this
    file's read-modify-write single-writer. A second writer here previously
    raced with evaluate_alerts()'s own read-modify-write and could clobber an
    unrelated alert key's snooze timestamp (#153 review). The ledger catches up
    on its own within one hook cycle regardless: evaluate_alerts()'s auto-clear
    drops any key no longer in active_keys, and active_keys for
    ALERT_EMBEDDINGS_FAILING is recomputed from this sidecar's live existence
    on every call — so the brief disagreement this reopens (ledger still lists
    ALERT_EMBEDDINGS_FAILING until the next SessionStart hook runs) is
    self-healing, not a stuck state.

    ``path`` resolves to ``EMBEDDING_STATUS_PATH`` at call time — see the
    "Sidecar paths" comment above for why this isn't a signature default.
    """
    if path is None:
        path = EMBEDDING_STATUS_PATH
    if reasons is not None:
        current = read_embedding_status(path=path)
        if current is not None and current.get("reason") not in reasons:
            return
    path.unlink(missing_ok=True)


# ── Backfill schedule marker ───────────────────────────────────────────────────
# Suppresses ALERT_DRAFT_QUALITY_VECTORS: a user who has configured a
# scheduled backfill job, or explicitly dismissed the alert, shouldn't be
# nagged every session. Two writers share this one sidecar — `ccrecall backfill
# schedule write` sets "configured_at"; `ccrecall backfill embeddings --dismiss`
# sets "dismissed_at" — either field satisfies the suppression check below.


def read_schedule_marker(path: Path | None = None) -> dict | None:
    """Read the backfill-schedule marker sidecar.

    Returns the parsed dict when it carries "configured_at" or "dismissed_at"
    (either satisfies the suppression check). Returns None when the file is
    missing, malformed, not a JSON object, or present but missing both
    recognized fields — treating an unrecognized marker as absent so the alert
    still fires rather than being silently suppressed by garbage content.

    ``path`` resolves to ``BACKFILL_SCHEDULE_PATH`` at call time — see the
    "Sidecar paths" comment above for why this isn't a signature default.
    """
    if path is None:
        path = BACKFILL_SCHEDULE_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if "configured_at" not in data and "dismissed_at" not in data:
        return None
    return data


def write_schedule_marker(field: str, path: Path | None = None) -> None:
    """Write the backfill-schedule marker with a single timestamp field.

    ``field`` is "configured_at" (``ccrecall backfill schedule write``) or
    "dismissed_at" (``ccrecall backfill embeddings --dismiss``). Uses an
    atomic write so a concurrent SessionStart hook read never sees a partial
    file.

    ``path`` resolves to ``BACKFILL_SCHEDULE_PATH`` at call time — see the
    "Sidecar paths" comment above for why this isn't a signature default.
    """
    if path is None:
        path = BACKFILL_SCHEDULE_PATH
    atomic_write_json(path, {field: Instant.now().format_iso()})


def clear_schedule_marker(path: Path | None = None) -> bool:
    """Remove the backfill-schedule marker sidecar. Returns True iff it existed.

    ``path`` resolves to ``BACKFILL_SCHEDULE_PATH`` at call time — see the
    "Sidecar paths" comment above for why this isn't a signature default.
    """
    if path is None:
        path = BACKFILL_SCHEDULE_PATH
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def describe_schedule_marker(marker: dict | None) -> str:
    """Render the backfill-schedule marker state as a one-line human string.

    Shared by `ccrecall backfill schedule status` so the marker-state prose
    lives in one place rather than being re-derived in the CLI layer. Takes
    the already-parsed marker (from read_schedule_marker) rather than a path,
    so callers control the DB/IO boundary.
    """
    if marker is None:
        return "not set"
    if "dismissed_at" in marker:
        return f"dismissed at {marker['dismissed_at']}"
    if "configured_at" in marker:
        return f"configured at {marker['configured_at']}"
    return "present but unrecognized"


# ── Draft-quality chunk count ────────────────────────────────────────────────
# Plain sqlite3 query — no vec/fastembed/onnxruntime import needed, so this
# stays safe on the SessionStart hot path (see module docstring). Shared by
# hooks.context_alerts.has_draft_quality_chunks (bool check, hot path) and
# `ccrecall backfill schedule status` (raw count, cli/commands.py) so the
# draft-quality predicate can't drift between the two call sites.


def count_draft_quality_chunks(conn: sqlite3.Connection) -> int:
    """Return the count of chunks embedded below FULL_QUALITY_TOKEN_LIMIT tokens.

    Cheap COUNT(*) — cap_tokens lives in the base `chunks` table, so this
    needs no vec extension or embedding model. NULL cap_tokens means
    untruncated (full quality), so it's excluded by the `IS NOT NULL` guard
    rather than by a value comparison. Joined to `branches` and filtered on
    `is_active = 1` so a chunk left behind by an inactive (historical/orphaned)
    branch never counts — mirrors the standing `is_active = 1` invariant every
    other read path in this codebase follows.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM chunks c JOIN branches b ON b.id = c.branch_id "
        "WHERE b.is_active = 1 AND c.cap_tokens IS NOT NULL AND c.cap_tokens < ?",
        (FULL_QUALITY_TOKEN_LIMIT,),
    ).fetchone()[0]


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
    """Write the snooze ledger atomically.

    Delegates to config.atomic_write_json (tempfile + replace + cleanup + ensure_parent_dir).
    Raises on failure — callers that need the degrade-to-re-fire behavior catch externally.
    """
    atomic_write_json(path, ledger)


def evaluate_alerts(
    active_keys: set[str],
    snooze_hours: float,
    snooze_path: Path | None = None,
) -> list[str]:
    """Return the alert keys that should fire now, and update the snooze ledger.

    For each key in active_keys:
    - Not in ledger OR last_fired older than snooze_hours → fire, update ledger.
    - last_fired within snooze_hours → suppress, keep ledger record.

    Auto-clear: any key NOT in active_keys is dropped from the ledger so a
    later recurrence fires immediately rather than being held by a stale record.

    If the ledger write fails (runtime dir unwritable), the fire list is
    still returned — degrading to re-tell every session is preferable to swallowing
    the most severe alert class.

    ``snooze_path`` resolves to ``ALERT_SNOOZE_PATH`` at call time — see the
    "Sidecar paths" comment above for why this isn't a signature default.
    """
    if snooze_path is None:
        snooze_path = ALERT_SNOOZE_PATH
    snooze_seconds = snooze_hours * SECONDS_PER_HOUR
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
        # A write failure (e.g. dir unwritable) must not suppress the alert.
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
    ALERT_TOOL_CONTENT_INCOMPLETE: (
        "ccrecall's tool-content index is incomplete — tool_use content from older sessions is not yet searchable.",
        "sessions synced before tool-content extraction was added have not been backfilled",
        "run `ccrecall backfill tool-content` to index historical tool_use content (one-time, opt-in)",
    ),
    ALERT_DRAFT_QUALITY_VECTORS: (
        "Some conversations have draft-quality embeddings — semantic search works but may miss context "
        "from long exchanges.",
        "the sync path caps embeddings at 4096 tokens for memory safety; full 8192-token quality requires "
        "a scheduled backfill",
        "run `ccrecall backfill embeddings` to upgrade existing drafts; to prevent recurrence, set up a "
        "scheduled job (`ccrecall backfill schedule write`) or dismiss permanently "
        "(`ccrecall backfill embeddings --dismiss`)",
    ),
}

_RELAY_INSTRUCTION = "Surface this to the user in prose; do not hard-code how prominently to raise it."

# Maps machine-readable embedding-status reason codes to user-facing cause prose,
# so the alert never shows a raw code like "vec_unavailable". Unknown codes pass
# through as-is (future-safe). Lives here beside the REASON_* constants — the
# parse/format boundary for the embedding-status sub-protocol.
_REASON_PROSE: dict[str, str] = {
    REASON_VEC_UNAVAILABLE: "vector extension (sqlite-vec) unavailable",
    REASON_MODEL_UNAVAILABLE: "embedding model unavailable or inaccessible",
}


def build_alert_block(
    keys_to_fire: list[str],
    fault_reason: str = "",
    embedding_reason: str = "",
) -> str:
    """Build a single Markdown alert block for all active, un-snoozed alerts.

    Returns "" when keys_to_fire is empty.

    Mirrors format_pending_block(for_injection=True): ## ⚠ heading followed by
    prose paragraphs — each carrying a likely cause, suggested action, and an
    explicit instruction to relay to the user without hard-coding prominence.
    Multiple alerts are concatenated into ONE block.

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
        # Translate the embedding reason code to prose here so callers pass the raw
        # code from the sidecar and never have to know the prose mapping.
        ALERT_EMBEDDINGS_FAILING: _REASON_PROSE.get(embedding_reason, embedding_reason),
    }

    for key in keys_to_fire:
        if key not in _ALERT_PROSE:
            continue
        intro, default_cause, action = _ALERT_PROSE[key]
        cause = custom_causes.get(key) or default_cause
        lines.append(f"{intro} Likely cause: {cause}. Suggested action: {action}. {_RELAY_INSTRUCTION}")

    return "\n\n".join(lines)
