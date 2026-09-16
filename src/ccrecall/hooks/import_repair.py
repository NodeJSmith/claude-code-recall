"""Force-reimport execution loop for `ccrecall import --repair-gaps`.

Split out of hooks/import_conversations.py (already ~430 lines) rather than
growing it further — see design/specs/016-stale-tail-import-repair/design.md.

Durability model (deliberate, not an oversight): unlike import_project, this
loop never wraps its candidates in an outer transaction. find_repairable_sessions
is pure-SELECT and the call site runs after _run()'s per-project loop has
already committed, so each file's SAVEPOINT/RELEASE here commits independently
and durably the moment it completes. A repair batch has no cross-candidate
invariant to protect: a run interrupted partway through keeps whatever it
already fixed, and is safe to resume by re-running --repair-gaps (force=True
reimports are idempotent — sync_session's UUID-dedup insert is a safe no-op on
an already-fixed session). Do not add a `BEGIN` guard around the candidate
loop to make this "more atomic" — that would trade resumability for an
invariant this loop doesn't need.
"""

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from ccrecall import ingestion_status
from ccrecall.hooks import import_conversations
from ccrecall.models import LOGGER_NAME
from ccrecall.parsing import sort_session_files

log = logging.getLogger(LOGGER_NAME)


def _noop() -> None:
    pass


def repair_sessions(
    conn: sqlite3.Connection,
    candidates: list[tuple[str, int, int | None, list[Path]]],
    on_reclaim: Callable[[], None] = _noop,
) -> tuple[int, int, int, int]:
    """Force-reimport every (session_uuid, session_id, project_id, filepaths)
    candidate — matching ingestion_status.find_repairable_sessions's return
    shape, where project_id is None when the session's project_id column is
    nullable and unset. Returns (sessions_repaired, messages_recovered,
    sessions_failed, sessions_unrepairable).

    force=True on import_session bypasses the stat/hash skip gates; sync_session's
    UUID-dedup insert (insert_new_messages) is already idempotent, so re-running
    this on an already-fixed session is a safe no-op that recovers 0 messages.

    After a candidate's files are processed (and none raised), the session's
    classification is re-checked via ingestion_status.reclassify_session:
      - now "ok" -> sessions_repaired
      - still "stale_tail"/"ingestion_gap" (or the rarer "pending_tail"/
        "missing_source" edge cases reclassify_session can theoretically
        return) -> sessions_unrepairable (the on-disk transcript genuinely
        lacks the expected content; reattempting won't help)
    If any of the candidate's files raised, it counts toward sessions_failed
    instead, and reclassification is skipped for it (a poison-file failure is
    an operational problem, not evidence the source content is missing).

    messages_recovered is measured per candidate as the delta in that
    session's total `messages` row count from before its file loop to after
    (whether or not the loop completed without a failure) — NOT a sum of
    import_session's own returned msg_count, which is a per-file *total*
    session message count (matching import_project's existing "total per
    file" convention), not a per-call delta. Summing that total across a
    multi-file candidate would overcount, and it would never read 0 on an
    idempotent re-run of an already-fixed session even though nothing new was
    inserted. The before/after delta gives the correct "how many messages did
    this repair actually add" answer in both cases.
    """
    sessions_repaired = 0
    messages_recovered = 0
    sessions_failed = 0
    sessions_unrepairable = 0

    for session_uuid, session_id, project_id, filepaths in candidates:
        if project_id is None:
            # sessions.project_id is nullable; import_session requires a concrete
            # project_id to force-reimport into. A candidate with no project_id
            # is a data-integrity problem this loop cannot resolve on its own —
            # treat it the same as a poison file rather than crashing or
            # silently dropping the candidate.
            log.error(
                "Skipping repair for session %s — no project_id on record",
                session_uuid,
            )
            sessions_failed += 1
            continue

        candidate_failed = False
        count_before = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]

        for target in sort_session_files(filepaths):
            conn.execute("SAVEPOINT import_file")
            try:
                import_conversations.import_session(conn, target, project_id, force=True)
            except sqlite3.OperationalError:
                conn.execute("ROLLBACK TO SAVEPOINT import_file")
                conn.execute("RELEASE SAVEPOINT import_file")
                log.exception(
                    "Database-level failure repairing %s (session %s) — aborting run",
                    target,
                    session_uuid,
                )
                raise
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT import_file")
                conn.execute("RELEASE SAVEPOINT import_file")
                log.exception(
                    "Skipping poison transcript file %s while repairing session %s",
                    target,
                    session_uuid,
                )
                candidate_failed = True
                on_reclaim()
                continue

            conn.execute("RELEASE SAVEPOINT import_file")
            on_reclaim()

        count_after = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]
        messages_recovered += max(count_after - count_before, 0)

        if candidate_failed:
            sessions_failed += 1
            continue

        category = ingestion_status.reclassify_session(conn, session_uuid, filepaths)
        if category == "ok":
            sessions_repaired += 1
        else:
            sessions_unrepairable += 1

    return sessions_repaired, messages_recovered, sessions_failed, sessions_unrepairable
