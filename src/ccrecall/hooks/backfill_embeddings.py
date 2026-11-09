"""Backfill chunk-grain embeddings for existing active-leaf branches.

Opt-in: invoke manually via `ccrecall backfill embeddings` to seed historical
embeddings. NOT auto-spawned on SessionStart (embedding inference is CPU-bound);
forward coverage comes from embed-on-write instead.
Processes branches in batches, committing each branch on its own with no lock
held during inference, and marks per-row content errors with
embedding_version = -1 to avoid infinite retry. A batch selects only ids above
the last one seen, so a run attempts each branch at most once; a branch it
couldn't finish waits for the next run.
Each run first clears the watermark on branches stamped as embedded but missing
exchanges (`clear_false_watermarks`), since selection can't see a chunk that
was never created.

Exception taxonomy is catch-all-then-filter-infra, matching
`backfill_summaries.py`: any exception raised while embedding one branch is a
content error for that row (mark the sentinel, continue) unless it's a
`sqlite3.Error`/`OSError` — an infra/DB failure, which propagates to abort the
run without poisoning the row. (A lock timeout is the exception: it costs only
that branch, which the next run retries, unless three in a row abort the run.)
A narrow content-error allow-list would let an unanticipated exception type fall through to the infra path unmarked, and
since this backfill is built to run unattended on a recurring schedule (see
below), an unmarked row wedges every scheduled run indefinitely: the run
aborts on that same row every time, blocking every branch behind it in `id`
order from ever being embedded — the same failure mode as issue #178, fixed
here as issue #201.

`backfill_tool_content.py` keeps the older narrow-allow-list taxonomy
deliberately: it's a one-off migration command with no scheduler-oriented
design, so a wedged row there just sits idle until someone reruns it by hand.

Two-level failure distinction:
  - Model/session failure: abort the whole run, mark NOTHING.
  - Per-row content error (tokenizer overflow, malformed content, or any
    other unanticipated exception): mark that row embedding_version = -1 and
    continue.

fastembed/onnxruntime don't expose a narrow, stable exception type that
distinguishes "this branch's content broke the model" from "the model/session
itself is broken" — both surface as ordinary `Exception` subclasses. So the
distinction above is enforced with a live re-probe rather than exception-type
classification: when `_embed_one_branch`'s catch-all clause fires, it calls
`_model_still_healthy()`, which re-runs `embed_batch` — the same call
`embed_branch_chunks` uses for real branches — on a trivial fixed string.
If the probe succeeds, the failure was specific to that branch's content —
mark the sentinel and continue as before. If the probe also raises, the
model/session is the thing that broke; the caught exception is re-raised
instead of marking the row, so it flows through the same infra-abort path as
a `sqlite3.Error`/`OSError` (`run()` returns `EXIT_ABORT`). Branches finished
earlier in the run stay committed, and the row that triggered the failure stays
eligible for retry on the next run.

Built to run unattended (systemd timer): `--status [--json]` reports progress
without embedding, progress lines carry elapsed/ETA, and abort paths exit
non-zero so the scheduler sees the failure.

Query construction/constants live in `backfill_query.py`; status reporting
lives in `backfill_status.py`; the outer batch-loop scaffolding shared with
`backfill_tool_content.run()` lives in `backfill_runner.py`. This module
keeps the `run()` orchestrator (progress reporting wired into
`run_batch_loop`'s callbacks) plus the shared `reclaim_memory` helper from
`subprocess_utils` (gc + malloc_trim between batches, the same RSS
discipline as import_conversations). Per-item processing and exception
taxonomy live in `_embed_one_branch`, called once per row from
`process_batch`'s per-item loop.
"""

import contextlib
import json
import logging
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path
from typing import NamedTuple

from ccrecall import run_history
from ccrecall.config import DEFAULT_DB_PATH, get_db_path, load_settings_for_db, setup_logging
from ccrecall.db import CONTENT_ERROR_VERSION, get_connection
from ccrecall.db_base import commit_single_write, is_database_locked
from ccrecall.db_vec import chunk_vec_queryable, find_stamped_branches_missing_chunks
from ccrecall.embed_ops import embed_branch_chunks
from ccrecall.embeddings import (
    DEFAULT_EMBED_THREADS,
    MODEL_TOKEN_LIMIT,
    embed_batch,
    model_available,
)
from ccrecall.health import (
    REASON_MODEL_UNAVAILABLE,
    REASON_VEC_UNAVAILABLE,
    clear_embedding_failure,
    record_embedding_failure,
)
from ccrecall.hooks.backfill_query import (
    BACKFILL_BATCH_DELAY_SECONDS,
    BACKFILL_NICE_LEVEL,
    BATCH_SIZE,
    DEFAULT_PROGRESS_EVERY,
    EXIT_ABORT,
    EXIT_OK,
    build_selection,
)
from ccrecall.hooks.backfill_runner import limit_reached, lower_scheduling_priority, run_batch_loop
from ccrecall.hooks.backfill_status import format_duration, run_status
from ccrecall.hooks.subprocess_utils import reclaim_memory, try_load_libc

_PRINT_PREFIX = "ccrecall backfill embeddings"
_LOG_PREFIX = "Backfill embeddings"
_WARMUP_BRANCHES = 5
_RATE_WINDOW = 30
# A lock timeout costs one branch, but this many in a row means a writer is stuck,
# and walking the rest of the backlog would only wait out each branch's timeout.
_MAX_CONSECUTIVE_LOCK_TIMEOUTS = 3


def run(
    *,
    status: bool = False,
    json_mode: bool = False,
    days: int | None = None,
    limit: int | None = None,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
    threads: int = DEFAULT_EMBED_THREADS,
    verbose: bool = False,
    db: Path = DEFAULT_DB_PATH,
) -> int:
    """Embed active-leaf branch exchanges at chunk grain (opt-in; not auto-spawned)."""
    # Backstop for direct callers; the CLI validators reject <1 before reaching
    # here. A negative --days flips to a future date (no-op); --limit < 1 stops
    # the loop immediately — both silent. Raise rather than clamp (unlike
    # recent/search, which clamp to range): a silently wrong window is worse here.
    if days is not None and days < 1:
        raise ValueError("days must be >= 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")

    settings = load_settings_for_db(db)
    logger = setup_logging(settings, process_name="backfill-embed", verbose=verbose)

    if status:
        return run_status(days=days, json_mode=json_mode, settings=settings, logger=logger)

    # Only an unscoped run advances last_ok: a scoped one can finish ok having
    # worked a sliver of the backlog.
    scoped = days is not None or limit is not None
    with run_history.record_run(get_db_path(settings), run_history.KIND_BACKFILL, advances_ok=not scoped) as recorder:
        return _run(
            recorder,
            settings=settings,
            logger=logger,
            json_mode=json_mode,
            days=days,
            limit=limit,
            progress_every=progress_every,
            threads=threads,
        )


def _run(
    recorder: run_history.RunRecorder,
    *,
    settings: dict,
    logger: logging.Logger,
    json_mode: bool,
    days: int | None,
    limit: int | None,
    progress_every: int,
    threads: int,
) -> int:
    """Run the backfill, setting the run's recorded outcome on every exit path."""
    # Background CPU job: lower scheduling priority so the bounded inference
    # threads yield to interactive work (machines.md thrash risk).
    lower_scheduling_priority(BACKFILL_NICE_LEVEL)

    # ABORT level: check model availability before touching any rows.
    # model_available() warms the singleton session on success — no extra cost.
    # Pass --threads here since this is the call that constructs the session.
    if not model_available(threads=threads):
        with contextlib.suppress(Exception):  # best-effort; sidecar write must not affect exit behavior
            record_embedding_failure(reason=REASON_MODEL_UNAVAILABLE)
        recorder.finish(run_history.OUTCOME_FAILED, run_history.REASON_MODEL_UNAVAILABLE)
        logger.error("%s: model not available, aborting (no rows marked)", _LOG_PREFIX)
        print(
            f"{_PRINT_PREFIX}: model not available, aborting (no rows marked)",
            file=sys.stderr,
        )
        return EXIT_ABORT

    total_updated = 0
    total_processed = 0
    total_inferences = 0
    last_progress = 0
    work_done = 0
    last_selected_id = 0
    consecutive_lock_timeouts = 0
    lock_timeouts = 0
    abort_reason = run_history.REASON_ERROR
    rate_samples: deque[tuple[float, int]] = deque(maxlen=_RATE_WINDOW)
    started = time.monotonic()

    where, params = build_selection(days)
    libc = try_load_libc()

    try:
        with get_connection(settings, load_vec=True) as conn:
            cursor = conn.cursor()

            # ABORT level: chunk_vec must be queryable before any selection runs —
            # the eligibility WHERE references chunks/chunk_vec, which get_connection
            # creates only when sqlite-vec loaded. Without this guard the queries
            # below crash with "no such table: chunk_vec" instead of exiting cleanly.
            if not chunk_vec_queryable(conn):
                with contextlib.suppress(Exception):  # best-effort; sidecar write must not affect exit behavior
                    record_embedding_failure(reason=REASON_VEC_UNAVAILABLE)
                recorder.finish(run_history.OUTCOME_FAILED, run_history.REASON_VECTOR_UNAVAILABLE)
                logger.error("%s: sqlite-vec unavailable, aborting (no rows marked)", _LOG_PREFIX)
                print(
                    f"{_PRINT_PREFIX}: sqlite-vec unavailable, aborting (no rows marked)",
                    file=sys.stderr,
                )
                return EXIT_ABORT

            # Before the eligible count, so healed branches are in this run's
            # totals. Committed now because the first embed's BEGIN IMMEDIATE
            # needs no transaction open.
            clear_false_watermarks(cursor, logger)
            conn.commit()

            # Compute total-eligible count once (avoid per-batch full COUNT).
            cursor.execute(f"SELECT COUNT(*) FROM branches {where}", params)
            total_eligible = cursor.fetchone()[0]
            if limit is not None:
                total_eligible = min(total_eligible, limit)

            # Cost-proportional total: non-notification message count across
            # eligible branches. Excludes notifications to match the
            # include_notifications=False filter on fetch_branch_messages, so
            # total_work and work_done are in the same units.
            # When --limit is active, restrict to the first `limit` branches
            # (by id) so the ETA reflects the limited run, not the full backlog.
            limit_clause = f"ORDER BY id LIMIT {limit}" if limit is not None else ""
            cursor.execute(
                f"""SELECT COUNT(*) FROM branch_messages bm
                    JOIN messages m ON m.id = bm.message_id
                    WHERE bm.branch_id IN (SELECT id FROM branches {where} {limit_clause})
                      AND COALESCE(m.is_notification, 0) = 0""",
                params,
            )
            total_work = cursor.fetchone()[0]

            logger.info("%s: starting, %s branches to embed", _LOG_PREFIX, total_eligible)
            print(
                f"{_PRINT_PREFIX}: starting, {total_eligible} to embed",
                file=sys.stderr,
            )

            def select_batch_cb() -> list[tuple]:
                nonlocal last_selected_id
                # Forward-only: a branch that stays eligible after its attempt
                # would otherwise head every later batch and be inferred again.
                cursor.execute(
                    f"SELECT id FROM branches {where} AND id > ? ORDER BY id LIMIT ?",
                    (*params, last_selected_id, BATCH_SIZE),
                )
                rows = cursor.fetchall()
                # Honor --limit precisely even though batches are BATCH_SIZE-wide.
                if limit is not None:
                    rows = rows[: limit - total_processed]
                if rows:
                    last_selected_id = rows[-1][0]
                return rows

            def is_limit_reached() -> bool:
                # --limit caps work, so a branch that didn't stamp still uses it up.
                return limit_reached(limit, total_processed)

            def on_stuck(_current_ids: list[int]) -> bool:
                # A required callback, but selection only moves forward, so a
                # batch can't repeat.
                logger.error("%s: no progress — same batch re-selected; aborting to avoid infinite loop", _LOG_PREFIX)
                print(
                    f"{_PRINT_PREFIX}: no progress — same batch re-selected, aborting",
                    file=sys.stderr,
                )
                return True

            def process_batch(rows: list[tuple]) -> bool:
                nonlocal total_updated, total_processed, total_inferences, last_progress, work_done
                nonlocal consecutive_lock_timeouts, lock_timeouts, abort_reason
                try:
                    for (branch_id,) in rows:
                        result = _embed_one_branch(conn, branch_id, logger)
                        total_updated += result.updated
                        total_inferences += result.inferences
                        total_processed += 1
                        work_done += result.message_count
                        rate_samples.append((time.monotonic(), work_done))

                        consecutive_lock_timeouts = consecutive_lock_timeouts + 1 if result.lock_timed_out else 0
                        lock_timeouts += result.lock_timed_out
                        if consecutive_lock_timeouts >= _MAX_CONSECUTIVE_LOCK_TIMEOUTS:
                            abort_reason = run_history.REASON_DATABASE_BUSY
                            logger.error(
                                "%s: %s lock timeouts in a row, aborting after %s branches embedded",
                                _LOG_PREFIX,
                                consecutive_lock_timeouts,
                                total_updated,
                            )
                            print(f"{_PRINT_PREFIX}: database stayed locked, aborting", file=sys.stderr)
                            return False

                        if total_processed - last_progress >= progress_every:
                            msg = _format_embed_progress(
                                total_inferences=total_inferences,
                                total_updated=total_updated,
                                total_eligible=total_eligible,
                                elapsed=time.monotonic() - started,
                                rate_samples=rate_samples,
                                total_work=total_work,
                                work_done=work_done,
                            )
                            logger.info("%s: %s", _LOG_PREFIX, msg)
                            print(f"{_PRINT_PREFIX}: {msg}", file=sys.stderr)
                            last_progress = total_processed
                except Exception as exc:
                    abort_reason = (
                        run_history.REASON_MODEL_UNAVAILABLE
                        if isinstance(exc, ModelBrokenError)
                        else run_history.REASON_ERROR
                    )
                    # Batch-level abort: an infra error re-raised by
                    # _embed_one_branch (see its docstring), or any other
                    # unexpected failure from the bookkeeping code above
                    # (progress formatting, logging) — never a per-row content
                    # error, which _embed_one_branch marks and absorbs
                    # internally. Branches finished earlier in the run are
                    # already committed; the failed one is re-selected next run.
                    logger.exception("%s: session failure, aborting", _LOG_PREFIX)
                    conn.rollback()
                    return False
                return True

            def after_batch(_rows: list[tuple]) -> None:
                reclaim_memory(libc)
                time.sleep(BACKFILL_BATCH_DELAY_SECONDS)

            if not run_batch_loop(
                select_batch=select_batch_cb,
                is_limit_reached=is_limit_reached,
                process_batch=process_batch,
                on_stuck=on_stuck,
                after_batch=after_batch,
            ):
                recorder.finish(run_history.OUTCOME_FAILED, abort_reason)
                return EXIT_ABORT
    except (sqlite3.Error, OSError) as exc:
        recorder.finish(run_history.OUTCOME_FAILED, run_history.failure_reason(exc))
        logger.exception("%s: aborted", _LOG_PREFIX)
        print(
            f"{_PRINT_PREFIX}: aborted: {exc}",
            file=sys.stderr,
        )
        return EXIT_ABORT

    elapsed = time.monotonic() - started
    remaining = max(0, total_eligible - total_updated)
    not_updated = total_processed - total_updated
    logger.info(
        "%s complete: %s exchanges across %s branches, %s attempted and not updated, in %s",
        _LOG_PREFIX,
        total_inferences,
        total_updated,
        not_updated,
        format_duration(elapsed),
    )
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "complete",
                    "embedded": total_updated,
                    "not_updated": not_updated,
                    "inferences": total_inferences,
                    "remaining": remaining,
                    "elapsed_seconds": round(elapsed, 1),
                }
            )
        )
    else:
        print(
            f"{_PRINT_PREFIX}: complete — "
            f"{total_inferences} exchanges across {total_updated} branches, "
            f"{not_updated} attempted and not updated, "
            f"in {format_duration(elapsed)}",
            file=sys.stderr,
        )
    with contextlib.suppress(Exception):  # best-effort; sidecar clear must not affect exit behavior
        clear_embedding_failure()
    # A lock timeout leaves its branch for the next run. A content error doesn't
    # count: it is marked and never retried, so it can never clear.
    if lock_timeouts:
        recorder.finish(run_history.OUTCOME_PARTIAL, run_history.REASON_DATABASE_BUSY)
    else:
        recorder.finish(run_history.OUTCOME_OK)
    return EXIT_OK


def clear_false_watermarks(cursor: sqlite3.Cursor, logger: logging.Logger) -> None:
    """Make branches stamped as embedded but missing exchanges eligible again.

    Covers the whole database even under --days or --limit: the cleared watermark
    is accurate whether or not this run gets to the branch, and a later run embeds it.
    """
    missing = find_stamped_branches_missing_chunks(cursor)
    if not missing:
        return
    cursor.executemany("UPDATE branches SET embedding_version = 0 WHERE id = ?", [(bid,) for bid in missing])
    logger.info(
        "%s: cleared watermark on %s branches missing %s exchanges",
        _LOG_PREFIX,
        len(missing),
        sum(missing.values()),
    )


class ModelBrokenError(RuntimeError):
    """The embedding model failed its health probe: the run must abort, not mark the branch."""


class _EmbedResult(NamedTuple):
    """Outcome of embedding one branch. `updated` is 1 when the branch ended up
    stamped, or 0 when it didn't: a content error, a lock timeout, or the branch
    changing or vanishing during inference."""

    message_count: int
    updated: int
    inferences: int
    lock_timed_out: bool = False


def _model_still_healthy(logger: logging.Logger) -> bool:
    """Probe the embedding model with a trivial input to distinguish a
    per-row content error from a broken model/session.

    Called only after `_embed_one_branch` catches an exception that isn't
    sqlite3.Error/OSError. Probes via `embed_batch` — the same function
    `embed_branch_chunks` uses to embed real branches — rather than
    `embed_text`, which bypasses `embed_batch`'s token-counting and explicit
    batch-size machinery and so can't detect a failure specific to that path.
    If the probe still succeeds, the failure was specific to that branch's
    content — safe to mark and continue. If the probe itself fails, the
    model/session is broken and the caller re-raises to abort the run
    instead of marking every subsequent branch as an unrecoverable content
    error.
    """
    try:
        embed_batch(["healthcheck"])
        return True
    except Exception:
        logger.exception("%s: model health probe failed", _LOG_PREFIX)
        return False


def _count_branch_messages(conn: sqlite3.Connection, branch_id: int) -> int:
    """Non-notification messages in a branch, the unit of the run's `total_work`."""
    return conn.execute(
        """
        SELECT COUNT(*) FROM branch_messages bm
        JOIN messages m ON m.id = bm.message_id
        WHERE bm.branch_id = ? AND COALESCE(m.is_notification, 0) = 0
        """,
        (branch_id,),
    ).fetchone()[0]


def _embed_one_branch(
    conn: sqlite3.Connection,
    branch_id: int,
    logger: logging.Logger,
) -> _EmbedResult:
    """Embed one branch, which commits on its own.

    Implements the content-vs-infra split described in this module's docstring:
    a lock timeout costs this branch alone, other infra errors propagate to the
    caller's abort handler, and everything else marks the row's content-error
    sentinel and returns.
    """
    message_count = _count_branch_messages(conn, branch_id)
    not_updated = _EmbedResult(message_count=message_count, updated=0, inferences=0)

    try:
        # max_embeds=None: backfill is off the hot path and fully embeds each
        # branch in one pass.
        outcome = embed_branch_chunks(
            conn,
            branch_id,
            max_embeds=None,
            cap_limit=MODEL_TOKEN_LIMIT,
            replace_stale=True,
        )
    except sqlite3.OperationalError as exc:
        if not is_database_locked(exc):
            raise
        logger.warning("%s: database locked on branch %s, next run retries", _LOG_PREFIX, branch_id)
        return not_updated._replace(lock_timed_out=True)
    except (sqlite3.Error, OSError):
        # Both are Exception subclasses, so this clause exists only to shadow the
        # broader `except Exception` below and keep an infra failure from
        # poisoning the row.
        raise
    except Exception as exc:
        logger.exception("%s: branch %s failed", _LOG_PREFIX, branch_id)
        if not _model_still_healthy(logger):
            # Model/session failure, not a content error: don't poison this
            # row — re-raise so the caller's abort handler stops the run
            # instead of marking every subsequent branch unrecoverable.
            logger.error(
                "%s: model health probe failed after branch %s, aborting without marking",
                _LOG_PREFIX,
                branch_id,
            )
            raise ModelBrokenError(f"embedding model failed its health probe after branch {branch_id}") from exc
        marked = _mark_content_error(conn, branch_id, logger)
        return not_updated._replace(lock_timed_out=not marked)
    return _EmbedResult(message_count=message_count, updated=int(outcome.stamped), inferences=outcome.embedded)


def _mark_content_error(conn: sqlite3.Connection, branch_id: int, logger: logging.Logger) -> bool:
    """Mark the sentinel so this row is skipped next run, returning whether it was marked.

    A lock timeout leaves it unmarked, to be retried.
    """
    try:
        commit_single_write(
            conn,
            "UPDATE branches SET embedding_version = ? WHERE id = ?",
            (CONTENT_ERROR_VERSION, branch_id),
        )
    except sqlite3.OperationalError as exc:
        if not is_database_locked(exc):
            raise
        logger.warning("%s: database locked marking branch %s, next run retries", _LOG_PREFIX, branch_id)
        return False
    return True


def _format_embed_progress(
    *,
    total_inferences: int,
    total_updated: int,
    total_eligible: int,
    elapsed: float,
    rate_samples: deque[tuple[float, int]],
    total_work: int,
    work_done: int,
) -> str:
    """Render one embedding-backfill progress line, including ETA.

    Gates the ETA on branches processed (rate_samples, success or
    content-error), not total_updated (successes only) — a run with many
    early content-errors would otherwise report "warming up" indefinitely
    even though rate_samples has plenty of data to compute a rate from.
    """
    remaining = max(0, total_eligible - total_updated)
    if len(rate_samples) >= _WARMUP_BRANCHES:
        t0, w0 = rate_samples[0]
        t1, w1 = rate_samples[-1]
        dt = t1 - t0
        dw = w1 - w0
        rate = dw / dt if dt > 0 else 0.0
        remaining_work = max(0, total_work - work_done)
        eta = format_duration(remaining_work / rate) if rate > 0 else "?"
    else:
        eta = "warming up"
    return (
        f"{total_inferences} exchanges embedded across "
        f"{total_updated}/{total_eligible} branches, "
        f"{remaining} remaining, "
        f"{format_duration(elapsed)} elapsed, ETA {eta}"
    )
