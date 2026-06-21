"""Retrieve recent conversation sessions from the memory database.

Returns markdown by default (token-efficient), or JSON when output_format="json"
(the CLI maps the global --json flag onto that argument).
"""

import json
import sqlite3
import sys
from pathlib import Path

from ccrecall.db import DEFAULT_DB_PATH, get_db_connection
from ccrecall.formatting import format_json_sessions, format_markdown_session
from ccrecall.serialization import decode_json_column

# Upper bound on --n, single-sourced here and referenced by the CLI validator
# (cli/commands.py) so the clamp and the validator can't drift apart.
MAX_RECENT_SESSIONS = 20


def get_recent_sessions(
    conn: sqlite3.Connection,
    n: int = 3,
    sort_order: str = "desc",
    before: str | None = None,
    after: str | None = None,
    projects: list[str] | None = None,
    session_id: str | None = None,
    path: str | None = None,
    verbose: bool = False,
    include_notifications: bool = False,
) -> list[dict]:
    """Get n most recent sessions with all their messages."""
    cursor = conn.cursor()

    # Check if tool_counts column exists (may not on pre-migration DBs)
    cursor.execute("PRAGMA table_info(branches)")
    branch_columns = {row[1] for row in cursor.fetchall()}
    has_tool_counts = "tool_counts" in branch_columns

    tool_counts_col = ", b.tool_counts" if has_tool_counts else ""
    sql = f"""
        SELECT s.id, s.uuid, b.started_at, b.ended_at, b.exchange_count,
               b.files_modified, b.commits, s.git_branch,
               p.name as project, p.path as project_path,
               b.id as branch_db_id{tool_counts_col}
        FROM sessions s
        JOIN branches b ON b.session_id = s.id AND b.is_active = 1
        JOIN projects p ON s.project_id = p.id
        WHERE 1=1
    """
    params = []

    if session_id:
        sql += " AND s.uuid LIKE ? ESCAPE '\\'"
        escaped = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"{escaped}%")

    if before:
        sql += " AND b.started_at < ?"
        params.append(before)

    if after:
        sql += " AND b.started_at > ?"
        params.append(after)

    if projects:
        placeholders = ",".join("?" * len(projects))
        sql += f" AND p.name IN ({placeholders})"
        params.extend(projects)

    if path:
        sql += " AND s.cwd LIKE ? ESCAPE '\\'"
        escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")

    order = "DESC" if sort_order == "desc" else "ASC"
    sql += f" ORDER BY b.ended_at {order} LIMIT ?"
    params.append(n)

    cursor.execute(sql, params)
    sessions = cursor.fetchall()

    results = []

    for session in sessions:
        if has_tool_counts:
            (
                _session_id,
                uuid,
                started_at,
                ended_at,
                _exchange_count,
                files_json,
                commits_json,
                git_branch,
                project,
                _project_path,
                branch_db_id,
                tool_counts_json,
            ) = session
        else:
            (
                _session_id,
                uuid,
                started_at,
                ended_at,
                _exchange_count,
                files_json,
                commits_json,
                git_branch,
                project,
                _project_path,
                branch_db_id,
            ) = session
            tool_counts_json = None

        cursor.execute(
            """
            SELECT m.role, m.content, m.timestamp, COALESCE(m.is_notification, 0) as is_notification
            FROM branch_messages bm
            JOIN messages m ON bm.message_id = m.id
            WHERE bm.branch_id = ?
              AND (? OR COALESCE(m.is_notification, 0) = 0)
            ORDER BY m.timestamp ASC
        """,
            (branch_db_id, include_notifications),
        )

        messages = [
            {"role": r, "content": c, "timestamp": t, "is_notification": notif} for r, c, t, notif in cursor.fetchall()
        ]

        session_data = {
            "uuid": uuid,
            "project": project,
            "started_at": started_at,
            "ended_at": ended_at,
            "git_branch": git_branch,
            "messages": messages,
        }

        if verbose:
            session_data["files_modified"] = decode_json_column(files_json, [])
            session_data["commits"] = decode_json_column(commits_json, [])
            session_data["tool_counts"] = decode_json_column(tool_counts_json, {})

        results.append(session_data)

    return results


def format_markdown(sessions: list[dict], verbose: bool = False) -> str:
    """Format sessions as markdown."""
    if not sessions:
        return "No sessions found."

    lines = [f"# Recent Conversations ({len(sessions)} sessions)\n"]
    lines.extend(format_markdown_session(session, verbose=verbose) for session in sessions)

    return "\n".join(lines)


def run(
    *,
    n: int = 3,
    sort_order: str = "desc",
    before: str | None = None,
    after: str | None = None,
    session: str | None = None,
    project: str | None = None,
    path: str | None = None,
    output_format: str = "markdown",
    verbose: bool = False,
    include_notifications: bool = False,
    db: Path = DEFAULT_DB_PATH,
) -> None:
    """Get recent conversation sessions."""
    # Backstop for direct callers; the CLI validator rejects out-of-range --n
    # before reaching here. Both sides bound on MAX_RECENT_SESSIONS.
    n = max(1, min(MAX_RECENT_SESSIONS, n))
    projects = [p.strip() for p in project.split(",")] if project else None

    if not db.exists():
        if output_format == "json":
            print(json.dumps({"error": "Database not found", "sessions": [], "total_sessions": 0}))
        else:
            print("Error: Database not found. Run memory setup first.")
        sys.exit(1)

    try:
        settings = {"db_path": str(db)} if db != DEFAULT_DB_PATH else None
        conn = get_db_connection(settings)
        sessions = get_recent_sessions(
            conn,
            n=n,
            sort_order=sort_order,
            before=before,
            after=after,
            projects=projects,
            session_id=session,
            path=path,
            verbose=verbose,
            include_notifications=include_notifications,
        )
        conn.close()

        if output_format == "json":
            print(format_json_sessions(sessions))
        else:
            print(format_markdown(sessions, verbose=verbose))

    # Deliberately broad: top-level CLI handler — reports any error to the user
    # and exits non-zero rather than dumping a traceback.
    except Exception as e:
        if output_format == "json":
            print(json.dumps({"error": str(e), "sessions": [], "total_sessions": 0}))
        else:
            print(f"Error: {e}")
        sys.exit(1)
