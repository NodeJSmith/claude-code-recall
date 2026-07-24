"""Proactive health-alert block builder for the SessionStart context hook.

Evaluates the filesystem-writability probe, the embedding-status sidecar, the
DB write-lock probe, and the tool-content backfill coverage check, then builds
a single combined alert block for whatever fires. Split out of
memory_context.py so the alert-evaluation concern has its own module.
"""

import logging
import sqlite3
from pathlib import Path

from ccrecall.config import DEFAULT_SETTINGS
from ccrecall.health import (
    ALERT_CANT_PERSIST,
    ALERT_EMBEDDINGS_FAILING,
    ALERT_TOOL_CONTENT_INCOMPLETE,
    build_alert_block,
    evaluate_alerts,
    probe_db,
    probe_filesystem,
    read_embedding_status,
)
from ccrecall.models import LOGGER_NAME

_TOOL_CONTENT_SAMPLE_SIZE = 5


def proactive_alert_block(
    settings: dict,
    conn: sqlite3.Connection | None,
    db_available: bool,
    *,
    _marker_path: Path | None = None,
    _snooze_path: Path | None = None,
    _status_path: Path | None = None,
) -> str:
    """Build the proactive health-alert block for SessionStart injection.

    Evaluates both proactive alert classes:
    - Filesystem writability probe (active, cheap, no DB required)
    - Embedding-status sidecar read (passive, plain file read — never imports
      fastembed/onnxruntime/sqlite_vec — hot-path invariant)
    - DB write-lock probe (active, on the already-open connection, or conn=None
      when the connection itself failed — that failure becomes a fault)

    Passes active keys through the snooze ledger (fire / suppress / auto-clear)
    and builds ONE combined block for whatever fires.

    Defensive wrapper: follows the pending_question_block precedent — any
    exception degrades to "" so the hook is never broken and context injection is
    unaffected.

    _marker_path / _snooze_path / _status_path: test injection points for sidecar
    paths; None means use the health.py function defaults (production paths).
    """
    try:
        # 1. Filesystem probe — no DB needed, unconditional.
        fs_result = probe_filesystem() if _marker_path is None else probe_filesystem(_marker_path)

        # 2. Embedding-status sidecar — plain file read only, no vec/fastembed load.
        embedding_status = read_embedding_status() if _status_path is None else read_embedding_status(_status_path)

        # 3. DB probe — only when the DB file exists; a missing DB is not a fault
        # (fresh install). conn=None here means the connection failed (dir/WAL
        # unwritable), which probe_db correctly classifies as a persist fault.
        db_probe = probe_db(conn) if db_available else None

        # 4. Compute active alert keys and human-readable reasons.
        active_keys: set[str] = set()
        fault_reason = ""
        embedding_reason = ""

        if not fs_result.ok:
            active_keys.add(ALERT_CANT_PERSIST)
            fault_reason = fs_result.reason

        if db_probe is not None and not db_probe.ok:
            active_keys.add(ALERT_CANT_PERSIST)
            # Prefer the FS reason (more actionable) when both probes fail.
            if not fault_reason:
                fault_reason = db_probe.reason

        if embedding_status is not None:
            active_keys.add(ALERT_EMBEDDINGS_FAILING)
            # Pass the raw reason code; build_alert_block translates it to prose
            # (the mapping lives in health.py beside the REASON_* constants).
            embedding_reason = embedding_status.get("reason", "")

        if conn is not None and _has_backfillable_tool_content(conn):
            active_keys.add(ALERT_TOOL_CONTENT_INCOMPLETE)

        # 5. Evaluate snooze ledger: fire / suppress / auto-clear.
        # load_settings() always carries alert_snooze_hours from DEFAULT_SETTINGS;
        # fall back to the canonical default only for sparse (test) settings dicts.
        snooze_hours = float(settings.get("alert_snooze_hours", DEFAULT_SETTINGS["alert_snooze_hours"]))
        keys_to_fire = (
            evaluate_alerts(active_keys, snooze_hours)
            if _snooze_path is None
            else evaluate_alerts(active_keys, snooze_hours, _snooze_path)
        )

        # 6. Build one combined block.
        return build_alert_block(
            keys_to_fire,
            fault_reason=fault_reason,
            embedding_reason=embedding_reason,
        )

    except Exception:
        # Deliberately broad: this optional alert must never break the SessionStart
        # hook or drop the main context injection. Log best-effort (no-op unless
        # logging_enabled) so the failure isn't silently lost.
        logging.getLogger(LOGGER_NAME).exception("proactive alert block failed")
        return ""


def _has_backfillable_tool_content(conn: sqlite3.Connection) -> bool:
    """Check if there are sessions with NULL tool_content whose JSONL still exists.

    Two-phase cheap check for the hot path:
    1. SQL: sample a few session UUIDs with tool_content IS NULL.
    2. For each, LIKE-match import_log for a file containing that UUID and
       check Path.exists() — caps at _TOOL_CONTENT_SAMPLE_SIZE queries + stat calls.

    Returns False for fresh installs (no NULL-tool_content sessions) and for
    databases where all remaining NULL sessions have lost their JSONL on disk.
    """
    rows = conn.execute(
        """SELECT DISTINCT s.uuid
           FROM sessions s
           JOIN messages m ON m.session_id = s.id
           JOIN branches b ON b.session_id = s.id AND b.is_active = 1
           WHERE m.tool_content IS NULL
           LIMIT ?""",
        (_TOOL_CONTENT_SAMPLE_SIZE,),
    ).fetchall()
    if not rows:
        return False

    for (uuid,) in rows:
        # Mirrors the agent- prefix convention in parsing.extract_session_uuid.
        file_rows = conn.execute(
            "SELECT file_path FROM import_log WHERE file_path LIKE ? OR file_path LIKE ?",
            (f"%/{uuid}.jsonl", f"%/agent-{uuid}.jsonl"),
        ).fetchall()
        if any(Path(r[0]).exists() for r in file_rows):
            return True

    return False
