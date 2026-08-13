"""Minimal no-migration durable intent write used by the SessionEnd hook."""

from ccrecall.llm_summary_db import open_no_migrate_connection, recap_schema_capability
from ccrecall.recap_state import upsert_job


def record_intent(settings: dict, session_uuid: str, now: str, *, platform_supported: bool) -> str:
    """Return ``recorded``, ``unknown``, or ``unavailable`` without migrating."""
    try:
        conn = open_no_migrate_connection(settings)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if recap_schema_capability(conn) != "ready":
                return "unavailable"
            row = conn.execute(
                "SELECT s.id, b.recap_input_hash FROM sessions s JOIN branches b ON b.session_id = s.id "
                "WHERE s.uuid = ? AND b.is_active = 1",
                (session_uuid,),
            ).fetchone()
            if row is None:
                return "unknown"
            upsert_job(conn, row[0], row[1], "session_end", now)
            if not platform_supported:
                conn.execute(
                    "UPDATE session_recap_jobs SET state = 'blocked', reason = 'platform_unsupported', "
                    "lease_expires_at = NULL WHERE session_id = ?",
                    (row[0],),
                )
            conn.commit()
            return "recorded"
        finally:
            conn.close()
    except Exception:
        return "unavailable"
