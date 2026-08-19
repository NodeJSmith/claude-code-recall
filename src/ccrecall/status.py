"""Consolidated health/status reporting for the CLI.

Default status reads are read-only; ``--check-ingestion`` also records
confirmed-OK cache metadata for future deep-check runs.
"""

import contextlib
import json
import logging
import sqlite3
import sys
from pathlib import Path

from ccrecall.config import DEFAULT_DB_PATH, get_db_path, load_settings_for_db
from ccrecall.db import get_connection
from ccrecall.db_vec import branch_embedding_coverage, chunk_vec_queryable, vec_available
from ccrecall.hooks.backfill_status import count_status as count_embedding_status
from ccrecall.import_log_ops import import_log_source_index
from ccrecall.ingestion_status import summarize_ingestion
from ccrecall.models import LOGGER_NAME
from ccrecall.tool_content_status import count_eligible as count_tool_content_pending
from ccrecall.tool_content_status import count_pending_missing_jsonl
from ccrecall.tool_content_status import count_total_sessions as count_tool_content_total

log = logging.getLogger(LOGGER_NAME)


@contextlib.contextmanager
def _readonly_connection(db_path: Path, *, load_vec: bool = False):
    """Open an existing SQLite DB without creating/migrating it."""
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    try:
        if load_vec:
            vec_available(conn)
        yield conn
    finally:
        conn.close()


def _db_counts(conn: sqlite3.Connection) -> dict[str, int]:
    cursor = conn.cursor()
    projects = cursor.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    sessions = cursor.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    messages = cursor.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    branches = cursor.execute("SELECT COUNT(*) FROM branches").fetchone()[0]
    active_branches = cursor.execute("SELECT COUNT(*) FROM branches WHERE is_active = 1").fetchone()[0]
    branch_invariant_violations = count_branch_invariant_violations(conn)
    return {
        "projects": projects,
        "sessions": sessions,
        "messages": messages,
        "branches": branches,
        "active_branches": active_branches,
        "branch_invariant_violations": branch_invariant_violations,
    }


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def count_branch_invariant_violations(conn: sqlite3.Connection) -> int:
    """Count sessions with more than one active branch row."""
    return len(
        conn.execute(
            "SELECT session_id FROM branches WHERE is_active = 1 GROUP BY session_id HAVING COUNT(*) > 1"
        ).fetchall()
    )


def collect_status(*, db: Path = DEFAULT_DB_PATH, days: int | None = None, check_ingestion: bool = False) -> dict:
    """Collect status across DB, ingestion, tool content, and embeddings."""
    settings = load_settings_for_db(db)
    db_path = get_db_path(settings)
    ingestion = None

    with _readonly_connection(db_path, load_vec=False) as conn:
        cursor = conn.cursor()
        db_counts = _db_counts(conn)
        schema_current = _has_column(conn, "messages", "tool_content")

        if not schema_current:
            return {
                "db_path": str(db_path),
                "db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
                "days": days,
                "database": db_counts,
                "schema": {"current": False, "reason": "messages.tool_content column missing"},
                "ingestion": None,
                "tool_content": {
                    "total_sessions": None,
                    "done_sessions": None,
                    "pending_sessions": None,
                    "pending_backfillable_sessions": None,
                    "pending_missing_jsonl_sessions": None,
                },
                "embeddings": {
                    "watermark": {"embedded_branches": None, "total_branches": None},
                    "backfill": {
                        "available": False,
                        "error": "database schema is out of date; run `ccrecall import` to migrate",
                        "branches": {"embedded": None, "total": None, "remaining": None, "errored": None},
                        "chunks": {"done": None, "total": None},
                    },
                },
            }

        tool_pending = count_tool_content_pending(cursor, days)
        source_index = import_log_source_index(cursor) if tool_pending else None
        tool_missing = count_pending_missing_jsonl(cursor, days, source_index) if tool_pending else 0
        tool_total = count_tool_content_total(cursor, days)
        embedded_watermark, embeddable_watermark = branch_embedding_coverage(conn)

    if check_ingestion:
        if not db_path.exists():
            raise FileNotFoundError(db_path)
        with get_connection(settings, load_vec=False) as conn:
            ingestion = summarize_ingestion(conn, sources=import_log_source_index(conn.cursor()))

    embedding_backfill: dict = {
        "available": False,
        "error": "sqlite-vec unavailable or not initialized",
        "branches": {"embedded": None, "total": None, "remaining": None, "errored": None},
        "chunks": {"done": None, "total": None},
    }
    try:
        with _readonly_connection(db_path, load_vec=True) as conn:
            if chunk_vec_queryable(conn):
                counts = count_embedding_status(conn.cursor(), days)
                embedding_backfill = {
                    "available": True,
                    "error": None,
                    "branches": {
                        "embedded": counts["embedded_branches"],
                        "total": counts["total_branches"],
                        "remaining": counts["eligible"],
                        "errored": counts["errored"],
                    },
                    "chunks": {"done": counts["done"], "total": counts["universe"]},
                }
    except (sqlite3.Error, OSError) as exc:
        log.warning(
            "embedding backfill status query failed; reporting unavailable",
            exc_info=True,
            extra={"db_path": str(db_path)},
        )
        embedding_backfill["error"] = str(exc)

    return {
        "db_path": str(db_path),
        "db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "days": days,
        "database": db_counts,
        "schema": {"current": True, "reason": None},
        "ingestion": ingestion,
        "tool_content": {
            "total_sessions": tool_total,
            "done_sessions": tool_total - tool_pending,
            "pending_sessions": tool_pending,
            "pending_backfillable_sessions": tool_pending - tool_missing,
            "pending_missing_jsonl_sessions": tool_missing,
        },
        "embeddings": {
            "watermark": {"embedded_branches": embedded_watermark, "total_branches": embeddable_watermark},
            "backfill": embedding_backfill,
        },
    }


def print_status_report(status: dict) -> None:
    """Render collected status in a compact human-readable form."""
    db = status["database"]
    print(f"Database: {status['db_path']}")
    print(f"Size: {status['db_size_bytes'] / (1024 * 1024):.2f} MB")
    print(f"Projects: {db['projects']}")
    print(f"Sessions: {db['sessions']}")
    print(f"Branches: {db['branches']} ({db['active_branches']} active)")
    print(f"Messages: {db['messages']}")
    print(f"Branch invariant violations: {db['branch_invariant_violations']} session(s) with multiple active branches")

    schema = status.get("schema", {"current": True})
    if not schema["current"]:
        print(f"Schema: out of date ({schema['reason']})")
        print("Run `ccrecall import` to apply migrations before checking ingestion/backfill status.")
        return

    ingestion = status["ingestion"]
    if ingestion is None:
        print("Ingestion: not checked (run `ccrecall status --check-ingestion`)")
    else:
        print(
            "Ingestion: "
            f"{ingestion['ok_sessions']}/{ingestion['sessions_checked']} sessions up to date; "
            f"pending tail {ingestion['pending_tail_sessions']} session(s) "
            f"/{ingestion['pending_tail_turns']} turn(s); "
            f"stale tail {ingestion['stale_tail_sessions']} session(s) "
            f"/{ingestion['stale_tail_turns']} turn(s); "
            f"gaps {ingestion['ingestion_gap_sessions']} session(s) "
            f"/{ingestion['ingestion_gap_turns']} turn(s); "
            f"missing source {ingestion['missing_source_sessions']} session(s)"
        )

    tool = status["tool_content"]
    total = tool["total_sessions"]
    done = tool["done_sessions"]
    pct = (done / total * 100) if total else 0.0
    print(f"Tool content: {done}/{total} sessions backfilled ({pct:.0f}%)")
    print(
        f"  remaining: {tool['pending_sessions']} sessions; "
        f"backfillable: {tool['pending_backfillable_sessions']}; "
        f"missing JSONL: {tool['pending_missing_jsonl_sessions']}"
    )

    embeddings = status["embeddings"]
    backfill = embeddings["backfill"]
    if backfill["available"]:
        branches = backfill["branches"]
        chunks = backfill["chunks"]
        total = branches["total"]
        embedded = branches["embedded"]
        pct = (embedded / total * 100) if total else 0.0
        print(f"Embeddings: {embedded}/{total} branches embedded ({pct:.0f}%)")
        print(f"  branch backfill: {branches['remaining']} remaining; {branches['errored']} errored")
        print(f"  chunk coverage: {chunks['done']}/{chunks['total']} chunks at current version")
    else:
        watermark = embeddings["watermark"]
        total = watermark["total_branches"]
        embedded = watermark["embedded_branches"]
        print(f"Embeddings: unavailable ({backfill['error']})")
        print(f"  watermark: {embedded}/{total} branches")


def run(
    *,
    db: Path = DEFAULT_DB_PATH,
    days: int | None = None,
    check_ingestion: bool = False,
    output_format: str = "markdown",
) -> None:
    try:
        status = collect_status(db=db, days=days, check_ingestion=check_ingestion)
    except FileNotFoundError as exc:
        if output_format == "json":
            print(json.dumps({"error": "database_not_found", "path": str(exc.filename or exc.args[0])}))
        else:
            print(f"ccrecall status: database not found: {exc.filename or exc.args[0]}", file=sys.stderr)
        raise SystemExit(1) from exc
    if output_format == "json":
        print(json.dumps(status, indent=2))
    else:
        print_status_report(status)
