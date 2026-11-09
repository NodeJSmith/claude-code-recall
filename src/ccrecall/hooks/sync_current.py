"""Incremental sync for the current session only (Stop-hook helper — fast and lightweight).

Reads session_id from stdin (or --input-file) and only syncs that session file.
Detects conversation branches (from rewind) and stores each branch separately.

v3 schema: messages stored once per session, branches as a separate index.
"""

import contextlib
import json
import logging
import re
import sqlite3
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pydantic import ValidationError

from ccrecall import job_lock, run_history
from ccrecall.config import (
    DEFAULT_LOG_PATH,
    JOB_KEY_SYNC,
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    ensure_parent_dir,
    get_db_path,
    load_settings,
    setup_logging,
)
from ccrecall.db import DEFAULT_PROJECTS_DIR, get_connection
from ccrecall.db_base import is_database_locked
from ccrecall.db_vec import chunk_vec_queryable
from ccrecall.embed_ops import embed_branch_chunks, resolve_sync_cap
from ccrecall.embeddings import is_model_cached_on_disk
from ccrecall.formatting import extract_project_name, normalize_cwd
from ccrecall.health import REASON_VEC_UNAVAILABLE, clear_embedding_failure, record_embedding_failure
from ccrecall.hooks.subprocess_utils import reclaim_memory, try_load_libc
from ccrecall.models import HookInput
from ccrecall.parsing import extract_session_uuid
from ccrecall.session_ops import sync_session
from ccrecall.transcript_sources import discover_session_transcript_files

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# Reasons a vec-ok clear here may erase. Scoped to what this hook's own
# structural check (chunk_vec_queryable) actually verifies — REASON_VEC_UNAVAILABLE
# and the no-reason/malformed-record case (None) — so a REASON_MODEL_UNAVAILABLE
# record written by backfill (an orthogonal, unverified-here failure mode)
# survives instead of being silently clobbered (#164).
_VEC_OK_CLEAR_REASONS: set[str | None] = {None, REASON_VEC_UNAVAILABLE}

# Job-lock concurrency guard: at most one sync-current at a time.
# Skip (not queue) if another is running — recovered on the next Stop.
JOB_KEY = JOB_KEY_SYNC

# Dedicated logger for the cold-model warning, kept separate from the main
# ccrecall logger (LOGGER_NAME) so it fires regardless of logging_enabled (see
# _warn_cold_model). The hyphen is intentional: "cold-model" keeps this name
# distinct from the dotted "ccrecall.*" loggers that setup_logging gates, so the
# warning isn't suppressed. Do not "normalize" the hyphen — it is load-bearing.
COLD_MODEL_LOGGER_NAME = "ccrecall.cold-model"

# Hook-contract payload printed at every early-return site, serialized once.
# The success path is excluded: it may add suppressOutput before dumping.
_CONTINUE_OUTPUT = json.dumps({"continue": True})


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


def get_session_file(projects_dir: Path, session_id: str) -> Path | None:
    """Find the JSONL file for a session ID. Validates path stays under projects_dir."""
    discovery = discover_session_transcript_files(projects_dir, session_id)
    if discovery.had_matching_unsafe_path:
        return None
    if discovery.files:
        return discovery.files[0]
    return None


def _project_root_for_session_file(session_file: Path, projects_dir: Path) -> Path:
    """Resolve the top-level Claude project dir for a direct or nested subagent transcript."""
    try:
        relative = session_file.relative_to(projects_dir)
    except ValueError:
        return session_file.parent
    if not relative.parts:
        return session_file.parent
    return projects_dir / relative.parts[0]


def embed_session_branch(conn: sqlite3.Connection, session_file: Path, settings: dict, logger: logging.Logger) -> None:
    """Embed the session's branch. Runs after sync commits, so a failure here never undoes its messages.

    A failure leaves the branch eligible for backfill, and the next Stop tries again.
    """
    row = conn.execute(
        """
        SELECT b.id FROM branches b JOIN sessions s ON s.id = b.session_id
        WHERE s.uuid = ? AND b.is_active = 1
        """,
        (extract_session_uuid(session_file),),
    ).fetchone()
    if row is None:
        return

    # Return the parse's memory to the OS before the inference peak.
    reclaim_memory(try_load_libc())
    try:
        embed_branch_chunks(
            conn,
            row[0],
            cap_limit=resolve_sync_cap(settings.get("sync_path_token_limit")),
            yield_to_backfill=True,
        )
    except Exception as exc:
        if isinstance(exc, sqlite3.OperationalError) and is_database_locked(exc):
            logger.warning("embedding skipped: database locked; left for backfill", extra={"branch_id": row[0]})
        else:
            logger.exception("embedding failed for branch; left for backfill", extra={"branch_id": row[0]})


def run(input_file: Path | None = None) -> None:
    """Sync only the current session into the memory DB (Stop-hook helper)."""
    # Concurrency guard: at most one sync-current at a time; skip (not queue) if
    # another holds the lock. job_lock.acquire creates the runtime dir, since a
    # fresh machine can fire Stop before anything else creates ~/.ccrecall/.
    lock = job_lock.acquire(JOB_KEY)
    if lock is None:
        # Another sync-current is running — skip; recovered on the next Stop
        print(_CONTINUE_OUTPUT)
        return

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
            print(_CONTINUE_OUTPUT)
            return

        # Honor exclude_projects for the live session too — import applies it on the
        # batch path, and without this an excluded project's current session would
        # still sync on Stop. Match by the current cwd's project name. The import
        # path also has a key-suffix fallback (key_could_match_excluded) for the
        # lossy case where cwd metadata is missing; this path doesn't need it
        # because the live hook always has cwd. Fail open when cwd is absent: a
        # Stop hook shouldn't block, and cwd is effectively always present.
        exclude_projects = settings["exclude_projects"]
        if exclude_projects and hook_input.cwd:
            project_name = extract_project_name(normalize_cwd(hook_input.cwd))
            if project_name in exclude_projects:
                logger.info("Skipping sync — project %r is excluded", project_name)
                print(_CONTINUE_OUTPUT)
                return

        session_file = get_session_file(DEFAULT_PROJECTS_DIR, session_id)

        if not session_file:
            print(_CONTINUE_OUTPUT)
            return

        # Warn best-effort if the model hasn't been warmed in this detached process:
        # the first embed call may trigger a ~120 MB download, which would be invisible
        # since detached processes have logging off by default.
        _warn_cold_model()

        # Recorded only from here: a run that exited above attempted no work.
        with run_history.record_run(get_db_path(settings), run_history.KIND_SYNC) as recorder:
            try:
                file_size = session_file.stat().st_size
                started = time.monotonic()

                with get_connection(settings, load_vec=True) as conn:
                    project_dir = _project_root_for_session_file(session_file, DEFAULT_PROJECTS_DIR)

                    # Embedding capability check: sqlite-vec availability determines whether
                    # embedding can run. Record a failure on unavailability; clear on success.
                    # Only the vec check is accessible here — model failures in sync_current
                    # are logged by embed_session_branch and are detected authoritatively
                    # by backfill_embeddings instead.
                    # Both calls are best-effort: a sidecar write failure must never affect
                    # the hook's output or exit behavior.
                    vec_ok = chunk_vec_queryable(conn)
                    if not vec_ok:
                        with contextlib.suppress(Exception):  # best-effort; must not affect hook behavior
                            record_embedding_failure(reason=REASON_VEC_UNAVAILABLE)

                    new_messages = sync_session(conn, session_file, project_dir)
                    # Commit here, not at the `with` exit: inference must run with no write lock held.
                    conn.commit()

                    if vec_ok:
                        embed_session_branch(conn, session_file, settings, logger)

                if new_messages > 0:
                    logger.info("Synced %s new message(s) from session %s", new_messages, session_id[:8])

                elapsed = time.monotonic() - started
                logger.info("sync complete", extra={"file_size": file_size, "duration_s": elapsed})

                # Clear the embedding failure sidecar on a clean sync pass.
                # Only when vec was available — if it wasn't, we already recorded above
                # and must not clear until the next run where embedding can actually run.
                # Scoped to _VEC_OK_CLEAR_REASONS: this check only verifies sqlite-vec,
                # not the embedding model, so a model_unavailable record from backfill
                # must not be erased here (#164).
                if vec_ok:
                    with contextlib.suppress(Exception):  # best-effort; must not affect hook behavior
                        clear_embedding_failure(reasons=_VEC_OK_CLEAR_REASONS)

                # Output for hook (continue = True means don't block)
                output = {"continue": True}
                if new_messages > 0:
                    output["suppressOutput"] = True  # Don't show in transcript

                recorder.finish(run_history.OUTCOME_OK)
                print(json.dumps(output))

            except Exception as exc:
                logger.error("Sync error: %s", exc)
                recorder.finish(run_history.OUTCOME_FAILED, run_history.failure_reason(exc))
                # Don't block Claude on sync errors
                print(_CONTINUE_OUTPUT)
                sys.exit(0)

    finally:
        # Runs on every exit path (normal, early return, exception, sys.exit) so
        # the next Stop can acquire the lock and the record doesn't read as killed.
        lock.release()
