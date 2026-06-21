#!/usr/bin/env python3
"""
Search conversations using full-text search with FTS5/FTS4/LIKE fallback,
optionally fused with vector KNN via Reciprocal Rank Fusion.

Returns markdown by default (token-efficient), JSON with --format json.
"""

import json
import sqlite3
import sys
from pathlib import Path

import sqlite_vec

from ccrecall.content import sanitize_fts_term

# Local imports
from ccrecall.db import (
    DEFAULT_DB_PATH,
    EMBEDDABLE_BRANCH_FILTER,
    branch_vec_queryable,
    get_db_connection,
)
from ccrecall.embeddings import (
    DEPS_AVAILABLE,
    EMBEDDING_MODEL,
    EMBEDDING_VERSION,
    embed_text,
    model_available,
)
from ccrecall.formatting import format_json_sessions, format_markdown_session
from ccrecall.fusion import rrf
from ccrecall.schema import detect_fts_support
from ccrecall.serialization import decode_json_column


def _get_fts_branch_ids(
    cursor: sqlite3.Cursor,
    query: str,
    fts_level: str | None,
    top_k: int,
    projects: list[str] | None = None,
    session_id: str | None = None,
    path: str | None = None,
) -> list[int]:
    """Return an ordered list of branch IDs from FTS or LIKE search.

    Returns branch_ids only (no hydration), ordered by relevance descending.
    Returns empty list when query is empty or fts_level is None and LIKE has no results.
    """
    terms = query.split()
    if not terms:
        return []

    params: list = []

    if fts_level in ("fts5", "fts4"):
        sanitized_terms = [sanitize_fts_term(term) for term in terms]
        sanitized_terms = [t for t in sanitized_terms if t]
        if not sanitized_terms:
            return []
        fts_query = " OR ".join(f'"{term}"' for term in sanitized_terms)

        sql = """
            SELECT b.id
            FROM branches_fts
            JOIN branches b ON branches_fts.rowid = b.id
            JOIN sessions s ON b.session_id = s.id
            JOIN projects p ON s.project_id = p.id
            WHERE b.is_active = 1
              AND branches_fts MATCH ?
        """
        params.append(fts_query)

        if projects:
            placeholders = ",".join("?" * len(projects))
            sql += f" AND p.name IN ({placeholders})"
            params.extend(projects)

        if session_id:
            sql += " AND s.uuid LIKE ? ESCAPE '\\'"
            escaped = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"{escaped}%")

        if path:
            sql += " AND s.cwd LIKE ? ESCAPE '\\'"
            escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")

        if fts_level == "fts5":
            sql += " ORDER BY bm25(branches_fts) LIMIT ?"
        else:
            sql += " ORDER BY b.ended_at DESC LIMIT ?"
        params.append(top_k)

    else:
        # LIKE fallback
        like_clauses = " AND ".join("b.aggregated_content LIKE ?" for _ in terms)
        sql = f"""
            SELECT b.id
            FROM branches b
            JOIN sessions s ON b.session_id = s.id
            JOIN projects p ON s.project_id = p.id
            WHERE b.is_active = 1
              AND {like_clauses}
        """
        params.extend(f"%{term}%" for term in terms)

        if projects:
            placeholders = ",".join("?" * len(projects))
            sql += f" AND p.name IN ({placeholders})"
            params.extend(projects)

        if session_id:
            sql += " AND s.uuid LIKE ? ESCAPE '\\'"
            escaped = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"{escaped}%")

        if path:
            sql += " AND s.cwd LIKE ? ESCAPE '\\'"
            escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")

        sql += " ORDER BY b.ended_at DESC LIMIT ?"
        params.append(top_k)

    cursor.execute(sql, params)
    return [row[0] for row in cursor.fetchall()]


def _get_vec_branch_ids(
    cursor: sqlite3.Cursor,
    query_vec: list[float],
    top_k: int,
    projects: list[str] | None = None,
    session_id: str | None = None,
    path: str | None = None,
) -> list[int]:
    """Return ordered branch IDs from vec0 KNN, filtered to current embedding version.

    Only returns branches whose embedding_version == EMBEDDING_VERSION and
    embedding_model == EMBEDDING_MODEL (stale-version exclusion).
    Returns empty list on any error.
    """
    try:
        serialized = sqlite_vec.serialize_float32(query_vec)
        knn_k = top_k
        rows = cursor.execute(
            "SELECT branch_id, distance FROM branch_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (serialized, knn_k),
        ).fetchall()
    except Exception:
        return []

    if not rows:
        return []

    candidate_ids = [row[0] for row in rows]
    placeholders = ",".join("?" * len(candidate_ids))

    # Filter to current embedding version and apply optional user filters
    filter_sql = f"""
        SELECT b.id
        FROM branches b
        JOIN sessions s ON b.session_id = s.id
        JOIN projects p ON s.project_id = p.id
        WHERE b.id IN ({placeholders})
          AND b.is_active = 1
          AND b.embedding_version = ?
          AND b.embedding_model = ?
    """
    filter_params: list = [*list(candidate_ids), EMBEDDING_VERSION, EMBEDDING_MODEL]

    if projects:
        ph2 = ",".join("?" * len(projects))
        filter_sql += f" AND p.name IN ({ph2})"
        filter_params.extend(projects)

    if session_id:
        filter_sql += " AND s.uuid LIKE ? ESCAPE '\\'"
        escaped = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filter_params.append(f"{escaped}%")

    if path:
        filter_sql += " AND s.cwd LIKE ? ESCAPE '\\'"
        escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filter_params.append(f"%{escaped}%")

    try:
        valid_ids = {row[0] for row in cursor.execute(filter_sql, filter_params).fetchall()}
    except Exception:
        return []

    # Preserve KNN distance ordering, keeping only valid (current-version) IDs
    return [bid for bid in candidate_ids if bid in valid_ids]


def _dedup_by_session(cursor: sqlite3.Cursor, ordered_branch_ids: list[int]) -> list[int]:
    """Keep the highest-ranked branch per session_id.

    Returns a new list with one branch per session, preserving relative order.
    """
    if not ordered_branch_ids:
        return []

    placeholders = ",".join("?" * len(ordered_branch_ids))
    rows = cursor.execute(
        f"SELECT id, session_id FROM branches WHERE id IN ({placeholders})",
        ordered_branch_ids,
    ).fetchall()

    id_to_session: dict[int, int] = {row[0]: row[1] for row in rows}

    seen_sessions: set[int] = set()
    deduped: list[int] = []
    for bid in ordered_branch_ids:
        sess = id_to_session.get(bid)
        if sess is None:
            continue
        if sess not in seen_sessions:
            seen_sessions.add(sess)
            deduped.append(bid)
    return deduped


def _hydrate_branches(
    cursor: sqlite3.Cursor,
    branch_ids: list[int],
    verbose: bool = False,
    include_notifications: bool = False,
) -> list[dict]:
    """Fetch session metadata and messages for an ordered list of branch IDs."""
    if not branch_ids:
        return []

    placeholders = ",".join("?" * len(branch_ids))
    rows = cursor.execute(
        f"""
        SELECT s.id, s.uuid, b.started_at, b.ended_at, b.files_modified,
               b.commits, s.git_branch, p.name as project, b.id as branch_db_id
        FROM branches b
        JOIN sessions s ON b.session_id = s.id
        JOIN projects p ON s.project_id = p.id
        WHERE b.id IN ({placeholders})
        """,
        branch_ids,
    ).fetchall()

    # Index by branch_id so we can re-order by the caller's ranking
    branch_map: dict[int, tuple] = {row[8]: row for row in rows}

    results = []
    for bid in branch_ids:
        row = branch_map.get(bid)
        if row is None:
            continue

        (
            _session_id,
            uuid,
            started_at,
            ended_at,
            files_json,
            commits_json,
            git_branch,
            project,
            branch_db_id,
        ) = row

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

        results.append(session_data)

    return results


def search_sessions(
    conn: sqlite3.Connection,
    query: str,
    fts_level: str | None,
    max_results: int = 5,
    projects: list[str] | None = None,
    session_id: str | None = None,
    path: str | None = None,
    verbose: bool = False,
    include_notifications: bool = False,
    keyword_only: bool = False,
) -> list[dict]:
    """Search for sessions using branch-level FTS with BM25 ranking, FTS4 MATCH, or LIKE fallback.

    When model and vec extension are available and keyword_only is False, results
    are fused from vector KNN and FTS via Reciprocal Rank Fusion. Otherwise falls
    back to keyword-only ranking. Degrades silently to keyword path on any error.
    """
    terms = query.split()
    if not terms:
        return []

    cursor = conn.cursor()
    top_k = max(max_results * 4, 20)

    use_fusion = not keyword_only
    query_vec: list[float] | None = None
    _emit_degrade: bool = False

    # Attempt vector path only if model and vec extension are both available
    if use_fusion:
        if not model_available():
            use_fusion = False
        elif not branch_vec_queryable(conn):
            use_fusion = False
            _emit_degrade = True

    if use_fusion:
        try:
            query_vec = embed_text(query)
        except Exception:
            use_fusion = False
            _emit_degrade = True

    if use_fusion and query_vec is not None:
        try:
            fts_ids = _get_fts_branch_ids(cursor, query, fts_level, top_k, projects, session_id, path)
            vec_ids = _get_vec_branch_ids(cursor, query_vec, top_k, projects, session_id, path)
            fused_ids = rrf([fts_ids, vec_ids])
            deduped_ids = _dedup_by_session(cursor, fused_ids)
            ordered_ids = deduped_ids[:max_results]
            return _hydrate_branches(cursor, ordered_ids, verbose, include_notifications)
        except Exception:
            _emit_degrade = True

    if _emit_degrade and not keyword_only:
        print(
            "search: vector index unavailable, using keyword search",
            file=sys.stderr,
        )

    # Keyword-only path (FTS or LIKE)
    branch_ids = _get_fts_branch_ids(cursor, query, fts_level, top_k, projects, session_id, path)
    deduped_ids = _dedup_by_session(cursor, branch_ids)
    ordered_ids = deduped_ids[:max_results]
    return _hydrate_branches(cursor, ordered_ids, verbose, include_notifications)


def format_markdown(sessions: list[dict], query: str, verbose: bool = False) -> str:
    """Format sessions as markdown."""
    if not sessions:
        return f"No sessions found for query: {query}"

    lines = [f'# Search Results: "{query}" ({len(sessions)} sessions)\n']
    lines.extend(format_markdown_session(session, verbose=verbose) for session in sessions)

    return "\n".join(lines)


def print_status(settings: dict | None) -> None:
    """Print diagnostic status and exit 0."""
    # Open one connection with vec loaded; branch_vec_queryable probes whether
    # the vec table is usable — get_db_connection already loaded the extension
    # iff it could, so the table-existence probe is sufficient.
    try:
        conn = get_db_connection(settings, load_vec=True)
        is_vec = branch_vec_queryable(conn)
    except Exception:
        conn = None
        is_vec = False
    print(f"vec extension: {'yes' if is_vec else 'no'}")

    # Model: name + whether the embedding stack imports. Deliberately does not
    # call model_available() — that constructs the fastembed model and would
    # download it (~120 MB) on a cold cache, which a read-only status must not do.
    print(f"model: {EMBEDDING_MODEL} (deps {'available' if DEPS_AVAILABLE else 'missing'})")

    # Embedded vs total branch counts — reuse the same connection
    if conn is not None:
        try:
            # Denominator is the shared embeddable universe (db.EMBEDDABLE_BRANCH_FILTER),
            # the same predicate the backfill's build_selection()/count_status() use,
            # so this diagnostic can't drift from `ccrecall backfill embeddings --status`.
            total = conn.execute(f"SELECT count(*) FROM branches WHERE {EMBEDDABLE_BRANCH_FILTER}").fetchone()[0]
            embedded = conn.execute(
                f"SELECT count(*) FROM branches WHERE {EMBEDDABLE_BRANCH_FILTER}"
                " AND embedding_version = ? AND embedding_model = ?",
                (EMBEDDING_VERSION, EMBEDDING_MODEL),
            ).fetchone()[0]
            print(f"embedded branches: {embedded}/{total}")
        except Exception as e:
            print(f"embedded branches: error ({e})")
        finally:
            conn.close()
    else:
        print("embedded branches: error (could not open database)")

    sys.exit(0)


def run(
    *,
    query: str | None = None,
    status: bool = False,
    keyword_only: bool = False,
    max_results: int = 5,
    session: str | None = None,
    project: str | None = None,
    path: str | None = None,
    output_format: str = "markdown",
    verbose: bool = False,
    include_notifications: bool = False,
    db: Path = DEFAULT_DB_PATH,
) -> None:
    """Search conversation sessions (keyword + vector fusion)."""
    # Validate: exactly one of --query / --status must be provided.
    if not status and not query:
        print("error: one of --query/-q or --status is required", file=sys.stderr)
        sys.exit(2)
    if status and query:
        print("error: --query and --status are mutually exclusive", file=sys.stderr)
        sys.exit(2)

    max_results = max(1, min(10, max_results))
    projects = [p.strip() for p in project.split(",")] if project else None

    if not db.exists():
        if status:
            # For --status, report missing DB gracefully rather than hard-exiting.
            # Model identity is independent of the DB, so report it even when the
            # DB is absent (deps check only — no download in a read-only path).
            print("vec extension: no")
            print(f"model: {EMBEDDING_MODEL} (deps {'available' if DEPS_AVAILABLE else 'missing'})")
            print("embedded branches: error (database not found)")
            sys.exit(0)
        if output_format == "json":
            print(json.dumps({"error": "Database not found", "sessions": [], "query": query}))
        else:
            print("Error: Database not found. Run memory setup first.")
        sys.exit(1)

    settings = {"db_path": str(db)} if db != DEFAULT_DB_PATH else None

    if status:
        print_status(settings)
        return  # print_status calls sys.exit(0), but be explicit

    # Past the status branch with the xor-validation above satisfied, query is
    # guaranteed present (status is False, so a missing query already exited 2).
    assert query is not None  # noqa: S101 — type-checker narrowing; the real guard is the exit above

    try:
        conn = get_db_connection(settings, load_vec=True)
        fts_level = detect_fts_support(conn)

        sessions = search_sessions(
            conn,
            query=query,
            fts_level=fts_level,
            max_results=max_results,
            projects=projects,
            session_id=session,
            path=path,
            verbose=verbose,
            include_notifications=include_notifications,
            keyword_only=keyword_only,
        )
        conn.close()

        if output_format == "json":
            print(format_json_sessions(sessions, {"query": query}))
        else:
            print(format_markdown(sessions, query, verbose=verbose))

    except Exception as e:
        if output_format == "json":
            print(json.dumps({"error": str(e), "sessions": [], "query": query}))
        else:
            print(f"Error: {e}")
        sys.exit(1)
