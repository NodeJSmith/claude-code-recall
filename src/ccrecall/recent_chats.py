"""Retrieve recent conversation sessions from the memory database.

Returns markdown by default (token-efficient), or JSON when output_format="json"
(the CLI maps the global --json flag onto that argument).
"""

import logging
import sqlite3
from pathlib import Path

from ccrecall.config import DEFAULT_DB_PATH
from ccrecall.dates import validate_or_exit
from ccrecall.db import get_connection, has_tool_counts, parse_project_filter, resolve_db_settings
from ccrecall.db_vec import fetch_branch_messages
from ccrecall.errors import emit_error
from ccrecall.formatting import format_json_sessions, format_markdown_session
from ccrecall.models import LOGGER_NAME
from ccrecall.search_query import EMPTY_SCOPE, ScopeFilter, scope_filter_clause
from ccrecall.serialization import decode_json_column

log = logging.getLogger(LOGGER_NAME)

# Upper bound on --n, single-sourced here and referenced by the CLI validator
# (cli/commands.py) so the clamp and the validator can't drift apart.
MAX_RECENT_SESSIONS = 20


def get_recent_sessions(
    conn: sqlite3.Connection,
    n: int = 3,
    sort_order: str = "desc",
    scope: ScopeFilter = EMPTY_SCOPE,
    verbose: bool = False,
    include_notifications: bool = False,
) -> list[dict]:
    """Get n most recent sessions with all their messages."""
    cursor = conn.cursor()

    has_tc = has_tool_counts(cursor)

    tool_counts_col = ", b.tool_counts" if has_tc else ""
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
    scope_sql, params = scope_filter_clause(scope)
    sql += scope_sql

    order = "DESC" if sort_order == "desc" else "ASC"
    sql += f" ORDER BY b.ended_at {order} LIMIT ?"
    params.append(n)

    cursor.execute(sql, params)
    sessions = cursor.fetchall()

    results = []

    for session in sessions:
        if has_tc:
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

        messages = fetch_branch_messages(cursor, branch_db_id, include_notifications)

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
    projects = parse_project_filter(project)

    # Reassignment is required, not stylistic: a non-UTC offset instant is
    # normalized to UTC here, and the original (unnormalized) value would
    # compare incorrectly against the stored UTC timestamps.
    before, after = validate_or_exit(before, after)

    if not db.exists():
        emit_error(
            "Database not found",
            code="db_not_found",
            exit_code=1,
            remediation="Run ccrecall import or start a session with the ccrecall plugin installed.",
        )

    scope = ScopeFilter(projects=projects, session_id=session, path=path, before=before, after=after)

    try:
        settings = resolve_db_settings(db)
        with get_connection(settings) as conn:
            sessions = get_recent_sessions(
                conn,
                n=n,
                sort_order=sort_order,
                scope=scope,
                verbose=verbose,
                include_notifications=include_notifications,
            )

        if output_format == "json":
            print(format_json_sessions(sessions))
        else:
            print(format_markdown(sessions, verbose=verbose))

    except Exception as e:
        emit_error(
            str(e),
            code="query_error",
            exit_code=1,
            remediation="Check ccrecall status for database health.",
        )
