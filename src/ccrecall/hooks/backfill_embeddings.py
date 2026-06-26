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
"""

import contextlib
import json
import logging
import os
import sqlite3
import sys
import time

from ccrecall.db import (
    CHUNK_EMBEDDABLE_BRANCH_FILTER,
    CONTENT_ERROR_VERSION,
    chunk_vec_queryable,
    fetch_branch_messages,
    get_db_connection,
    load_settings,
    remove_pid_file,
    setup_logging,
)
from ccrecall.embeddings import (
    DEFAULT_EMBED_THREADS,
    EMBEDDING_MODEL,
    EMBEDDING_VERSION,
    model_available,
)
from ccrecall.session_ops import embed_branch_chunks

BATCH_SIZE = 20
BACKFILL_BATCH_DELAY_SECONDS = 0.05
DEFAULT_PROGRESS_EVERY = BATCH_SIZE

# Lower scheduling priority for this background CPU job so interactive work wins.
BACKFILL_NICE_LEVEL = 10

EXIT_OK = 0
EXIT_ABORT = 1

# PID key for the self-concurrency guard (this command is manual-only, never
# auto-spawned; the marker is cleaned up by the CLI command on exit).
PID_KEY = "ccrecall-backfill-embeddings"


def cleanup_pid() -> None:
    """Remove the self-concurrency PID marker (no-op if absent)."""
    remove_pid_file(PID_KEY)


def days_modifier(days: int) -> str:
    """SQLite datetime() modifier for an N-day lookback (days=7 -> '-7 days').

    Single source of truth for the --days recency bound so build_selection()
    (eligibility) and count_status() (progress) can't construct it differently.
    """
    return f"-{days} days"


def build_selection(days: int | None) -> tuple[str, list]:
    """Return the shared WHERE clause + params for eligible-branch selection (chunk path).

    Eligible = CHUNK_EMBEDDABLE branch (active leaf with at least one message),
    not the content-error sentinel (-1), and needing a chunk embed:
    - watermark-stale: embedding_version IS NULL or below EMBEDDING_VERSION or
      wrong model — this includes version-stale chunks the write path skips;
      the backfill owns their re-embed.
    - heal clause: EXISTS a chunks row without a chunk_vec — catches crash
      victims and post-drop orphans the watermark can't see (design C1).

    summary_version_at_embed is dropped from the predicate: chunk staleness is
    driven by content_hash + EMBEDDING_VERSION, not the summary version.

    Optional recency bound on ended_at via --days; NULL ended_at is excluded
    (SQLite NULL > x is false). Returns a SQL fragment for
    f"... FROM branches {where}".
    """
    where = f"""
        WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}
          AND embedding_version IS NOT {CONTENT_ERROR_VERSION}
          AND (
            embedding_version IS NULL
            OR embedding_version < ?
            OR embedding_model IS NOT ?
            OR EXISTS (
              SELECT 1 FROM chunks c
              WHERE c.branch_id = branches.id
                AND NOT EXISTS (SELECT 1 FROM chunk_vec WHERE chunk_id = c.id)
            )
          )
    """
    params: list = [EMBEDDING_VERSION, EMBEDDING_MODEL]
    if days is not None:
        where += "          AND ended_at > datetime('now', ?)\n"
        params.append(days_modifier(days))
    return where, params


def count_status(cursor: sqlite3.Cursor, days: int | None) -> dict[str, int]:
    """Count chunk-coverage backfill progress without doing any work.

    universe = chunks belonging to CHUNK_EMBEDDABLE branches.
    done     = chunks with a current-version chunk_vec row.
    eligible = branches still needing work (build_selection predicate; branch count).
    errored  = branches marked with the content-error sentinel (branch count).

    universe/done are chunk-grain; eligible/errored are branch-grain since the
    iteration unit and the sentinel both live on branches.

    The optional --days recency bound is applied consistently to all queries.
    """
    recency_joined = ""  # for chunk JOIN branches queries
    recency_branch = ""  # for branches-only queries
    recency_params: list = []
    if days is not None:
        recency_joined = " AND branches.ended_at > datetime('now', ?)"
        recency_branch = " AND ended_at > datetime('now', ?)"
        recency_params = [days_modifier(days)]

    # universe: total chunks belonging to CHUNK_EMBEDDABLE branches
    cursor.execute(
        f"""
        SELECT COUNT(*) FROM chunks
        JOIN branches ON chunks.branch_id = branches.id
        WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}{recency_joined}
        """,
        recency_params,
    )
    universe = cursor.fetchone()[0]

    # done: chunks with a current-version embedding AND an existing chunk_vec row
    cursor.execute(
        f"""
        SELECT COUNT(*) FROM chunks
        JOIN branches ON chunks.branch_id = branches.id
        WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}
          AND chunks.embedding_version = ?
          AND chunks.embedding_model = ?
          AND EXISTS (SELECT 1 FROM chunk_vec WHERE chunk_id = chunks.id){recency_joined}
        """,
        [EMBEDDING_VERSION, EMBEDDING_MODEL, *recency_params],
    )
    done = cursor.fetchone()[0]

    # errored: branches at the content-error sentinel
    cursor.execute(
        f"""
        SELECT COUNT(*) FROM branches
        WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}
          AND embedding_version IS {CONTENT_ERROR_VERSION}{recency_branch}
        """,
        recency_params,
    )
    errored = cursor.fetchone()[0]

    # eligible: branches that build_selection would include (branch count)
    where, params = build_selection(days)
    cursor.execute(f"SELECT COUNT(*) FROM branches {where}", params)
    eligible = cursor.fetchone()[0]

    return {
        "universe": universe,
        "done": done,
        "eligible": eligible,
        "errored": errored,
    }


def format_duration(seconds: float) -> str:
    """Compact human duration: '45s', '12m03s', '1h07m'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def run_status(
    *,
    days: int | None,
    json_mode: bool,
    settings: dict | None,
    logger: logging.Logger,
) -> int:
    """Report chunk coverage (done/universe) and branch eligible/errored counts (read-only)."""
    try:
        conn = get_db_connection(settings, load_vec=True)
    except (sqlite3.Error, OSError) as e:
        logger.error("Backfill status: failed to connect to DB: %s", e)
        print(f"ccrecall backfill embeddings: failed to connect to DB: {e}", file=sys.stderr)
        return EXIT_ABORT

    if not chunk_vec_queryable(conn):
        logger.error("Backfill status: sqlite-vec unavailable")
        print("ccrecall backfill embeddings: sqlite-vec unavailable", file=sys.stderr)
        conn.close()
        return EXIT_ABORT

    try:
        counts = count_status(conn.cursor(), days)
    finally:
        conn.close()

    if json_mode:
        print(json.dumps({**counts, "days": days}))
        return EXIT_OK

    universe = counts["universe"]
    done = counts["done"]
    pct = (done / universe * 100) if universe else 0.0
    scope = f" (last {days}d)" if days is not None else ""
    print(f"ccrecall backfill embeddings status{scope}:")
    print(f"  embedded:  {done} / {universe} chunks  ({pct:.0f}%)")
    print(f"  remaining: {counts['eligible']} branches")
    if counts["errored"]:
        print(f"  errored:   {counts['errored']} branches  (content errors, won't retry)")
    return EXIT_OK


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
    logger = setup_logging(settings)

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
        logger.error("Backfill embeddings: model not available, aborting (no rows marked)")
        print(
            "ccrecall backfill embeddings: model not available, aborting (no rows marked)",
            file=sys.stderr,
        )
        return EXIT_ABORT

    try:
        conn = get_db_connection(settings, load_vec=True)
    except (sqlite3.Error, OSError) as e:
        logger.error("Backfill embeddings: failed to connect to DB: %s", e)
        print(
            f"ccrecall backfill embeddings: failed to connect to DB: {e}",
            file=sys.stderr,
        )
        return EXIT_ABORT

    cursor = conn.cursor()

    # ABORT level: chunk_vec must be queryable before any selection runs — the
    # eligibility WHERE references chunks/chunk_vec, which get_db_connection
    # creates only when sqlite-vec loaded. Without this guard the queries below
    # crash with "no such table: chunk_vec" instead of exiting cleanly.
    if not chunk_vec_queryable(conn):
        logger.error("Backfill embeddings: sqlite-vec unavailable, aborting (no rows marked)")
        print(
            "ccrecall backfill embeddings: sqlite-vec unavailable, aborting (no rows marked)",
            file=sys.stderr,
        )
        conn.close()
        return EXIT_ABORT

    total_updated = 0
    total_inferences = 0
    last_progress = 0
    last_batch_ids: list[int] | None = None
    started = time.monotonic()

    where, params = build_selection(days)

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
        # below compares current_ids to last_batch_ids, which is only meaningful
        # if re-selection returns rows in a stable order.
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
            logger.error("Backfill embeddings: no progress — same batch re-selected; aborting to avoid infinite loop")
            print(
                "ccrecall backfill embeddings: no progress — same batch re-selected, aborting",
                file=sys.stderr,
            )
            conn.close()
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
            conn.close()
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

    conn.close()
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
    return EXIT_OK
