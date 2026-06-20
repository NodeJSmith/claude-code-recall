#!/usr/bin/env python3
"""
Backfill context summaries for existing branches.

Runs as a background process spawned by memory-setup.py on SessionStart.
Processes branches in batches, commits between batches, and marks errors
with summary_version = -1 to avoid infinite retry.
"""

from ccrecall.db import (
    CONTENT_ERROR_VERSION,
    DEFAULT_DB_PATH,
    get_db_connection,
    load_settings,
    setup_logging,
)
from ccrecall.summarizer import SUMMARY_VERSION, compute_context_summary

BATCH_SIZE = 50

_PID_FILE = DEFAULT_DB_PATH.parent / ".pid-cm-backfill-summaries"


def main():
    try:
        _main()
    finally:
        # Delete PID file so _spawn_background can spawn again next session
        try:
            _PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass


def _main():
    settings = load_settings()
    logger = setup_logging(settings)

    try:
        conn = get_db_connection(settings)
    except Exception as e:
        logger.error(f"Backfill: failed to connect to DB: {e}")
        return

    cursor = conn.cursor()
    total_updated = 0

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
                except (ValueError, TypeError, KeyError) as e:
                    # Per-row content error (malformed summary data): mark the
                    # sentinel so it isn't retried forever. Infra errors fall
                    # through to the outer handler instead of poisoning the row.
                    cursor.execute(
                        "UPDATE branches SET summary_version = ? WHERE id = ?",
                        (CONTENT_ERROR_VERSION, branch_id),
                    )
                    logger.error(f"Backfill: branch {branch_id} content error: {e}")
        except Exception as e:
            # Infra/session failure (locked DB, I/O): abort without marking
            # further rows — they stay eligible next run. Commit prior batches.
            logger.error(f"Backfill: session failure, aborting: {e}")
            conn.commit()
            conn.close()
            return

        conn.commit()

    conn.close()
    logger.info(f"Backfill complete: {total_updated} branches summarized")


if __name__ == "__main__":
    main()
