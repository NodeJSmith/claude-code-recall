"""SessionStart hook - setup memory directory and trigger initial import."""

import contextlib
import json
import logging
import sqlite3
import tempfile
import time
from pathlib import Path

from ccrecall import job_lock
from ccrecall.config import (
    DEFAULT_DB_PATH,
    JOB_KEY_BACKFILL_SUMMARIES,
    JOB_KEY_IMPORT,
    JOB_KEY_WARM_MODEL,
    SYNC_TEMP_PREFIX,
    ensure_parent_dir,
    get_db_path,
    load_settings,
    log_hook_exception,
    setup_logging,
)
from ccrecall.db import CONTENT_ERROR_VERSION, get_connection
from ccrecall.hooks.subprocess_utils import detached_popen_kwargs
from ccrecall.models import LOGGER_NAME
from ccrecall.summarizer import SUMMARY_VERSION

# Stale sync temp files older than this (seconds) are reaped on SessionStart.
STALE_TEMP_FILE_MAX_AGE_SECONDS = 3600


def _spawn_background(argv: list[str], job_key: str, logger: logging.Logger, scope: dict | None = None) -> None:
    """Spawn an installed entry point as a detached background job, unless that job is already running.

    ``job_key`` names the job's lock (see job_lock.py) and must match the key the
    spawned command acquires, so the child adopts the lock this spawn hands it.
    ``scope`` is what the spawn record says the run covers, until the child
    rewrites it.
    """
    if not job_lock.spawn(argv, job_key, scope, **detached_popen_kwargs()):
        logger.debug("background job already running; not spawning", extra={"job_key": job_key})


def _needs_reimport(settings: dict | None, logger: logging.Logger) -> bool:
    """Check if any import_log entries have NULL file_hash.

    NULL file_hash is written by the normal sync path when file_hash is unavailable.
    """
    try:
        with get_connection(settings) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM import_log WHERE file_hash IS NULL")
            count = cursor.fetchone()[0]
        return count > 0
    except (sqlite3.Error, OSError):
        logger.exception("DB check failed, assuming no reimport needed")
        return False


def _needs_backfill(settings: dict | None, logger: logging.Logger) -> bool:
    """Check if any branches need summary backfill. Returns False on any error."""
    try:
        with get_connection(settings) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(branches)")
            cols = {row[1] for row in cursor.fetchall()}
            if "summary_version" not in cols:
                return False
            cursor.execute(
                "SELECT COUNT(*) FROM branches WHERE summary_version IS NULL"
                " OR (summary_version < ? AND summary_version != ?)",
                (SUMMARY_VERSION, CONTENT_ERROR_VERSION),
            )
            count = cursor.fetchone()[0]
        return count > 0
    except (sqlite3.Error, OSError):
        logger.exception("DB check failed, assuming no backfill needed")
        return False


def _reap_stale_temp_files() -> None:
    """Delete ccrecall-sync-*.json temp files older than 1 hour.

    These are left behind when `ccrecall sync-current` crashes or is killed
    before it can clean up its own input file.
    """
    tmp_dir = Path(tempfile.gettempdir())
    cutoff = time.time() - STALE_TEMP_FILE_MAX_AGE_SECONDS
    for path in tmp_dir.glob(f"{SYNC_TEMP_PREFIX}*.json"):
        with contextlib.suppress(OSError):
            if path.stat().st_mtime < cutoff:
                path.unlink()


def main():
    settings = None
    try:
        settings = load_settings()
        logger = setup_logging(settings, process_name="setup")
    except Exception:
        # Logging setup is best-effort: a bad log path (unwritable dir, a file
        # where a directory is expected) must not block the real setup work
        # below. Fall back to the bare named logger — with no handler attached
        # it can't raise on .warning()/.exception() calls (stdlib routes
        # WARNING+ through logging.lastResort to stderr instead). `settings`
        # stays whatever load_settings() last returned (None if that itself
        # failed) — the callers below already treat None as "use defaults".
        log_hook_exception("setup")
        logger = logging.getLogger(LOGGER_NAME)

    try:
        ensure_parent_dir(DEFAULT_DB_PATH)

        # Clean up stale temp files from crashed/killed sync processes
        _reap_stale_temp_files()

        db_absent = not DEFAULT_DB_PATH.exists()

        if db_absent or _needs_reimport(settings, logger):
            _spawn_background(
                ["ccrecall", "import", "--repair-gaps"],
                JOB_KEY_IMPORT,
                logger,
                job_lock.import_scope(repair_gaps=True, db=get_db_path(settings)),
            )

        if _needs_backfill(settings, logger):
            _spawn_background(["ccrecall", "backfill", "summaries"], JOB_KEY_BACKFILL_SUMMARIES, logger)

        # Note: embedding backfill is NOT auto-spawned. Embeddings are filled
        # forward by embed-on-write (active leaves only); historical seeding is
        # opt-in via `ccrecall backfill embeddings [--days N] [--limit N]` so
        # embedding the full history never fires unbidden (machines.md thrash risk).

        # Pre-warm the fastembed model cache so sync-current's first embed never
        # triggers an invisible ~120 MB download. Lock-guarded via _spawn_background:
        # at most one concurrent warm runs at a time.
        # Runs on every SessionStart; fast no-op after the first download since the
        # model is already cached on disk.
        _spawn_background(["ccrecall-warm-model"], JOB_KEY_WARM_MODEL, logger)
    except Exception:
        # Top-level hook guard: must never crash the session start. Log
        # best-effort (no-op unless logging_enabled) so the failure isn't silent.
        log_hook_exception("setup")

    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
