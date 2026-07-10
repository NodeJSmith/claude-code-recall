"""Import Claude Code JSONL conversations into the SQLite memory database.

Extracts only searchable text content, skipping progress entries (90% of file size).
Detects conversation branches (from rewind) and stores each branch separately.

v3 schema: messages stored once per session, branches as separate index.
"""

import contextlib
import ctypes
import gc
import hashlib
import logging
import resource
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ccrecall.config import DEFAULT_DB_PATH, get_db_path, load_settings, remove_pid_file, setup_logging
from ccrecall.db import (
    DEFAULT_PROJECTS_DIR,
    TRIGGER_CHUNKS_VEC_AD,
    branch_embedding_coverage,
    get_connection,
    vec_available,
)
from ccrecall.formatting import extract_project_name, normalize_project_key
from ccrecall.models import LOGGER_NAME
from ccrecall.parsing import extract_session_uuid
from ccrecall.project_ops import upsert_project
from ccrecall.session_ops import sync_session

# Chunk size for streaming a file through the change-detection hash (bounded memory).
HASH_CHUNK_SIZE = 8192
BYTES_PER_MB = 1024 * 1024
KB_PER_MB = 1024

log = logging.getLogger(LOGGER_NAME)

# PID key — must stay in sync with the spawn in memory_setup (`ccrecall import`).
PID_KEY = "ccrecall-import"


def _rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / KB_PER_MB
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / KB_PER_MB


def get_file_hash(filepath: Path) -> str:
    """Get MD5 hash of file for change detection."""
    hasher = hashlib.md5(usedforsecurity=False)
    with open(filepath, "rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def import_session(
    conn: sqlite3.Connection,
    filepath: Path,
    project_id: int,
) -> tuple[int, int]:
    """
    Import a single session JSONL file with v3 schema.
    Messages stored once, branches tracked via branch_messages.
    Returns: (branches_imported, total_message_count)

    This function is a thin adapter over session_ops.sync_session that preserves
    the (conn, filepath, project_id) calling convention used by import_project and
    the test suite.  Hash-based dedup and import_log writes are handled here.
    """
    cursor = conn.cursor()

    st = filepath.stat()
    file_size = st.st_size
    file_mtime = st.st_mtime

    # Fast path: if size + mtime match the stored values, skip without hashing.
    cursor.execute(
        "SELECT id, file_hash, file_size, file_mtime FROM import_log WHERE file_path = ?",
        (str(filepath),),
    )
    log_row = cursor.fetchone()
    if log_row and log_row[2] == file_size and log_row[3] == file_mtime:
        log.debug("skip %s (%.1f MB, stat match)", filepath.name, file_size / BYTES_PER_MB)
        return -1, 0

    # Stat changed or no prior record — fall back to full hash comparison.
    file_hash = get_file_hash(filepath)
    if log_row and log_row[1] is not None and log_row[1] == file_hash:
        # Content unchanged despite stat difference (e.g. touch without edit).
        # Update stored stat so the fast path works next time.
        cursor.execute(
            "UPDATE import_log SET file_size = ?, file_mtime = ? WHERE id = ?",
            (file_size, file_mtime, log_row[0]),
        )
        log.debug("skip %s (%.1f MB, hash match, stat updated)", filepath.name, file_size / BYTES_PER_MB)
        return -1, 0

    # Delegate to shared session_ops logic.
    # Pass the pre-resolved project_id via _project_id to skip a redundant
    # project upsert (import_project already handled it via upsert_project).
    new_messages = sync_session(
        conn,
        filepath,
        filepath.parent,
        file_hash=file_hash,
        _project_id=project_id,
        embed=False,
        file_size=file_size,
        file_mtime=file_mtime,
    )

    if new_messages == -1:
        # sync_session returns -1 when it found an exact hash match and skipped
        return -1, 0

    # Gather branch and message counts for the return value
    session_uuid = extract_session_uuid(filepath)

    cursor.execute("SELECT id FROM sessions WHERE uuid = ?", (session_uuid,))
    session_row = cursor.fetchone()
    if not session_row:
        return -1, 0
    session_id = session_row[0]

    cursor.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,))
    total_messages = cursor.fetchone()[0]

    if total_messages == 0:
        # All of this session's content was filtered out (tool results,
        # notifications, empty text), but sync_session's find_all_branches still
        # inserted branch rows before that filtering. Tear down the FK chain
        # grandchild->child->parent (branch_messages -> branches -> sessions) so
        # the session delete doesn't trip the branches.session_id constraint.
        #
        # The branches_chunks_ad → chunks_vec_ad cascade reaches chunk_vec (a
        # vec0 virtual table). If the trigger exists and the extension isn't
        # loaded, the DELETE crashes with "no such module: vec0". Load it
        # on demand (same approach as _apply_migrations, which loads vec
        # before migration DML that triggers the same cascade).
        has_vec_cascade = (
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                (TRIGGER_CHUNKS_VEC_AD,),
            ).fetchone()
            is not None
        )
        if has_vec_cascade:
            vec_available(conn)

        cursor.execute(
            "DELETE FROM branch_messages WHERE branch_id IN (SELECT id FROM branches WHERE session_id = ?)",
            (session_id,),
        )
        cursor.execute("DELETE FROM branches WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return -1, 0

    cursor.execute(
        "SELECT COUNT(*) FROM branches "
        "WHERE session_id = ? AND aggregated_content IS NOT NULL AND aggregated_content != ''",
        (session_id,),
    )
    branches_imported = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM branches WHERE session_id = ?", (session_id,))
    if cursor.fetchone()[0] == 0:
        return -1, 0

    log.debug(
        "imported %s (%.1f MB): %d branches, %d messages [RSS %.0f MB]",
        filepath.name,
        file_size / BYTES_PER_MB,
        branches_imported,
        total_messages,
        _rss_mb(),
    )
    return branches_imported, total_messages


def _noop() -> None:
    pass


def import_project(
    conn: sqlite3.Connection,
    project_dir: Path,
    exclude_projects: list[str] | None = None,
    reclaim_memory: Callable[[], None] = _noop,
) -> tuple[int, int, int]:
    """
    Import all sessions from a project directory.
    Returns: (sessions_imported, messages_imported, sessions_skipped)
    """
    cursor = conn.cursor()

    project_key = normalize_project_key(project_dir.name)

    # Upsert project using the JSONL-probe strategy for accurate path derivation
    project_id = upsert_project(cursor, project_key, project_dir=project_dir)

    # Check exclusion after we know the real project name
    cursor.execute("SELECT name FROM projects WHERE id = ?", (project_id,))
    project_row = cursor.fetchone()
    project_name = project_row[0] if project_row else extract_project_name(str(project_dir))

    if exclude_projects and project_name in exclude_projects:
        return 0, 0, 0

    sessions_imported = 0
    messages_imported = 0
    sessions_skipped = 0

    for jsonl_file in sorted(project_dir.glob("*.jsonl")):
        if jsonl_file.name.startswith("."):
            continue

        branches_count, msg_count = import_session(conn, jsonl_file, project_id)
        if branches_count == -1:
            sessions_skipped += 1
        else:
            sessions_imported += branches_count
            messages_imported += msg_count
            reclaim_memory()

    if sessions_imported or sessions_skipped:
        log.debug(
            "project %s (%s): %d branches imported, %d sessions skipped [RSS %.0f MB]",
            project_name,
            project_dir.name,
            sessions_imported,
            sessions_skipped,
            _rss_mb(),
        )

    return sessions_imported, messages_imported, sessions_skipped


def print_stats(db: Path = DEFAULT_DB_PATH) -> None:
    """Print row counts and on-disk size for the memory DB to stdout.

    Read-only: deliberately does NOT touch the import PID file (it shares no
    lifecycle with run()), so `ccrecall stats` can't delete a live background
    import's PID sentinel and let the session hook spawn a duplicate import.
    load_vec=False keeps it genuinely read-only — the counts never query
    chunk_vec, so there's no reason to create and commit the vec schema here.
    """
    settings = load_settings()
    if db != DEFAULT_DB_PATH:
        settings["db_path"] = str(db)
    db_path = get_db_path(settings)

    with get_connection(settings, load_vec=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM projects")
        projects = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM sessions")
        sessions = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM messages")
        messages = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM branches")
        total_branches = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM branches WHERE is_active = 1")
        active = cursor.fetchone()[0]
        # Branch-grain embedding coverage (vec-free watermark count).
        embedded, embeddable = branch_embedding_coverage(conn)

    db_size = db_path.stat().st_size if db_path.exists() else 0

    print(f"Database: {db_path}")
    print(f"Size: {db_size / BYTES_PER_MB:.2f} MB")
    print(f"Projects: {projects}")
    print(f"Sessions: {sessions}")
    print(f"Branches: {total_branches} ({active} active)")
    print(f"Messages: {messages}")
    if embeddable:
        print(f"Embeddings: {embedded}/{embeddable} branches ({embedded / embeddable * 100:.0f}%)")
    else:
        print("Embeddings: 0/0 branches")


def run(
    *,
    db: Path = DEFAULT_DB_PATH,
    projects_dir: Path = DEFAULT_PROJECTS_DIR,
    project: str | None = None,
    verbose: bool = False,
) -> None:
    """Import Claude Code conversations into the memory DB."""
    try:
        _run(db=db, projects_dir=projects_dir, project=project, verbose=verbose)
    finally:
        # Delete PID file so _spawn_background can spawn again next session
        remove_pid_file(PID_KEY)


def _run(
    *,
    db: Path,
    projects_dir: Path,
    project: str | None,
    verbose: bool,
) -> None:
    settings = load_settings()
    logger = setup_logging(settings, process_name="import", verbose=verbose)

    if db != DEFAULT_DB_PATH:
        settings["db_path"] = str(db)
    db_path = get_db_path(settings)
    exclude_projects = settings.get("exclude_projects", [])

    total_sessions = 0
    total_messages = 0
    total_skipped = 0

    t_start = time.monotonic()

    # load_vec=True so the chunks_vec_ad cascade trigger works during empty-session
    # cleanup (DELETE FROM branches fires triggers that touch chunk_vec). embed=False
    # on sync_session keeps the expensive embedding model unloaded.
    with get_connection(settings, load_vec=True) as conn:
        t_conn = time.monotonic()
        logger.debug("connection opened in %.2fs", t_conn - t_start)

        # gc.collect() frees Python objects; malloc_trim() releases freed glibc arena
        # pages back to the OS (without it, RSS grows monotonically). Runs between
        # sessions (not just projects) so large projects don't accumulate unbounded.
        libc: ctypes.CDLL | None = None
        if sys.platform == "linux":
            with contextlib.suppress(OSError):
                libc = ctypes.CDLL("libc.so.6")

        t_gc_total = 0.0

        def reclaim_memory() -> None:
            nonlocal t_gc_total
            t0 = time.monotonic()
            gc.collect()
            if libc is not None:
                libc.malloc_trim(0)
            t_gc_total += time.monotonic() - t0

        if project:
            project_dir = projects_dir / project
            if not project_dir.exists():
                print(f"Project not found: {project_dir}")
                return

            sessions, messages, skipped = import_project(conn, project_dir, exclude_projects, reclaim_memory)
            conn.commit()
            total_sessions += sessions
            total_messages += messages
            total_skipped += skipped
            print(f"Imported {project}: {sessions} branches, {messages} messages")
        else:
            t_import_total = 0.0
            t_commit_total = 0.0
            project_count = 0

            for project_dir in sorted(projects_dir.iterdir()):
                if not project_dir.is_dir() or project_dir.name.startswith("."):
                    continue

                project_count += 1
                t0 = time.monotonic()
                sessions, messages, skipped = import_project(conn, project_dir, exclude_projects, reclaim_memory)
                t_import_total += time.monotonic() - t0

                t0 = time.monotonic()
                conn.commit()
                t_commit_total += time.monotonic() - t0

                total_sessions += sessions
                total_messages += messages
                total_skipped += skipped

                if sessions > 0 or messages > 0:
                    print(f"Imported {project_dir.name}: {sessions} branches, {messages} messages")

            logger.debug(
                "timing: %d projects, import=%.2fs, commit=%.2fs, gc=%.2fs",
                project_count,
                t_import_total,
                t_commit_total,
                t_gc_total,
            )

    t_end = time.monotonic()
    logger.debug("total wall time: %.2fs", t_end - t_start)
    logger.info("Import complete: %s branches, %s messages", total_sessions, total_messages)
    print(f"\nTotal: {total_sessions} branches, {total_messages} messages imported ({total_skipped} unchanged)")

    if db_path.exists():
        db_size = db_path.stat().st_size
        print(f"Database size: {db_size / BYTES_PER_MB:.2f} MB")
