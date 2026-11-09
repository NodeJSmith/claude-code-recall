"""Structured error output for agent-friendly diagnostics.

Every error is written to stderr as a single JSON line with remediation hints,
so agents can parse it programmatically regardless of --json mode. This replaces
the former unstructured stderr text — the JSON envelope is the only error output.
"""

import errno
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from ccrecall.db_base import is_database_locked

CODE_SCHEMA_AHEAD = "database_schema_ahead"
CODE_LOCKED = "database_locked"
CODE_DISK_FULL = "disk_full"
CODE_CORRUPT = "database_corrupt"
CODE_DATABASE_ERROR = "database_error"
CODE_FILESYSTEM_ERROR = "filesystem_error"

# Primary SQLite result codes for a damaged database file.
_CORRUPT_CODES = frozenset({sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB})


def emit_error(
    message: str,
    *,
    code: str,
    exit_code: int,
    remediation: str | None = None,
) -> None:
    """Write a structured error to stderr and exit.

    The JSON envelope is always one line on stderr, parseable by agents even
    when stdout carries other output. The envelope is the only error output.

    Exit codes:
      1 — runtime error (DB missing, query failed, transient)
      2 — usage error (bad flags, missing required args)
    """
    envelope: dict[str, str | int] = {"error": message, "code": code}
    if remediation:
        envelope["remediation"] = remediation
    envelope["exit_code"] = exit_code
    print(json.dumps(envelope), file=sys.stderr)
    raise SystemExit(exit_code)


def emit_error_return(
    message: str,
    *,
    code: str,
    exit_code: int,
    remediation: str | None = None,
) -> int:
    """Write a structured error to stderr and return the exit code.

    Same as emit_error but returns instead of raising SystemExit — for
    functions that communicate exit codes via return value (e.g. session_tail).
    """
    envelope: dict[str, str | int] = {"error": message, "code": code}
    if remediation:
        envelope["remediation"] = remediation
    envelope["exit_code"] = exit_code
    print(json.dumps(envelope), file=sys.stderr)
    return exit_code


@dataclass(frozen=True)
class FailureReport:
    """One line for the user: what failed and why (``message``), and what to do (``remediation``)."""

    message: str
    code: str
    remediation: str


def classify_failure(
    exc: sqlite3.Error | OSError,
    *,
    operation: str,
    schema_ahead_cause: str,
    log_path: Path | None,
) -> FailureReport:
    """Turn a database or filesystem exception into one actionable line.

    A database ahead of this install explains any failure it causes, so it wins
    over the exception's own class. ``log_path`` is the log holding the full
    traceback, or None when logging is disabled.
    """
    see_log = (
        f"Full traceback in {log_path}."
        if log_path
        else "Set logging_enabled to true in the ccrecall config to keep tracebacks."
    )
    if schema_ahead_cause:
        return FailureReport(
            f"{operation} failed: {schema_ahead_cause} ({exc})",
            CODE_SCHEMA_AHEAD,
            f"Upgrade ccrecall the way you installed it. {see_log}",
        )
    sqlite_code = getattr(exc, "sqlite_errorcode", 0) & 0xFF  # primary result code, as in is_database_locked
    if isinstance(exc, sqlite3.OperationalError) and is_database_locked(exc):
        return FailureReport(
            f"{operation} failed: the database is locked by another ccrecall process",
            CODE_LOCKED,
            f"Retry once the other process finishes; `ccrecall status` shows what is running. {see_log}",
        )
    if sqlite_code == sqlite3.SQLITE_FULL or (isinstance(exc, OSError) and exc.errno == errno.ENOSPC):
        return FailureReport(
            f"{operation} failed: the disk is full",
            CODE_DISK_FULL,
            f"Free up disk space, then retry. {see_log}",
        )
    if sqlite_code in _CORRUPT_CODES:
        return FailureReport(
            f"{operation} failed: the database file is damaged ({exc})",
            CODE_CORRUPT,
            "Restore it from a backup, or move it aside and run `ccrecall import` to rebuild it from "
            f"transcripts (history whose transcripts are gone is lost). {see_log}",
        )
    if isinstance(exc, sqlite3.Error):
        return FailureReport(f"{operation} failed: database error: {exc}", CODE_DATABASE_ERROR, see_log)
    return FailureReport(f"{operation} failed: {exc}", CODE_FILESYSTEM_ERROR, see_log)


def emit_failure(report: FailureReport) -> None:
    """Write a classified failure as the standard error envelope and exit 1."""
    emit_error(report.message, code=report.code, exit_code=1, remediation=report.remediation)
