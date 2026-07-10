"""Import-log bookkeeping for session sync/import.

Both the sync and import pipelines track per-file dedup state in the
``import_log`` table. ``import_log_skip_check`` decides whether a file can be
skipped; ``upsert_import_log`` writes the post-sync row.
"""

import logging
import sqlite3
from pathlib import Path

from ccrecall.models import LOGGER_NAME

log = logging.getLogger(LOGGER_NAME)


def import_log_skip_check(
    cursor: sqlite3.Cursor,
    filepath: Path,
    file_hash: str | None,
) -> tuple[tuple | None, bool]:
    """Probe import_log for an existing row; return (log_row, should_skip).

    Returns (log_row, True) when file_hash is provided and the stored hash is
    non-NULL and matches — caller should return -1.
    Returns (log_row, False) otherwise (NULL-hash stale or no row).
    Preserves the NULL-hash-stale asymmetry: a stored NULL is never a match.
    """
    cursor.execute(
        "SELECT id, file_hash FROM import_log WHERE file_path = ?",
        (str(filepath),),
    )
    log_row = cursor.fetchone()
    if log_row and log_row[1] is not None and log_row[1] == file_hash:
        return log_row, True
    return log_row, False


def upsert_import_log(
    cursor: sqlite3.Cursor,
    filepath: Path,
    session_id: int,
    file_hash: str | None,
    log_row: tuple | None,
    file_size: int | None = None,
    file_mtime: float | None = None,
) -> None:
    """UPDATE or INSERT the import_log row for this file."""
    cursor.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,))
    total_messages = cursor.fetchone()[0]

    if log_row:
        cursor.execute(
            """
            UPDATE import_log
            SET file_hash = ?, imported_at = CURRENT_TIMESTAMP,
                messages_imported = ?, file_size = ?, file_mtime = ?
            WHERE file_path = ?
            """,
            (file_hash, total_messages, file_size, file_mtime, str(filepath)),
        )
        log.debug(
            "import_log UPDATE %s: hash=%s size=%s mtime=%s rows_affected=%d",
            filepath.name,
            file_hash,
            file_size,
            file_mtime,
            cursor.rowcount,
        )
    else:
        cursor.execute(
            "INSERT INTO import_log (file_path, file_hash, messages_imported, file_size, file_mtime)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(filepath), file_hash, total_messages, file_size, file_mtime),
        )
        log.debug(
            "import_log INSERT %s: hash=%s size=%s mtime=%s",
            filepath.name,
            file_hash,
            file_size,
            file_mtime,
        )
