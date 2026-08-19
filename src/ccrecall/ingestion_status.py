"""Transcript-vs-DB ingestion coverage diagnostics.

Diagnostics are read-only except for confirmed-OK cache rows, which let later
deep-check runs skip reparsing unchanged transcript sources.
"""

import logging
from pathlib import Path
from sqlite3 import Connection

from whenever import Instant

from ccrecall.import_log_ops import import_log_source_index
from ccrecall.message_ops import message_content_parts
from ccrecall.models import LOGGER_NAME
from ccrecall.parsing import parse_all_with_uuids, select_active_leaf_entry

log = logging.getLogger(LOGGER_NAME)

STALE_TAIL_SECONDS = 15 * 60


def _entry_expects_message(entry: dict) -> bool:
    """True when an entry should have a ``messages`` row after ingestion."""
    return message_content_parts(entry) is not None


def _expected_uuids(filepaths: list[Path]) -> list[str]:
    """Return ordered active-branch UUIDs expected to have messages rows."""
    entries: list[dict] = []
    for filepath in filepaths:
        entries.extend(parse_all_with_uuids(filepath))
    latest = select_active_leaf_entry(entries)
    if latest is None:
        return []

    uuid_to_entry = {entry["uuid"]: entry for entry in entries if entry.get("uuid")}
    ordered_branch: list[dict] = []
    current_uuid: str | None = latest["uuid"]
    while current_uuid:
        entry = uuid_to_entry.get(current_uuid)
        if entry is None:
            break
        ordered_branch.append(entry)
        current_uuid = entry.get("parentUuid")
    ordered_branch.reverse()
    return [entry["uuid"] for entry in ordered_branch if _entry_expects_message(entry)]


def _is_contiguous_suffix(indices: list[int], total: int) -> bool:
    if not indices:
        return False
    return indices == list(range(indices[0], total))


def _source_fingerprint(filepaths: list[Path]) -> str | None:
    """Return a deterministic stat fingerprint, or None if any source is missing."""
    parts: list[str] = []
    for path in sorted(filepaths, key=str):
        try:
            stat = path.stat()
        except FileNotFoundError:
            log.warning(
                "transcript source missing while computing ingestion fingerprint; "
                "session will be counted as missing_source",
                extra={"path": str(path)},
            )
            return None
        parts.append(f"{path}\t{stat.st_size}\t{stat.st_mtime_ns}")
    return "\n".join(parts)


def _db_coverage_fingerprint(cursor, session_id: int) -> str:
    """Return the stored UUID membership token for cache validation."""
    rows = cursor.execute(
        """
        SELECT uuid
        FROM messages
        WHERE session_id = ? AND uuid IS NOT NULL
        ORDER BY uuid
        """,
        (session_id,),
    ).fetchall()
    return "\n".join(row[0] for row in rows)


def _cached_ok_fingerprint(cursor, session_uuid: str) -> tuple[str, str] | None:
    row = cursor.execute(
        "SELECT source_fingerprint, db_coverage_fingerprint FROM ingestion_check_cache WHERE session_uuid = ?",
        (session_uuid,),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def _record_ok_fingerprint(
    cursor,
    session_uuid: str,
    source_fingerprint: str,
    db_coverage_fingerprint: str,
) -> None:
    cursor.execute(
        """
        INSERT OR REPLACE INTO ingestion_check_cache
        (session_uuid, source_fingerprint, db_coverage_fingerprint)
        VALUES (?, ?, ?)
        """,
        (session_uuid, source_fingerprint, db_coverage_fingerprint),
    )


def summarize_ingestion(
    conn: Connection,
    *,
    stale_tail_seconds: int = STALE_TAIL_SECONDS,
    sources: dict[str, dict[str, list[Path]]] | None = None,
) -> dict[str, int]:
    """Classify transcript ingestion gaps by comparing JSONL UUID order to DB rows.

    ``pending_tail`` means the DB is missing only a contiguous suffix from an
    existing transcript that was modified recently, which is normal while Claude
    Code is still writing the session. ``stale_tail`` is the same shape after the
    grace window. ``ingestion_gap`` means missing UUIDs are in the middle of the
    expected active branch and should be recoverable by import/sync. A session
    with import-log rows but no surviving JSONL is counted as ``missing_source``.
    """
    cursor = conn.cursor()
    if sources is None:
        sources = import_log_source_index(cursor)

    summary = {
        "sessions_checked": 0,
        "ok_sessions": 0,
        "pending_tail_sessions": 0,
        "pending_tail_turns": 0,
        "stale_tail_sessions": 0,
        "stale_tail_turns": 0,
        "ingestion_gap_sessions": 0,
        "ingestion_gap_turns": 0,
        "missing_source_sessions": 0,
    }
    ok_cache_writes: list[tuple[str, str, str]] = []

    for session_uuid, paths in sources.items():
        if not paths["missing"]:
            continue
        if cursor.execute("SELECT 1 FROM sessions WHERE uuid = ?", (session_uuid,)).fetchone() is not None:
            summary["sessions_checked"] += 1
            summary["missing_source_sessions"] += 1

    now = Instant.now()
    for session_uuid, paths in sources.items():
        if paths["missing"]:
            continue
        filepaths = paths["existing"]
        session_row = cursor.execute("SELECT id FROM sessions WHERE uuid = ?", (session_uuid,)).fetchone()
        if session_row is None:
            continue
        summary["sessions_checked"] += 1

        source_fingerprint = _source_fingerprint(filepaths)
        if source_fingerprint is None:
            summary["missing_source_sessions"] += 1
            continue

        db_coverage_fingerprint = _db_coverage_fingerprint(cursor, session_row[0])

        if _cached_ok_fingerprint(cursor, session_uuid) == (source_fingerprint, db_coverage_fingerprint):
            summary["ok_sessions"] += 1
            continue

        session_id = session_row[0]
        existing_msg_uuids = {
            row[0]
            for row in cursor.execute(
                "SELECT uuid FROM messages WHERE session_id = ? AND uuid IS NOT NULL",
                (session_id,),
            ).fetchall()
        }

        expected = _expected_uuids(filepaths)
        missing_indices = [i for i, uuid in enumerate(expected) if uuid not in existing_msg_uuids]
        if not missing_indices:
            summary["ok_sessions"] += 1
            ok_cache_writes.append((session_uuid, source_fingerprint, db_coverage_fingerprint))
            continue

        if _is_contiguous_suffix(missing_indices, len(expected)):
            newest_mtime = max(Instant.from_timestamp(path.stat().st_mtime) for path in filepaths)
            if (now - newest_mtime).total("seconds") <= stale_tail_seconds:
                summary["pending_tail_sessions"] += 1
                summary["pending_tail_turns"] += len(missing_indices)
            else:
                summary["stale_tail_sessions"] += 1
                summary["stale_tail_turns"] += len(missing_indices)
        else:
            summary["ingestion_gap_sessions"] += 1
            summary["ingestion_gap_turns"] += len(missing_indices)

    for session_uuid, source_fingerprint, db_coverage_fingerprint in ok_cache_writes:
        _record_ok_fingerprint(cursor, session_uuid, source_fingerprint, db_coverage_fingerprint)

    return summary
