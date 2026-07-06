"""SessionStart hook - setup memory directory and trigger initial import."""

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ccrecall.config import (
    DEFAULT_DB_PATH,
    PID_FILE_MODE,
    SYNC_TEMP_PREFIX,
    ensure_parent_dir,
    load_settings,
    log_hook_exception,
    pid_file_path,
)
from ccrecall.db import CONTENT_ERROR_VERSION, get_connection
from ccrecall.hooks import backfill_summaries, import_conversations
from ccrecall.hooks.warm_model import PID_KEY as WARM_MODEL_PID_KEY
from ccrecall.summarizer import SUMMARY_VERSION

# Stale sync temp files older than this (seconds) are reaped on SessionStart.
STALE_TEMP_FILE_MAX_AGE_SECONDS = 3600


def _spawn_background(argv: list[str], pid_key: str) -> None:
    """Spawn an installed entry point as a detached background process.

    ``argv`` is the command to run (e.g. ``["ccrecall", "import"]``); ``pid_key``
    names the PID file (``.pid-<pid_key>``) and must match the constant the
    spawned command unlinks on exit.

    Uses an atomic PID-file guard (O_CREAT | O_EXCL) to ensure at most one
    concurrent instance of the given command is running.  If a stale PID file
    exists (dead process), it is reaped and the new process is spawned.
    """
    pid_path = pid_file_path(pid_key)

    while True:
        try:
            # Atomic create — fails with FileExistsError if file already exists
            fd = os.open(str(pid_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, PID_FILE_MODE)
        except FileExistsError:  # noqa: PERF203 — the try/except IS the retry mechanism for atomic PID-file creation
            # File exists — check if the owning process is alive
            try:
                existing_pid = int(pid_path.read_text().strip())
                os.kill(existing_pid, 0)  # Signal 0 checks liveness; raises if dead
                # Process is alive — skip spawning
                return
            except (ValueError, OSError):
                # Dead process (OSError ESRCH) or unreadable PID — reap and retry
                with contextlib.suppress(OSError):
                    pid_path.unlink()
                continue
        else:
            break

    # We hold the exclusive lock (fd is open for writing)
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)  # noqa: S603 — spawns a trusted internal command, not untrusted input
        # Write child PID so the child process can clean up the file on exit
        os.write(fd, str(proc.pid).encode())
    finally:
        os.close(fd)


def _needs_reimport(settings: dict | None = None) -> bool:
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
        return False


def _needs_backfill(settings: dict | None = None) -> bool:
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
    # Initialized before the try so the output block below always runs, even if
    # the body raises — the hook must print a valid response, never crash start.
    additional_context: str | None = None
    try:
        ensure_parent_dir(DEFAULT_DB_PATH)

        # Clean up stale temp files from crashed/killed sync processes
        _reap_stale_temp_files()

        settings = load_settings()

        db_absent = not DEFAULT_DB_PATH.exists()

        if db_absent or _needs_reimport(settings):
            _spawn_background(["ccrecall", "import"], import_conversations.PID_KEY)

        if _needs_backfill(settings):
            _spawn_background(["ccrecall", "backfill", "summaries"], backfill_summaries.PID_KEY)

        # Note: embedding backfill is NOT auto-spawned. Embeddings are filled
        # forward by embed-on-write (active leaves only); historical seeding is
        # opt-in via `ccrecall backfill embeddings [--days N] [--limit N]` so
        # embedding the full history never fires unbidden (machines.md thrash risk).

        # Pre-warm the fastembed model cache so sync-current's first embed never
        # triggers an invisible ~120 MB download. PID-guarded via _spawn_background
        # (O_CREAT|O_EXCL): at most one concurrent warm runs at a time.
        # Runs on every SessionStart; fast no-op after the first download since the
        # model is already cached on disk.
        _spawn_background(["ccrecall-warm-model"], WARM_MODEL_PID_KEY)
    except Exception:
        # Top-level hook guard: must never crash the session start. Log
        # best-effort (no-op unless logging_enabled) so the failure isn't silent.
        log_hook_exception("memory-setup")

    output: dict = {"continue": True}
    if additional_context is not None:
        output["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
