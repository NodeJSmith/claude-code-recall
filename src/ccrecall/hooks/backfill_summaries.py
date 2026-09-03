"""Backfill context summaries for existing branches.

Runs as a background process spawned by memory-setup.py on SessionStart.
Processes branches in batches via the shared outer batch-loop scaffolding
(`backfill_runner.run_batch_loop`, also used by `backfill_embeddings.py` and
`backfill_tool_content.py`), commits between batches, and marks content
errors with summary_version = -1 (`CONTENT_ERROR_VERSION`) to avoid infinite
retry.

Exception taxonomy is catch-all-then-filter-infra: any exception raised while
summarizing one branch is a content error for that row (mark the sentinel,
continue) unless it's a `sqlite3.Error`/`OSError` — an infra/DB failure,
which propagates to abort the batch without poisoning the row, leaving it
eligible for the next run. A narrow content-error allow-list would let an
unanticipated exception type fall through to the infra path unmarked; since
this backfill respawns on every SessionStart, an unmarked row wedges every
session indefinitely (issue #178).

`backfill_embeddings.py` now shares this same catch-all-then-filter-infra taxonomy
(issue #201) — it's also built to run unattended on a recurring schedule, so the
same wedge risk applied there. `backfill_tool_content.py` is the one exception left
on the narrow content-error allow-list: it's a genuinely one-off manual migration
command with no scheduler-oriented design, so a wedged row there just sits idle
until someone reruns it by hand — keep its taxonomy distinct rather than "fixing"
it to match the other two.
"""

import sqlite3
from pathlib import Path

from ccrecall.config import DEFAULT_DB_PATH, load_settings_for_db, remove_pid_file, setup_logging
from ccrecall.config import PID_KEY_BACKFILL_SUMMARIES as PID_KEY
from ccrecall.db import CONTENT_ERROR_VERSION, get_connection
from ccrecall.hooks.backfill_query import EXIT_ABORT, EXIT_OK
from ccrecall.hooks.backfill_runner import run_batch_loop
from ccrecall.summarizer import SUMMARY_VERSION, compute_context_summary

BATCH_SIZE = 50
_LOG_PREFIX = "Backfill summaries"


def run(*, verbose: bool = False, db: Path = DEFAULT_DB_PATH) -> int:
    """Backfill context summaries for branches that lack a current one.

    Wraps the ``_main()`` work in PID-file cleanup. ``_main()`` is kept separate
    so tests can exercise the backfill logic without the PID-file lifecycle.
    """
    try:
        return _main(verbose=verbose, db=db)
    finally:
        # Delete PID file so _spawn_background can spawn again next session
        remove_pid_file(PID_KEY)


def _main(*, verbose: bool = False, db: Path = DEFAULT_DB_PATH) -> int:
    settings = load_settings_for_db(db)
    logger = setup_logging(settings, process_name="backfill-summary", verbose=verbose)

    total_updated = 0

    try:
        with get_connection(settings) as conn:
            cursor = conn.cursor()

            def select_batch() -> list[tuple]:
                cursor.execute(
                    """
                    SELECT id FROM branches
                    WHERE summary_version IS NULL
                       OR (summary_version < ? AND summary_version != ?)
                    ORDER BY id
                    LIMIT ?
                    """,
                    (SUMMARY_VERSION, CONTENT_ERROR_VERSION, BATCH_SIZE),
                )
                return cursor.fetchall()

            def on_stuck(current_ids: list[int]) -> bool:
                # Every row processed above either updates summary_version or marks
                # CONTENT_ERROR_VERSION, so a genuinely re-selected batch means no
                # per-row progress is possible — abort outright rather than
                # excluding-and-continuing (unlike backfill_tool_content.py, whose
                # eligibility predicate can't see every skip reason).
                logger.error(
                    "%s: no progress — same batch re-selected (branch ids: %s); aborting to avoid infinite loop",
                    _LOG_PREFIX,
                    current_ids,
                )
                return True

            def process_batch(rows: list[tuple]) -> bool:
                nonlocal total_updated
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
                        except (sqlite3.Error, OSError):  # noqa: PERF203 — per-row error isolation; one malformed branch must not abort the batch
                            # Shadows the broader `except Exception` below — see
                            # this module's docstring for why the split exists.
                            raise
                        except Exception:
                            # Anything else is a per-row content error (malformed
                            # summary data): mark the sentinel so it isn't retried
                            # forever.
                            cursor.execute(
                                "UPDATE branches SET summary_version = ? WHERE id = ?",
                                (CONTENT_ERROR_VERSION, branch_id),
                            )
                            logger.exception("%s: branch %s content error", _LOG_PREFIX, branch_id)
                except (sqlite3.Error, OSError):
                    # Infra/session failure (locked DB, I/O): abort without marking
                    # further rows — they stay eligible next run. Commit prior batches.
                    logger.exception("%s: session failure, aborting", _LOG_PREFIX)
                    conn.commit()
                    return False
                return True

            def after_batch(_rows: list[tuple]) -> None:
                conn.commit()

            if not run_batch_loop(
                select_batch=select_batch,
                is_limit_reached=lambda: False,  # no --limit flag on this auto-spawned backfill
                process_batch=process_batch,
                on_stuck=on_stuck,
                after_batch=after_batch,
            ):
                return EXIT_ABORT
    except (sqlite3.Error, OSError):
        logger.exception("%s: failed to connect to DB", _LOG_PREFIX)
        return EXIT_ABORT

    logger.info("%s complete: %s branches summarized", _LOG_PREFIX, total_updated)
    return EXIT_OK
