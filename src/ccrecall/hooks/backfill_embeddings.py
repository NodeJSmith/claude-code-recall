"""Backfill chunk-grain embeddings for existing active-leaf branches.

Opt-in: invoke manually via `ccrecall backfill embeddings` to seed historical
embeddings. NOT auto-spawned on SessionStart (embedding inference is CPU-bound);
forward coverage comes from embed-on-write instead.
Processes branches in batches, commits between batches, and marks per-row
content errors with embedding_version = -1 to avoid infinite retry.

Two-level failure distinction:
  - Model/session failure: abort the whole run, mark NOTHING.
  - Per-row content error (tokenizer overflow, malformed content): mark that
    row embedding_version = -1 and continue.

Built to run unattended (systemd timer): `--status [--json]` reports progress
without embedding, progress lines carry elapsed/ETA, and abort paths exit
non-zero so the scheduler sees the failure.

Query construction/constants live in `backfill_query.py`; status reporting
lives in `backfill_status.py`. This module keeps only the `run()` orchestrator.
"""

import contextlib
import json
import os
import sqlite3
import sys
import time
from collections import deque

from ccrecall.config import load_settings, setup_logging
from ccrecall.db import CONTENT_ERROR_VERSION, chunk_vec_queryable, fetch_branch_messages, get_connection
from ccrecall.embed_ops import embed_branch_chunks
from ccrecall.embeddings import DEFAULT_EMBED_THREADS, EMBEDDING_MODEL, EMBEDDING_VERSION, model_available
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
from ccrecall.hooks.backfill_status import format_duration, run_status

_PRINT_PREFIX = "ccrecall backfill embeddings"
_LOG_PREFIX = "Backfill embeddings"
_SAVEPOINT_NAME = "row"
_WARMUP_BRANCHES = 5
_RATE_WINDOW = 30


def run(
    *,
    status: bool = False,
    json_mode: bool = False,
    days: int | None = None,
    limit: int | None = None,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
    threads: int = DEFAULT_EMBED_THREADS,
    verbose: bool = False,
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

    settings = load_settings()
    logger = setup_logging(settings, process_name="backfill-embed", verbose=verbose)

    if status:
        return run_status(days=days, json_mode=json_mode, settings=settings, logger=logger)

    # Background CPU job: lower scheduling priority so the bounded inference
    # threads yield to interactive work (machines.md thrash risk). Best-effort —
    # os.nice is POSIX-only and may be denied; either way the run proceeds.
    with contextlib.suppress(AttributeError, OSError):
        os.nice(BACKFILL_NICE_LEVEL)

    # ABORT level: check model availability before touching any rows.
    # model_available() warms the singleton session on success — no extra cost.
    # Pass --threads here since this is the call that constructs the session.
    if not model_available(threads=threads):
        with contextlib.suppress(Exception):  # best-effort; sidecar write must not affect exit behavior
            record_embedding_failure(reason=REASON_MODEL_UNAVAILABLE)
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
    last_batch_ids: list[int] | None = None
    work_done = 0
    rate_samples: deque[tuple[float, int]] = deque(maxlen=_RATE_WINDOW)
    started = time.monotonic()

    where, params = build_selection(days)

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
                logger.error("%s: sqlite-vec unavailable, aborting (no rows marked)", _LOG_PREFIX)
                print(
                    f"{_PRINT_PREFIX}: sqlite-vec unavailable, aborting (no rows marked)",
                    file=sys.stderr,
                )
                return EXIT_ABORT

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

            while True:
                if limit is not None and total_updated >= limit:
                    break

                # ORDER BY id keeps batch order deterministic: the no-progress guard
                # below compares current_ids to last_batch_ids, which is only
                # meaningful if re-selection returns rows in a stable order.
                cursor.execute(
                    f"SELECT id FROM branches {where} ORDER BY id LIMIT ?",
                    (*params, BATCH_SIZE),
                )
                rows = cursor.fetchall()

                if not rows:
                    break

                # Honor --limit precisely even though batches are BATCH_SIZE-wide.
                if limit is not None:
                    rows = rows[: limit - total_updated]

                current_ids = [r[0] for r in rows]
                if current_ids == last_batch_ids:
                    logger.error(
                        "%s: no progress — same batch re-selected; aborting to avoid infinite loop", _LOG_PREFIX
                    )
                    print(
                        f"{_PRINT_PREFIX}: no progress — same batch re-selected, aborting",
                        file=sys.stderr,
                    )
                    return EXIT_ABORT
                last_batch_ids = current_ids

                try:
                    for (branch_id,) in rows:
                        # Fetch messages BEFORE the SAVEPOINT: a sqlite3.Error here is
                        # a batch-abort (infra failure), NOT a per-row content error.
                        # It propagates through the inner except to the outer handler.
                        branch_msgs = fetch_branch_messages(cursor, branch_id, include_notifications=False)

                        cursor.execute(f"SAVEPOINT {_SAVEPOINT_NAME}")
                        try:
                            # Pre-delete stale or missing-vector chunk rows so
                            # embed_branch_chunks sees them as new and re-embeds them.
                            # The cascade trigger removes their chunk_vec rows too.
                            # Chunks that are already current (correct version+model AND
                            # have a chunk_vec) are preserved — not re-embedded needlessly.
                            cursor.execute(
                                """
                                DELETE FROM chunks
                                WHERE branch_id = ?
                                  AND (
                                    embedding_version IS NULL
                                    OR embedding_version != ?
                                    OR embedding_model IS NOT ?
                                    OR NOT EXISTS (
                                      SELECT 1 FROM chunk_vec WHERE chunk_id = chunks.id
                                    )
                                  )
                                """,
                                (branch_id, EMBEDDING_VERSION, EMBEDDING_MODEL),
                            )
                            # embed_branch_chunks RAISES on failure — content errors
                            # (ValueError/OverflowError/UnicodeError) are caught below and
                            # marked once; infra errors propagate to the outer except.
                            # max_embeds=None: backfill is off the hot path and must fully
                            # embed each branch in one pass. With the write-path cap, a
                            # branch longer than the cap would stay eligible and trip the
                            # no-progress guard on re-selection. The return value is the
                            # actual inference count (exchanges embedded), so total_inferences
                            # excludes already-current chunks the pre-delete preserved.
                            embedded = embed_branch_chunks(
                                cursor, branch_id, branch_msgs, is_active=True, vec_writable=True, max_embeds=None
                            )
                            cursor.execute(f"RELEASE SAVEPOINT {_SAVEPOINT_NAME}")
                            total_updated += 1
                            total_inferences += embedded
                        except (ValueError, OverflowError, UnicodeError):
                            cursor.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT_NAME}")
                            cursor.execute(f"RELEASE SAVEPOINT {_SAVEPOINT_NAME}")
                            # Per-row content error: mark sentinel so this row is skipped next run.
                            cursor.execute(
                                "UPDATE branches SET embedding_version = ? WHERE id = ?",
                                (CONTENT_ERROR_VERSION, branch_id),
                            )
                            logger.exception("%s: branch %s failed", _LOG_PREFIX, branch_id)

                        total_processed += 1
                        work_done += len(branch_msgs)
                        rate_samples.append((time.monotonic(), work_done))

                        if total_processed - last_progress >= progress_every:
                            elapsed = time.monotonic() - started
                            remaining = max(0, total_eligible - total_updated)
                            # Gate on branches processed (success or content-error),
                            # not total_updated (successes only) — a run with many
                            # early content-errors would otherwise report "warming
                            # up" indefinitely even though rate_samples has plenty
                            # of data to compute a rate from.
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
                            msg = (
                                f"{total_inferences} exchanges embedded across "
                                f"{total_updated}/{total_eligible} branches, "
                                f"{remaining} remaining, "
                                f"{format_duration(elapsed)} elapsed, ETA {eta}"
                            )
                            logger.info("%s: %s", _LOG_PREFIX, msg)
                            print(f"{_PRINT_PREFIX}: {msg}", file=sys.stderr)
                            last_progress = total_processed
                except Exception:
                    # Infra/session failure (e.g. ONNX session crash, sqlite3.Error on
                    # fetch_branch_messages, OOM): abort without marking the content-error
                    # sentinel. Any committed partial state (a pre-delete + cleared watermark
                    # from an interrupted row) is recovered by the heal clause / watermark-
                    # stale predicate on the next run; affected rows stay eligible.
                    logger.exception("%s: session failure, aborting", _LOG_PREFIX)
                    conn.commit()
                    return EXIT_ABORT

                conn.commit()

                time.sleep(BACKFILL_BATCH_DELAY_SECONDS)
    except (sqlite3.Error, OSError) as e:
        logger.exception("%s: aborted", _LOG_PREFIX)
        print(
            f"{_PRINT_PREFIX}: aborted: {e}",
            file=sys.stderr,
        )
        return EXIT_ABORT

    elapsed = time.monotonic() - started
    remaining = max(0, total_eligible - total_updated)
    logger.info(
        "%s complete: %s exchanges across %s branches in %s",
        _LOG_PREFIX,
        total_inferences,
        total_updated,
        format_duration(elapsed),
    )
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "complete",
                    "embedded": total_updated,
                    "inferences": total_inferences,
                    "remaining": remaining,
                    "elapsed_seconds": round(elapsed, 1),
                }
            )
        )
    else:
        print(
            f"{_PRINT_PREFIX}: complete — "
            f"{total_inferences} exchanges across {total_updated} branches "
            f"in {format_duration(elapsed)}",
            file=sys.stderr,
        )
    with contextlib.suppress(Exception):  # best-effort; sidecar clear must not affect exit behavior
        clear_embedding_failure()
    return EXIT_OK
