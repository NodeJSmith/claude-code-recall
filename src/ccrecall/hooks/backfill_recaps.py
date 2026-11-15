"""Generate recaps for sessions that have stopped (`ccrecall backfill recaps`).

Built to run unattended on a timer, so each run is bounded (``limit`` defaults to
``recaps.DEFAULT_LIMIT`` calls) and fails loudly:

  - It imports first, so idleness and currency are judged against what the
    transcripts hold now, not what the last hook happened to sync. When another
    import holds the import lock, it proceeds without waiting: that import is
    already catching up.
  - It selects sessions with ``recaps.select_candidates`` and makes one Claude
    call per session, committing each result on its own.
  - A failure specific to one session records the error, cools that session down
    for a day, and the run continues; the run then finishes ``partial``.
  - A failure that would hit every session (not logged in, rate limited, Claude
    Code not starting) ends the run ``failed`` at once and cools nothing down, so
    the next scheduled run is the retry. ``MAX_CONSECUTIVE_FAILURES`` per-session
    failures in a row also end it ``failed``, as a backstop for a run-level failure
    the mapping misses.
  - A streak of per-session failures holds its cooldowns until something shows the
    failures really were per-session: a success after them, or the run ending
    without stopping early. A run that stops on the backstop or a run-level failure
    discards the streak's cooldowns, so a cause the mapping misses (a boot run
    before the network is up, a retired model) never cools the newest sessions.

The caller (the CLI command) holds this job's lock.
"""

import contextlib
import json
import logging
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from whenever import Instant

from ccrecall import job_lock, recaps, run_history
from ccrecall.config import (
    DEFAULT_DB_PATH,
    JOB_KEY_IMPORT,
    get_db_path,
    load_settings_for_db,
    setup_logging,
)
from ccrecall.db import DEFAULT_PROJECTS_DIR, get_connection
from ccrecall.hooks import import_conversations

EXIT_OK = 0
EXIT_FAILED = 1


@dataclass
class Counts:
    """A run's tallies; their field names are `--json` output keys, a public contract."""

    selected: int = 0
    generated: int = 0
    failed: int = 0


@dataclass(frozen=True)
class SessionFailure:
    """A failed attempt: what to record as the session's cooldown, and why the run may stop.

    ``run_reason`` is None for a failure that is this session's alone.
    """

    session_id: int
    error: str
    attempted_at: Instant
    run_reason: str | None


@dataclass(frozen=True)
class RunResult:
    outcome: str
    reason: str | None
    counts: Counts


def run(
    *,
    days: int = recaps.DEFAULT_DAYS,
    limit: int | None = recaps.DEFAULT_LIMIT,
    idle_minutes: int = recaps.DEFAULT_IDLE_MINUTES,
    json_mode: bool = False,
    verbose: bool = False,
    db: Path = DEFAULT_DB_PATH,
    projects_dir: Path = DEFAULT_PROJECTS_DIR,
) -> int:
    """Generate recaps for idle sessions; exit 0 when every attempted session succeeded."""
    if days < 1:
        raise ValueError("days must be >= 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    if idle_minutes < 0:
        raise ValueError("idle_minutes must be >= 0")

    settings = load_settings_for_db(db)
    logger = setup_logging(settings, process_name="backfill-recaps", verbose=verbose)
    with run_history.record_run(get_db_path(settings), run_history.KIND_RECAPS) as recorder:
        import_first(settings, logger, db=db, projects_dir=projects_dir)
        result = generate_recaps(settings, logger, days=days, limit=limit, idle_minutes=idle_minutes)
        recorder.finish(result.outcome, result.reason)
    report(result, json_mode=json_mode)
    logger.info(
        "recap run finished",
        extra={"outcome": result.outcome, "reason": result.reason, **asdict(result.counts)},
    )
    return EXIT_OK if result.outcome == run_history.OUTCOME_OK else EXIT_FAILED


def import_first(settings: dict, logger: logging.Logger, *, db: Path, projects_dir: Path) -> None:
    """Run the ordinary import, unless another import already holds its lock."""
    lock = job_lock.acquire(JOB_KEY_IMPORT, scope=job_lock.import_scope(repair_gaps=False, db=db))
    if lock is None:
        logger.info("another import is running; selecting from what it has imported so far")
        return
    # The import prints progress for a person; keep stdout for this command's own result.
    # It writes db_path into the settings it gets, so it gets a copy.
    with lock, contextlib.redirect_stdout(sys.stderr):
        import_conversations.import_locked(
            dict(settings), logger, db=db, projects_dir=projects_dir, project=None, repair_gaps=False
        )


def generate_recaps(
    settings: dict,
    logger: logging.Logger,
    *,
    days: int,
    limit: int | None,
    idle_minutes: int,
) -> RunResult:
    """Select and recap sessions, committing each result on its own."""
    counts = Counts()
    # Per-session failures awaiting proof they really were per-session: written on a
    # later success or a normal end of the run, dropped when the run stops early.
    streak: list[SessionFailure] = []
    with get_connection(settings) as conn:
        candidates = recaps.select_candidates(
            conn,
            now=Instant.now(),
            days=days,
            limit=limit,
            idle_minutes=idle_minutes,
            exclude_projects=settings.get("exclude_projects") or [],
        )
        counts.selected = len(candidates)
        logger.info("selected sessions for recaps", extra={"count": len(candidates)})
        for candidate in candidates:
            failure = attempt(conn, logger, candidate, counts)
            if failure is None:
                record_cooldowns(conn, streak)
                continue
            if failure.run_reason is not None:
                logger.error(
                    "recap call failed in a way every session would; stopping",
                    extra={"reason": failure.run_reason, "cooldowns_discarded": len(streak)},
                )
                return RunResult(run_history.OUTCOME_FAILED, failure.run_reason, counts)
            streak.append(failure)
            if len(streak) >= recaps.MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "recap calls failed repeatedly; stopping without cooling those sessions down",
                    extra={"consecutive_failures": len(streak)},
                )
                return RunResult(run_history.OUTCOME_FAILED, run_history.REASON_REPEATED_FAILURES, counts)
        record_cooldowns(conn, streak)
    if counts.failed:
        return RunResult(run_history.OUTCOME_PARTIAL, run_history.REASON_ERROR, counts)
    return RunResult(run_history.OUTCOME_OK, None, counts)


def record_cooldowns(conn: sqlite3.Connection, failures: list[SessionFailure]) -> None:
    """Store each failure with its time, which starts that session's cooldown, and empty the list."""
    for failure in failures:
        recaps.write_failure(conn, failure.session_id, failure.error, failure.attempted_at)
    failures.clear()


def attempt(
    conn: sqlite3.Connection, logger: logging.Logger, candidate: recaps.Candidate, counts: Counts
) -> SessionFailure | None:
    """Recap one session, storing a success; return the failure, if any, unstored.

    The caller decides whether a per-session failure becomes a cooldown. A run-level
    one never does, so the session stays eligible for the next run.
    """
    recap_input = recaps.build_input(conn, candidate.branch_id)
    if recap_input is None:
        # Selection only picks branches with input rows, so they vanished since.
        logger.warning("session lost its messages after selection; skipping", extra={"session": candidate.session_uuid})
        return None
    attempted_at = Instant.now()
    try:
        text = recaps.generate(recap_input.prompt)
    except recaps.RecapCallError as exc:
        counts.failed += 1
        logger.warning(
            "recap failed",
            extra={"session": candidate.session_uuid, "error": str(exc), "run_reason": exc.run_reason},
        )
        return SessionFailure(candidate.session_id, str(exc), attempted_at, exc.run_reason)
    recaps.write_success(conn, candidate.session_id, text, recap_input.as_of, attempted_at)
    counts.generated += 1
    logger.info("recap generated", extra={"session": candidate.session_uuid})
    return None


def report(result: RunResult, *, json_mode: bool) -> None:
    counts = result.counts
    if json_mode:
        print(json.dumps({"outcome": result.outcome, "reason": result.reason, **asdict(counts)}))
        return
    line = f"Recaps: {counts.generated} generated, {counts.failed} failed, {counts.selected} selected"
    if result.outcome == run_history.OUTCOME_FAILED and result.reason is not None:
        line += f"; stopped: {run_history.describe_reason(result.reason)}"
    print(line)
