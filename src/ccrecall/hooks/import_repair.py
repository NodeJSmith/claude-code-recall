"""Force-reimport execution loop for `ccrecall import --repair-gaps`.

Split out of hooks/import_conversations.py (already ~430 lines) rather than
growing it further — see design/specs/016-stale-tail-import-repair/design.md.

Durability model (deliberate, not an oversight): unlike import_project, this
loop never wraps its candidates in an outer transaction. find_repairable_sessions
is pure-SELECT and the call site runs after _run()'s per-project loop has
already committed, so each candidate's SAVEPOINT/RELEASE here commits
independently and durably the moment it completes. A repair batch has no
cross-candidate invariant to protect: a run interrupted partway through keeps
whatever it already fixed, and is safe to resume by re-running --repair-gaps
(force=True reimports are idempotent — sync_session's UUID-dedup insert is a
safe no-op on an already-fixed session). Do not add a `BEGIN` guard around the
candidate loop to make this "more atomic" — that would trade resumability for
an invariant this loop doesn't need.

Multi-file candidates (a parent session plus its agent-*.jsonl subagent
transcripts) are processed as ONE merged unit via import_conversations.
import_session_group, not one file at a time — see design/specs/016-stale
-tail-import-repair Finding 1. Reimporting files one at a time computes each
file's branch_messages diff from that file's own entries in isolation, so a
later file's diff can silently drop a link that only exists via an earlier
sibling file. Single-file candidates still go through import_session
unchanged (there is no cross-file scoping problem to fix for them).
"""

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from ccrecall import ingestion_status
from ccrecall.hooks import import_conversations
from ccrecall.models import LOGGER_NAME

log = logging.getLogger(LOGGER_NAME)

PROGRESS_LOG_INTERVAL = 10


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

    force=True (via import_session/import_session_group) bypasses the stat/hash
    skip gates; sync_session's/sync_session_group's UUID-dedup insert
    (insert_new_messages) is already idempotent, so re-running this on an
    already-fixed session is a safe no-op that recovers 0 messages.

    A candidate with exactly one file goes through import_session (force=True)
    unchanged. A candidate with multiple files goes through
    import_session_group instead, which parses and syncs every file's entries
    in a single pass — see the module docstring and Finding 1.

    Each candidate's SAVEPOINT acquisition, force-reimport, and RELEASE are
    all wrapped by the same try/except: an OperationalError raised by the
    force-reimport itself (a genuine infrastructure failure — full disk,
    corrupt DB, incompatible schema) still aborts the whole remaining batch
    by re-raising, matching import_project's existing convention. But an
    OperationalError on the SAVEPOINT/RELEASE statements themselves (a
    transient, candidate-local problem, not evidence the DB is broken) is
    treated like any other per-candidate failure instead — logged, counted,
    and the batch continues — rather than propagating and aborting every
    remaining candidate. A failed RELEASE is recovered by explicitly rolling
    back to and releasing the same savepoint, so the connection returns to a
    known, zero-depth state before the next candidate starts; if that
    recovery itself fails, the connection's savepoint stack can no longer be
    trusted, and the failure escalates to the same abort-the-batch handling
    as a genuine force-reimport infrastructure failure.

    After a candidate's files are processed (and none raised), the session's
    classification is re-checked via ingestion_status.reclassify_session:
      - now "ok" -> sessions_repaired
      - still "stale_tail"/"ingestion_gap" (or the rarer "pending_tail"/
        "missing_source" edge cases reclassify_session can theoretically
        return) -> sessions_unrepairable (the on-disk transcript genuinely
        lacks the expected content; reattempting won't help)
    If any of the candidate's files raised, it counts toward sessions_failed
    instead, and reclassification is skipped for it (a poison-file failure is
    an operational problem, not evidence the source content is missing). A
    candidate with no project_id on record is also unrepairable — a missing
    project_id is a permanent data-integrity condition retrying can never
    resolve, unlike a transient operational failure — so it is counted under
    sessions_unrepairable too, not sessions_failed.

    messages_recovered is measured per candidate as the delta in that
    session's total `messages` row count from before its file loop to after
    (whether or not the loop completed without a failure) — NOT a sum of
    import_session's own returned msg_count, which is a per-file *total*
    session message count (matching import_project's existing "total per
    file" convention), not a per-call delta. Summing that total across a
    multi-file candidate would overcount, and it would never read 0 on an
    idempotent re-run of an already-fixed session even though nothing new was
    inserted. The before/after delta gives the correct "how many messages did
    this repair actually add" answer in both cases. The session_id used for
    the after-count is re-resolved from sessions.uuid immediately before
    computing it, not the (possibly stale) id captured before the file loop:
    import_session can delete a session's row entirely when its message count
    hits 0, and a later re-sync for the same UUID gets a new id via
    upsert_session's ON CONFLICT(uuid) DO UPDATE — trusting the pre-loop id
    would silently report 0 messages recovered for a real recovery if that
    churn happened mid-candidate.
    """
    sessions_repaired = 0
    messages_recovered = 0
    sessions_failed = 0
    sessions_unrepairable = 0
    total_candidates = len(candidates)

    for index, (session_uuid, session_id, project_id, filepaths) in enumerate(candidates, start=1):
        if project_id is None:
            # sessions.project_id is nullable; import_session requires a concrete
            # project_id to force-reimport into. A candidate with no project_id
            # is a permanent data-integrity condition — project_id will never
            # become non-null via retry — not a transient operational failure,
            # so it counts as unrepairable rather than failed.
            log.error(
                "Skipping repair for session %s — no project_id on record (unrepairable, not retryable)",
                session_uuid,
            )
            sessions_unrepairable += 1
            _log_progress(index, total_candidates, sessions_repaired, sessions_failed, sessions_unrepairable)
            continue

        candidate_failed = False
        count_before = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]

        try:
            conn.execute("SAVEPOINT import_candidate")
        except sqlite3.OperationalError:
            log.exception(
                "Failed to acquire SAVEPOINT while repairing session %s — counting as failed, continuing batch",
                session_uuid,
            )
            sessions_failed += 1
            on_reclaim()
            _log_progress(index, total_candidates, sessions_repaired, sessions_failed, sessions_unrepairable)
            continue

        try:
            if len(filepaths) == 1:
                import_conversations.import_session(conn, filepaths[0], project_id, force=True)
            else:
                import_conversations.import_session_group(conn, filepaths, project_id)
        except sqlite3.OperationalError:
            conn.execute("ROLLBACK TO SAVEPOINT import_candidate")
            conn.execute("RELEASE SAVEPOINT import_candidate")
            log.exception(
                "Database-level failure repairing session %s — aborting run",
                session_uuid,
            )
            raise
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT import_candidate")
            conn.execute("RELEASE SAVEPOINT import_candidate")
            log.exception(
                "Skipping poison transcript(s) while repairing session %s",
                session_uuid,
            )
            candidate_failed = True
        else:
            try:
                conn.execute("RELEASE SAVEPOINT import_candidate")
            except sqlite3.OperationalError:
                # A failed RELEASE leaves the savepoint open on the connection's
                # stack instead of discarding it — left alone, the next
                # candidate's SAVEPOINT nests inside this "failed" one instead
                # of starting its own independent, durably-committing
                # transaction (see the module docstring's durability model).
                # Roll back the candidate's writes and release the savepoint
                # explicitly so the connection returns to a clean, zero-depth
                # state before the next candidate starts.
                try:
                    conn.execute("ROLLBACK TO SAVEPOINT import_candidate")
                    conn.execute("RELEASE SAVEPOINT import_candidate")
                except sqlite3.OperationalError:
                    # The recovery itself failed under the same transient
                    # condition — the connection's savepoint stack can no
                    # longer be trusted to be at a known depth, so this is a
                    # genuine infrastructure failure, not a candidate-local
                    # one. Abort the whole batch rather than let subsequent
                    # candidates nest inside an unknown state, matching the
                    # force-reimport OperationalError branch above.
                    log.exception(
                        "Failed to recover from RELEASE SAVEPOINT failure while repairing session %s — aborting run",
                        session_uuid,
                    )
                    raise
                log.exception(
                    "Failed to RELEASE SAVEPOINT while repairing session %s — counting as failed, continuing batch",
                    session_uuid,
                )
                candidate_failed = True

        on_reclaim()

        resolved = conn.execute("SELECT id FROM sessions WHERE uuid = ?", (session_uuid,)).fetchone()
        if resolved is not None:
            count_after = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (resolved[0],)).fetchone()[
                0
            ]
        else:
            count_after = 0
        delta = count_after - count_before
        if delta < 0:
            log.warning(
                "negative message delta while repairing session %s: before=%d after=%d",
                session_uuid,
                count_before,
                count_after,
            )
        messages_recovered += max(delta, 0)

        if candidate_failed:
            sessions_failed += 1
            _log_progress(index, total_candidates, sessions_repaired, sessions_failed, sessions_unrepairable)
            continue

        category = ingestion_status.reclassify_session(conn, session_uuid, filepaths)
        if category == "ok":
            sessions_repaired += 1
        else:
            sessions_unrepairable += 1

        _log_progress(index, total_candidates, sessions_repaired, sessions_failed, sessions_unrepairable)

    return sessions_repaired, messages_recovered, sessions_failed, sessions_unrepairable


def _log_progress(index: int, total: int, repaired: int, failed: int, unrepairable: int) -> None:
    """Log running totals every PROGRESS_LOG_INTERVAL candidates (and on the
    final one), so a long-running repair leaves a progress trail in the log a
    user can tail instead of going silent until the whole batch finishes."""
    if index % PROGRESS_LOG_INTERVAL == 0 or index == total:
        log.info(
            "repair progress: %d/%d candidates processed (repaired=%d, failed=%d, unrepairable=%d)",
            index,
            total,
            repaired,
            failed,
            unrepairable,
        )
