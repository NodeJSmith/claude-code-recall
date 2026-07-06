"""Import-log bookkeeping for session sync/import.

Both the sync and import pipelines track per-file dedup state in the
``import_log`` table. ``import_log_skip_check`` decides whether a file can be
skipped; ``upsert_import_log`` writes the post-sync row.
"""

import sqlite3
from pathlib import Path


def import_log_skip_check(
    cursor: sqlite3.Cursor,
    filepath: Path,
    write_import_log: bool,
    file_hash: str | None,
) -> tuple[tuple | None, bool]:
    """Probe import_log for an existing row; return (log_row, should_skip).

    Returns (log_row, True) when write_import_log is set, file_hash is provided,
    and the stored hash is non-NULL and matches — caller should return -1.
    Returns (log_row, False) otherwise (NULL-hash stale or no row).
    Preserves the NULL-hash-stale asymmetry: a stored NULL is never a match.
    """
    if write_import_log:
        cursor.execute(
            "SELECT id, file_hash FROM import_log WHERE file_path = ?",
            (str(filepath),),
        )
        log_row = cursor.fetchone()
        if log_row and log_row[1] is not None and log_row[1] == file_hash:
            return log_row, True
        return log_row, False
    return None, False


def upsert_import_log(
    cursor: sqlite3.Cursor,
    filepath: Path,
    session_id: int,
    file_hash: str | None,
    log_row: tuple | None,
) -> None:
    """UPDATE or INSERT the import_log row for this file."""
    cursor.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,))
    total_messages = cursor.fetchone()[0]

    if log_row:
        cursor.execute(
            """
            UPDATE import_log
            SET file_hash = ?, imported_at = CURRENT_TIMESTAMP, messages_imported = ?
            WHERE file_path = ?
            """,
            (file_hash, total_messages, str(filepath)),
        )
    else:
        cursor.execute(
            "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, ?, ?)",
            (str(filepath), file_hash, total_messages),
        )
