"""Incremental sync for the current session only (Stop-hook helper — fast and lightweight).

Reads session_id from stdin (or --input-file) and only syncs that session file.
Detects conversation branches (from rewind) and stores each branch separately.

v3 schema: messages stored once per session, branches as a separate index.
"""

import contextlib
import json
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pydantic import ValidationError

from ccrecall.config import (
    DEFAULT_LOG_PATH,
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    PID_FILE_MODE,
    ensure_parent_dir,
    load_settings,
    pid_file_path,
    remove_pid_file,
    setup_logging,
)
from ccrecall.db import DEFAULT_PROJECTS_DIR, chunk_vec_queryable, get_connection
from ccrecall.embeddings import is_model_cached_on_disk
from ccrecall.formatting import extract_project_name, normalize_cwd
from ccrecall.health import REASON_VEC_UNAVAILABLE, clear_embedding_failure, record_embedding_failure
from ccrecall.models import HookInput
from ccrecall.session_ops import sync_session

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# PID-file concurrency guard: at most one sync-current at a time.
# Skip (not queue) if another is running — recovered on the next Stop.
PID_KEY = "ccrecall-sync-current"

# Dedicated logger for the cold-model warning, kept separate from the main
# ccrecall logger (LOGGER_NAME) so it fires regardless of logging_enabled (see
# _warn_cold_model). The hyphen is intentional: "cold-model" keeps this name
# distinct from the dotted "ccrecall.*" loggers that setup_logging gates, so the
# warning isn't suppressed. Do not "normalize" the hyphen — it is load-bearing.
COLD_MODEL_LOGGER_NAME = "ccrecall.cold-model"


def _warn_cold_model() -> None:
    """Best-effort warning when the embedding model is absent from the disk cache.

    Fires regardless of logging_enabled by writing directly to the ccrecall log
    file, because the detached context has logging off by default — making an
    invisible ~120 MB download the silent failure mode this warning is designed to
    surface. Wrapped entirely in try/except so it can never raise.
    """
    if is_model_cached_on_disk():
        return  # disk cache present — load will be fast, no download risk

    try:
        ensure_parent_dir(DEFAULT_LOG_PATH)
        warn_logger = logging.getLogger(COLD_MODEL_LOGGER_NAME)
        if not warn_logger.handlers:
            handler = RotatingFileHandler(
                DEFAULT_LOG_PATH,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
            )
            warn_logger.addHandler(handler)
            warn_logger.setLevel(logging.WARNING)
        warn_logger.warning(
            "sync-current: embedding model not yet warmed in this detached process — "
            "first embed may trigger a ~120 MB download. "
            "Pre-warm by running `ccrecall-warm-model` or let the setup hook do it."
        )
    except Exception:  # noqa: S110 — best-effort warn; must never raise in a hook
        pass


def validate_session_id(session_id: str) -> bool:
    """Validate that session_id is a proper UUID to prevent path traversal."""
    return bool(session_id and _UUID_RE.match(session_id))


def _is_under(path: Path, base: Path) -> bool:
    """Check whether path resolves to a location under base (symlink-escape guard)."""
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def get_session_file(projects_dir: Path, session_id: str) -> Path | None:
    """Find the JSONL file for a session ID. Validates path stays under projects_dir."""
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue

        # Check main session files
        session_file = project_dir / f"{session_id}.jsonl"
        if session_file.exists():
            # Verify resolved path is still under projects_dir (symlink escape prevention)
            if _is_under(session_file, projects_dir):
                return session_file
            continue

        # Check subagent files
        for subdir in project_dir.iterdir():
            if subdir.is_dir():
                subagents_dir = subdir / "subagents"
                if subagents_dir.exists():
                    for f in subagents_dir.glob(f"*{session_id}*.jsonl"):
                        if _is_under(f, projects_dir):
                            return f

    return None


def run(input_file: Path | None = None) -> None:
    """Sync only the current session into the memory DB (Stop-hook helper)."""
    # Concurrency guard.
    # At most one sync-current at a time: skip (not queue) if another is alive.
    # Reap stale locks (dead PID) so a crash doesn't permanently block syncing.
    pid_path = pid_file_path(PID_KEY)
    # Ensure the runtime dir exists: on a fresh machine the Stop hook can fire
    # before anything else creates ~/.ccrecall/. Without this, the os.open() below
    # raises an uncaught FileNotFoundError (it's before the try/finally), leaving
    # the hook with no stdout — a violation of the {"continue": true} contract.
    ensure_parent_dir(pid_path)
    while True:
        try:
            # Atomic create — fails with FileExistsError if file already exists
            lock_fd = os.open(str(pid_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, PID_FILE_MODE)
        except FileExistsError:  # noqa: PERF203 — try/except IS the retry mechanism
            try:
                existing_pid = int(pid_path.read_text().strip())
                os.kill(existing_pid, 0)  # signal 0: liveness probe, no signal sent
                # Another sync-current is alive — skip; recovered on the next Stop
                print(json.dumps({"continue": True}))
                return
            except ValueError:
                # Unreadable PID file — reap and retry
                with contextlib.suppress(OSError):
                    pid_path.unlink()
                continue
            except PermissionError:
                # Process exists but we lack permission to signal it — treat as alive, skip
                print(json.dumps({"continue": True}))
                return
            except OSError:
                # ProcessLookupError (ESRCH) — process is dead — reap and retry
                with contextlib.suppress(OSError):
                    pid_path.unlink()
                continue
        else:
            # We hold the exclusive lock — write our PID so the next caller can
            # detect whether we're still alive via os.kill(pid, 0)
            try:
                os.write(lock_fd, str(os.getpid()).encode())
            finally:
                os.close(lock_fd)
            break

    try:
        settings = load_settings()
        logger = setup_logging(settings, process_name="sync")

        # Read hook input from file or stdin
        if input_file:
            try:
                raw = input_file.read_text(encoding="utf-8")
            except OSError:
                raw = ""
            finally:
                # Clean up temp file
                with contextlib.suppress(OSError):
                    input_file.unlink()
        else:
            raw = sys.stdin.read()

        try:
            hook_input = HookInput.model_validate_json(raw) if raw else HookInput()
        except ValidationError:
            hook_input = HookInput()

        session_id = hook_input.session_id

        if not session_id or not validate_session_id(session_id):
            # No session ID or invalid format — exit silently
            print(json.dumps({"continue": True}))
            return

        # Honor exclude_projects for the live session too — import applies it on the
        # batch path, and without this an excluded project's current session would
        # still sync on Stop. Match by the current cwd's project name. This uses the
        # same formula as the import path (extract_project_name(normalize_cwd(...))),
        # just on the live cwd instead of each session's recorded cwd — identical in
        # the normal case (they're the same cwd). Fail open when cwd is absent: a
        # Stop hook shouldn't block, and cwd is effectively always present.
        exclude_projects = settings["exclude_projects"]
        if exclude_projects and hook_input.cwd:
            project_name = extract_project_name(normalize_cwd(hook_input.cwd))
            if project_name in exclude_projects:
                logger.info("Skipping sync — project %r is excluded", project_name)
                print(json.dumps({"continue": True}))
                return

        session_file = get_session_file(DEFAULT_PROJECTS_DIR, session_id)

        if not session_file:
            print(json.dumps({"continue": True}))
            return

        # Warn best-effort if the model hasn't been warmed in this detached process:
        # the first embed call may trigger a ~120 MB download, which would be invisible
        # since detached processes have logging off by default.
        _warn_cold_model()

        try:
            with get_connection(settings, load_vec=True) as conn:
                project_dir = session_file.parent

                # Handle subagent paths
                if project_dir.name == "subagents":
                    project_dir = project_dir.parent.parent

                # Embedding capability check: sqlite-vec availability determines whether
                # embedding can run. Record a failure on unavailability; clear on success.
                # Only the vec check is accessible here — model failures in sync_current
                # are silently swallowed by session_ops (contextlib.suppress) and are
                # detected authoritatively by backfill_embeddings instead.
                # Both calls are best-effort: a sidecar write failure must never affect
                # the hook's output or exit behavior.
                _vec_ok = chunk_vec_queryable(conn)
                if not _vec_ok:
                    with contextlib.suppress(Exception):  # best-effort; must not affect hook behavior
                        record_embedding_failure(reason=REASON_VEC_UNAVAILABLE)

                new_messages = sync_session(
                    conn,
                    session_file,
                    project_dir,
                )
            # conn is committed and closed on the line above (context manager exit).

            if new_messages > 0:
                logger.info("Synced %s new message(s) from session %s", new_messages, session_id[:8])

            # Clear the embedding failure sidecar on a clean sync pass.
            # Only when vec was available — if it wasn't, we already recorded above
            # and must not clear until the next run where embedding can actually run.
            if _vec_ok:
                with contextlib.suppress(Exception):  # best-effort; must not affect hook behavior
                    clear_embedding_failure()

            # Output for hook (continue = True means don't block)
            output = {"continue": True}
            if new_messages > 0:
                output["suppressOutput"] = True  # Don't show in transcript

            print(json.dumps(output))

        except Exception as e:
            logger.error("Sync error: %s", e)
            # Don't block Claude on sync errors
            print(json.dumps({"continue": True}))
            sys.exit(0)

    finally:
        # Best-effort PID-file cleanup — must run on every exit path (normal,
        # early return, exception) so the next Stop can acquire the lock.
        remove_pid_file(PID_KEY)
