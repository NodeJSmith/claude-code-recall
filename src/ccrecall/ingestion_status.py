"""Transcript-vs-DB ingestion coverage diagnostics.

Diagnostics are read-only except for confirmed-OK cache rows, which let later
deep-check runs skip reparsing unchanged transcript sources.
"""

import logging
from collections.abc import Iterator
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
    """Return a token combining message-UUID membership and branch_messages
    linkage, for cache validation.

    UUID membership alone (the original fingerprint) can't see a linking-only
    regression: a message row can exist while its branch_messages link was
    dropped by a buggy diff (design/specs/016-stale-tail-import-repair
    Finding 1/6), and that leaves the message row itself, and therefore the
    UUID-only fingerprint, unchanged. Folding in a per-active-branch linked
    message count makes that class of regression invalidate the
    ingestion_check_cache the same way a content regression already does.
    """
    message_rows = cursor.execute(
        """
        SELECT uuid
        FROM messages
        WHERE session_id = ? AND uuid IS NOT NULL
        ORDER BY uuid
        """,
        (session_id,),
    ).fetchall()
    link_rows = cursor.execute(
        """
        SELECT b.id, COUNT(bm.message_id)
        FROM branches b
        LEFT JOIN branch_messages bm ON bm.branch_id = b.id
        WHERE b.session_id = ? AND b.is_active = 1
        GROUP BY b.id
        ORDER BY b.id
        """,
        (session_id,),
    ).fetchall()
    message_part = "\n".join(row[0] for row in message_rows)
    link_part = "\n".join(f"{branch_id}:{count}" for branch_id, count in link_rows)
    return message_part + "\x00" + link_part


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


def classify_sessions(
    cursor,
    sources: dict[str, dict[str, list[Path]]],
    now: Instant,
    stale_tail_seconds: int,
) -> Iterator[tuple[str, str, int, list[int]]]:
    """Yield (session_uuid, category, session_id, missing_indices) for each session with a verdict.

    category is one of "ok", "pending_tail", "stale_tail", "ingestion_gap", or
    "missing_source" (the rare case where a source file that was present when
    ``sources`` was built has since disappeared — a stat-time TOCTOU race, not
    the more common paths["missing"] case below).
    Sessions confirmed via the ok-fingerprint cache are yielded as category "ok"
    with an empty missing_indices list — same as a freshly-computed zero-gap session.
    Sessions whose entire source is missing (``paths["missing"]`` non-empty) are
    NOT yielded here; that classification happens in summarize_ingestion's
    separate first loop over sessions with no existing files.
    """
    for session_uuid, paths in sources.items():
        if paths["missing"]:
            continue
        filepaths = paths["existing"]
        session_row = cursor.execute("SELECT id FROM sessions WHERE uuid = ?", (session_uuid,)).fetchone()
        if session_row is None:
            continue
        session_id = session_row[0]

        source_fingerprint = _source_fingerprint(filepaths)
        if source_fingerprint is None:
            yield session_uuid, "missing_source", session_id, []
            continue

        db_coverage_fingerprint = _db_coverage_fingerprint(cursor, session_id)

        if _cached_ok_fingerprint(cursor, session_uuid) == (source_fingerprint, db_coverage_fingerprint):
            yield session_uuid, "ok", session_id, []
            continue

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
            yield session_uuid, "ok", session_id, []
            continue

        if _is_contiguous_suffix(missing_indices, len(expected)):
            newest_mtime = max(Instant.from_timestamp(path.stat().st_mtime) for path in filepaths)
            if (now - newest_mtime).total("seconds") <= stale_tail_seconds:
                yield session_uuid, "pending_tail", session_id, missing_indices
            else:
                yield session_uuid, "stale_tail", session_id, missing_indices
        else:
            yield session_uuid, "ingestion_gap", session_id, missing_indices


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
    for session_uuid, category, session_id, missing_indices in classify_sessions(
        cursor, sources, now, stale_tail_seconds
    ):
        summary["sessions_checked"] += 1

        if category == "missing_source":
            summary["missing_source_sessions"] += 1
            continue

        if category == "ok":
            summary["ok_sessions"] += 1
            filepaths = sources[session_uuid]["existing"]
            source_fingerprint = _source_fingerprint(filepaths)
            if source_fingerprint is not None:
                db_coverage_fingerprint = _db_coverage_fingerprint(cursor, session_id)
                if _cached_ok_fingerprint(cursor, session_uuid) != (source_fingerprint, db_coverage_fingerprint):
                    ok_cache_writes.append((session_uuid, source_fingerprint, db_coverage_fingerprint))
            continue

        turns = len(missing_indices)
        summary[f"{category}_sessions"] += 1
        summary[f"{category}_turns"] += turns

    for session_uuid, source_fingerprint, db_coverage_fingerprint in ok_cache_writes:
        _record_ok_fingerprint(cursor, session_uuid, source_fingerprint, db_coverage_fingerprint)

    return summary


def find_repairable_sessions(
    conn: Connection,
    *,
    stale_tail_seconds: int = STALE_TAIL_SECONDS,
    sources: dict[str, dict[str, list[Path]]] | None = None,
) -> list[tuple[str, int, int | None, list[Path]]]:
    """Return (session_uuid, session_id, project_id, filepaths) for every
    session classified as stale_tail or ingestion_gap — the categories a
    force-reimport can actually repair. project_id is None when the session's
    project_id column is nullable and unset. Excludes pending_tail (likely
    still being written) and missing_source (no surviving JSONL to reimport
    from)."""
    cursor = conn.cursor()
    if sources is None:
        sources = import_log_source_index(cursor)

    now = Instant.now()
    candidates: list[tuple[str, int, int | None, list[Path]]] = []
    for session_uuid, category, session_id, _missing_indices in classify_sessions(
        cursor, sources, now, stale_tail_seconds
    ):
        if category not in ("stale_tail", "ingestion_gap"):
            continue
        project_row = cursor.execute("SELECT project_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        project_id = project_row[0] if project_row is not None else None
        filepaths = sources[session_uuid]["existing"]
        candidates.append((session_uuid, session_id, project_id, filepaths))

    return candidates


def reclassify_session(
    conn: Connection,
    session_uuid: str,
    filepaths: list[Path],
    *,
    stale_tail_seconds: int = STALE_TAIL_SECONDS,
) -> str:
    """Return the current classification ("ok", "pending_tail", "stale_tail",
    "ingestion_gap", or "missing_source" for the rare no-session-row race) for
    one session, given its known-existing filepaths.

    Builds a one-entry sources dict and delegates to classify_sessions — the
    ingestion_check_cache short-circuit inside it naturally misses here after a
    real repair (the DB coverage fingerprint changed), so this reflects genuinely
    current state rather than a stale cached verdict.
    """
    cursor = conn.cursor()
    sources = {session_uuid: {"existing": filepaths, "missing": []}}
    result = next(classify_sessions(cursor, sources, Instant.now(), stale_tail_seconds), None)
    return result[1] if result else "missing_source"
