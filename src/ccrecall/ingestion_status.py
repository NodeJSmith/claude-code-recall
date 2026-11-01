"""Transcript-vs-DB ingestion coverage diagnostics.

Diagnostics are read-only except for confirmed-OK cache rows, which let later
runs skip reparsing unchanged transcript sources. ``summarize_ingestion`` leaves
committing them to its caller; ``find_repairable_sessions`` commits its own.
"""

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from sqlite3 import Connection
from typing import Literal, NamedTuple

from whenever import Instant

from ccrecall.import_log_ops import import_log_source_index
from ccrecall.message_ops import message_content_parts
from ccrecall.models import LOGGER_NAME
from ccrecall.parsing import active_chain, parse_all_with_uuids

log = logging.getLogger(LOGGER_NAME)

STALE_TAIL_SECONDS = 15 * 60

# Part of every source fingerprint, so bumping it invalidates all cached "ok"
# verdicts. Bump it when anything changes which entries an up-to-date session is
# expected to have (active_chain, is_valid_entry / TranscriptEntry,
# message_content_parts): an unchanged transcript can then be missing rows the
# old code never expected.
ACTIVE_CHAIN_VERSION = 2

# Sessions verified per commit on the repair path, so a killed run keeps its progress.
OK_CACHE_COMMIT_BATCH = 100

SessionCategory = Literal["ok", "pending_tail", "stale_tail", "ingestion_gap", "missing_source"]


class OkCacheRow(NamedTuple):
    session_uuid: str
    source_fingerprint: str
    db_coverage_fingerprint: str


class SessionVerdict(NamedTuple):
    session_uuid: str
    category: SessionCategory
    session_id: int
    missing_indices: list[int]
    # Set only when parsing just verified this session ok, so the caller can cache that.
    new_ok_row: OkCacheRow | None = None


def _entry_expects_message(entry: dict) -> bool:
    """True when an entry should have a ``messages`` row after ingestion.

    Changing this answer requires bumping ``ACTIVE_CHAIN_VERSION``; see that constant.
    """
    return message_content_parts(entry) is not None


def _expected_uuids(filepaths: list[Path]) -> list[str]:
    """Return ordered active-branch UUIDs expected to have messages rows."""
    entries: list[dict] = []
    for filepath in filepaths:
        entries.extend(parse_all_with_uuids(filepath))
    return [entry["uuid"] for entry in active_chain(entries) if _entry_expects_message(entry)]


def _is_contiguous_suffix(indices: list[int], total: int) -> bool:
    if not indices:
        return False
    return indices == list(range(indices[0], total))


def _source_fingerprint(filepaths: list[Path]) -> str | None:
    """Return a deterministic stat fingerprint, or None if any source is missing.

    Any other stat failure raises ``OSError``.
    """
    parts: list[str] = [f"active-chain-v{ACTIVE_CHAIN_VERSION}"]
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


def _db_coverage_fingerprint(cursor: sqlite3.Cursor, session_id: int) -> str:
    """Return a token combining message-UUID membership and branch_messages
    linkage, for cache validation.

    UUID membership alone (the original fingerprint) can't see a linking-only
    regression: a message row can exist while its branch_messages link was
    dropped by a buggy diff, and that leaves the message row itself, and therefore
    the UUID-only fingerprint, unchanged. Folding in per-active-branch linked
    message *membership* (not just a count) makes that class of regression
    invalidate the ingestion_check_cache the same way a content regression
    already does. A bare per-branch count would miss a same-count
    substitution — one link removed and a different one added, net count for
    that branch unchanged — so the fingerprint lists which messages (by
    stable UUID, not the internal messages.id) are linked to each branch.
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
    branch_rows = cursor.execute(
        """
        SELECT id
        FROM branches
        WHERE session_id = ? AND is_active = 1
        ORDER BY id
        """,
        (session_id,),
    ).fetchall()
    link_rows = cursor.execute(
        """
        SELECT bm.branch_id, m.uuid
        FROM branch_messages bm
        JOIN branches b ON b.id = bm.branch_id
        JOIN messages m ON m.id = bm.message_id
        WHERE b.session_id = ? AND b.is_active = 1
        ORDER BY bm.branch_id, m.uuid
        """,
        (session_id,),
    ).fetchall()
    message_part = "\n".join(row[0] for row in message_rows)
    # branch_part records active-branch existence independently of link_part
    # (an INNER JOIN starting from branch_messages is otherwise blind to a
    # branch with zero links — the all-links-dropped limit of the same
    # substitution bug link_part exists to catch).
    branch_part = "\n".join(str(row[0]) for row in branch_rows)
    link_part = "\n".join(f"{branch_id}:{uuid}" for branch_id, uuid in link_rows)
    return message_part + "\x00" + branch_part + "\x00" + link_part


def _cached_ok_fingerprint(cursor: sqlite3.Cursor, session_uuid: str) -> tuple[str, str] | None:
    row = cursor.execute(
        "SELECT source_fingerprint, db_coverage_fingerprint FROM ingestion_check_cache WHERE session_uuid = ?",
        (session_uuid,),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def _record_ok_rows(cursor: sqlite3.Cursor, rows: list[OkCacheRow]) -> None:
    cursor.executemany(
        """
        INSERT OR REPLACE INTO ingestion_check_cache
        (session_uuid, source_fingerprint, db_coverage_fingerprint)
        VALUES (?, ?, ?)
        """,
        rows,
    )


def classify_sessions(
    cursor,
    sources: dict[str, dict[str, list[Path]]],
    now: Instant,
    stale_tail_seconds: int,
) -> Iterator[SessionVerdict]:
    """Yield a SessionVerdict for each session with a verdict.

    category is one of "ok", "pending_tail", "stale_tail", "ingestion_gap", or
    "missing_source" (the rare case where a source file that was present when
    ``sources`` was built has since disappeared — a stat-time TOCTOU race, not
    the more common paths["missing"] case below — or can't be read: one bad
    transcript must not stop every other session from being classified).
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

        try:
            source_fingerprint = _source_fingerprint(filepaths)
        except OSError:
            log.exception("cannot stat transcript source", extra={"session_uuid": session_uuid})
            yield SessionVerdict(session_uuid, "missing_source", session_id, [])
            continue
        if source_fingerprint is None:
            yield SessionVerdict(session_uuid, "missing_source", session_id, [])
            continue

        db_coverage_fingerprint = _db_coverage_fingerprint(cursor, session_id)

        if _cached_ok_fingerprint(cursor, session_uuid) == (source_fingerprint, db_coverage_fingerprint):
            yield SessionVerdict(session_uuid, "ok", session_id, [])
            continue

        # Scoped to the active branch's branch_messages links, not just "any
        # message row exists for this session" — a message row can survive
        # while its link to the active branch is dropped or substituted
        # (e.g. by a per-file diff that doesn't see cross-file links), and a
        # row-existence check alone is blind to that: the UUID is still
        # "present" in messages even though it's no longer reachable from the
        # active branch, so a link corruption would classify as "ok" and be
        # re-cached that way, forever hiding it from find_repairable_sessions.
        existing_msg_uuids = {
            row[0]
            for row in cursor.execute(
                """
                SELECT m.uuid
                FROM branch_messages bm
                JOIN branches b ON b.id = bm.branch_id
                JOIN messages m ON m.id = bm.message_id
                WHERE b.session_id = ? AND b.is_active = 1 AND m.uuid IS NOT NULL
                """,
                (session_id,),
            ).fetchall()
        }

        try:
            expected = _expected_uuids(filepaths)
            missing_indices = [i for i, uuid in enumerate(expected) if uuid not in existing_msg_uuids]
            if not missing_indices:
                new_ok_row = OkCacheRow(session_uuid, source_fingerprint, db_coverage_fingerprint)
                yield SessionVerdict(session_uuid, "ok", session_id, [], new_ok_row)
                continue

            if _is_contiguous_suffix(missing_indices, len(expected)):
                newest_mtime = max(Instant.from_timestamp(path.stat().st_mtime) for path in filepaths)
                if (now - newest_mtime).total("seconds") <= stale_tail_seconds:
                    yield SessionVerdict(session_uuid, "pending_tail", session_id, missing_indices)
                else:
                    yield SessionVerdict(session_uuid, "stale_tail", session_id, missing_indices)
            else:
                yield SessionVerdict(session_uuid, "ingestion_gap", session_id, missing_indices)
        except FileNotFoundError as exc:
            log.warning(
                "transcript source missing while computing ingestion fingerprint; "
                "session will be counted as missing_source",
                extra={"path": str(exc.filename) if exc.filename else None},
            )
            yield SessionVerdict(session_uuid, "missing_source", session_id, [])
        except Exception:
            # Same containment as repair_sessions' poison-transcript handling.
            log.exception(
                "cannot read transcript source; session counted as missing_source",
                extra={"session_uuid": session_uuid},
            )
            yield SessionVerdict(session_uuid, "missing_source", session_id, [])


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
    with import-log rows but no surviving JSONL, or with a JSONL that can't be read,
    is counted as ``missing_source``.
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
    new_ok_rows: list[OkCacheRow] = []

    for session_uuid, paths in sources.items():
        if not paths["missing"]:
            continue
        if cursor.execute("SELECT 1 FROM sessions WHERE uuid = ?", (session_uuid,)).fetchone() is not None:
            summary["sessions_checked"] += 1
            summary["missing_source_sessions"] += 1

    now = Instant.now()
    for _session_uuid, category, _session_id, missing_indices, new_ok_row in classify_sessions(
        cursor, sources, now, stale_tail_seconds
    ):
        summary["sessions_checked"] += 1

        if category == "missing_source":
            summary["missing_source_sessions"] += 1
            continue

        if category == "ok":
            summary["ok_sessions"] += 1
            if new_ok_row is not None:
                new_ok_rows.append(new_ok_row)
            continue

        turns = len(missing_indices)
        summary[f"{category}_sessions"] += 1
        summary[f"{category}_turns"] += turns

    _record_ok_rows(cursor, new_ok_rows)

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
    still being written) and missing_source (no readable JSONL to reimport
    from).

    Also records each ok verdict it had to parse for, so the next run skips those
    transcripts. Commits in batches, then once more before returning, so a kill
    keeps its progress and the caller's repair loop starts with no open
    transaction (see hooks/import_repair.py)."""
    cursor = conn.cursor()
    if sources is None:
        sources = import_log_source_index(cursor)

    now = Instant.now()
    candidates: list[tuple[str, int, int | None, list[Path]]] = []
    new_ok_rows: list[OkCacheRow] = []
    for session_uuid, category, session_id, _missing_indices, new_ok_row in classify_sessions(
        cursor, sources, now, stale_tail_seconds
    ):
        if new_ok_row is not None:
            new_ok_rows.append(new_ok_row)
            if len(new_ok_rows) >= OK_CACHE_COMMIT_BATCH:
                _record_ok_rows(cursor, new_ok_rows)
                conn.commit()
                new_ok_rows = []
        if category not in ("stale_tail", "ingestion_gap"):
            continue
        project_row = cursor.execute("SELECT project_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        project_id = project_row[0] if project_row is not None else None
        filepaths = sources[session_uuid]["existing"]
        candidates.append((session_uuid, session_id, project_id, filepaths))

    _record_ok_rows(cursor, new_ok_rows)
    conn.commit()
    return candidates


def reclassify_session(
    conn: Connection,
    session_uuid: str,
    filepaths: list[Path],
    *,
    stale_tail_seconds: int = STALE_TAIL_SECONDS,
) -> SessionCategory:
    """Return the current classification ("ok", "pending_tail", "stale_tail",
    "ingestion_gap", or "missing_source" for a no-session-row race or an
    unreadable transcript) for one session, given its known-existing filepaths.

    Builds a one-entry sources dict and delegates to classify_sessions — the
    ingestion_check_cache short-circuit inside it naturally misses here after a
    real repair (the DB coverage fingerprint changed), so this reflects genuinely
    current state rather than a stale cached verdict.
    """
    cursor = conn.cursor()
    sources = {session_uuid: {"existing": filepaths, "missing": []}}
    result = next(classify_sessions(cursor, sources, Instant.now(), stale_tail_seconds), None)
    return result[1] if result else "missing_source"
