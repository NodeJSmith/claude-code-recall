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


def run(
    *,
    status: bool = False,
    json_mode: bool = False,
    days: int | None = None,
    limit: int | None = None,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
    threads: int = DEFAULT_EMBED_THREADS,
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
    logger = setup_logging(settings, process_name="backfill-embed")

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
        logger.error("Backfill embeddings: model not available, aborting (no rows marked)")
        print(
            "ccrecall backfill embeddings: model not available, aborting (no rows marked)",
            file=sys.stderr,
        )
        return EXIT_ABORT

    total_updated = 0
    total_inferences = 0
    last_progress = 0
    last_batch_ids: list[int] | None = None
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
                logger.error("Backfill embeddings: sqlite-vec unavailable, aborting (no rows marked)")
                print(
                    "ccrecall backfill embeddings: sqlite-vec unavailable, aborting (no rows marked)",
                    file=sys.stderr,
                )
                return EXIT_ABORT

            # Compute total-eligible count once (avoid per-batch full COUNT).
            cursor.execute(f"SELECT COUNT(*) FROM branches {where}", params)
            total_eligible = cursor.fetchone()[0]
            if limit is not None:
                total_eligible = min(total_eligible, limit)

            logger.info("Backfill embeddings: starting, %s branches to embed", total_eligible)
            print(
                f"ccrecall backfill embeddings: starting, {total_eligible} to embed",
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
                        "Backfill embeddings: no progress — same batch re-selected; aborting to avoid infinite loop"
                    )
                    print(
                        "ccrecall backfill embeddings: no progress — same batch re-selected, aborting",
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

                        cursor.execute("SAVEPOINT row")
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
                            cursor.execute("RELEASE SAVEPOINT row")
                            total_updated += 1
                            total_inferences += embedded
                        except (ValueError, OverflowError, UnicodeError) as e:
                            cursor.execute("ROLLBACK TO SAVEPOINT row")
                            cursor.execute("RELEASE SAVEPOINT row")
                            # Per-row content error: mark sentinel so this row is skipped next run.
                            cursor.execute(
                                "UPDATE branches SET embedding_version = ? WHERE id = ?",
                                (CONTENT_ERROR_VERSION, branch_id),
                            )
                            logger.error("Backfill embeddings: branch %s failed: %s", branch_id, e)
                except Exception as e:
                    # Infra/session failure (e.g. ONNX session crash, sqlite3.Error on
                    # fetch_branch_messages, OOM): abort without marking the content-error
                    # sentinel. Any committed partial state (a pre-delete + cleared watermark
                    # from an interrupted row) is recovered by the heal clause / watermark-
                    # stale predicate on the next run; affected rows stay eligible.
                    logger.error("Backfill embeddings: session failure, aborting: %s", e)
                    conn.commit()
                    return EXIT_ABORT

                conn.commit()

                # Progress: cadence-gated, with elapsed + ETA for unattended runs.
                if total_updated - last_progress >= progress_every:
                    elapsed = time.monotonic() - started
                    remaining = max(0, total_eligible - total_updated)
                    rate = total_updated / elapsed if elapsed > 0 else 0.0
                    eta = format_duration(remaining / rate) if rate > 0 else "?"
                    msg = (
                        f"{total_inferences} exchanges embedded across "
                        f"{total_updated}/{total_eligible} branches, "
                        f"{remaining} remaining, "
                        f"{format_duration(elapsed)} elapsed, ETA {eta}"
                    )
                    logger.info("Backfill embeddings: %s", msg)
                    print(f"ccrecall backfill embeddings: {msg}", file=sys.stderr)
                    last_progress = total_updated

                time.sleep(BACKFILL_BATCH_DELAY_SECONDS)
    except (sqlite3.Error, OSError) as e:
        logger.error("Backfill embeddings: aborted: %s", e)
        print(
            f"ccrecall backfill embeddings: aborted: {e}",
            file=sys.stderr,
        )
        return EXIT_ABORT

    elapsed = time.monotonic() - started
    remaining = max(0, total_eligible - total_updated)
    logger.info(
        "Backfill embeddings complete: %s exchanges across %s branches in %s",
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
            f"ccrecall backfill embeddings: complete — "
            f"{total_inferences} exchanges across {total_updated} branches "
            f"in {format_duration(elapsed)}",
            file=sys.stderr,
        )
    with contextlib.suppress(Exception):  # best-effort; sidecar clear must not affect exit behavior
        clear_embedding_failure()
    return EXIT_OK
