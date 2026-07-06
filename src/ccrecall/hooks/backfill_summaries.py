"""Backfill context summaries for existing branches.

Runs as a background process spawned by memory-setup.py on SessionStart.
Processes branches in batches, commits between batches, and marks errors
with summary_version = -1 to avoid infinite retry.
"""

import sqlite3

from ccrecall.config import load_settings, remove_pid_file, setup_logging
from ccrecall.db import CONTENT_ERROR_VERSION, get_connection
from ccrecall.summarizer import SUMMARY_VERSION, compute_context_summary

BATCH_SIZE = 50

# PID key — must stay in sync with the spawn in memory_setup
# (`ccrecall backfill summaries`).
PID_KEY = "ccrecall-backfill-summaries"


def run():
    """Backfill context summaries for branches that lack a current one.

    Wraps the ``_main()`` work in PID-file cleanup. ``_main()`` is kept separate
    so tests can exercise the backfill logic without the PID-file lifecycle.
    """
    try:
        _main()
    finally:
        # Delete PID file so _spawn_background can spawn again next session
        remove_pid_file(PID_KEY)


def _main():
    settings = load_settings()
    logger = setup_logging(settings)

    total_updated = 0

    try:
        with get_connection(settings) as conn:
            cursor = conn.cursor()

            while True:
                cursor.execute(
                    """
                    SELECT id FROM branches
                    WHERE summary_version IS NULL
                       OR (summary_version < ? AND summary_version != ?)
                    LIMIT ?
                """,
                    (SUMMARY_VERSION, CONTENT_ERROR_VERSION, BATCH_SIZE),
                )
                rows = cursor.fetchall()

                if not rows:
                    break

                try:
                    for (branch_id,) in rows:
                        try:
                            summary_md, summary_json = compute_context_summary(cursor, branch_id)
                            cursor.execute(
                                """
                                UPDATE branches SET context_summary = ?, context_summary_json = ?, summary_version = ?
                                WHERE id = ?
                            """,
                                (summary_md, summary_json, SUMMARY_VERSION, branch_id),
                            )
                            total_updated += 1
                        except (ValueError, TypeError, KeyError) as e:  # noqa: PERF203 — per-row error isolation
                            # Per-row content error (malformed summary data): mark the
                            # sentinel so it isn't retried forever. Infra errors fall
                            # through to the outer handler instead of poisoning the row.
                            cursor.execute(
                                "UPDATE branches SET summary_version = ? WHERE id = ?",
                                (CONTENT_ERROR_VERSION, branch_id),
                            )
                            logger.error("Backfill: branch %s content error: %s", branch_id, e)
                except Exception as e:
                    # Infra/session failure (locked DB, I/O): abort without marking
                    # further rows — they stay eligible next run. Commit prior batches.
                    logger.error("Backfill: session failure, aborting: %s", e)
                    conn.commit()
                    return

                conn.commit()
    except (sqlite3.Error, OSError) as e:
        logger.error("Backfill: failed to connect to DB: %s", e)
        return

    logger.info("Backfill complete: %s branches summarized", total_updated)
