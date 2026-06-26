"""Search conversations with FTS5/FTS4/LIKE, optionally fused with chunk-KNN via RRF.

Returns markdown by default (token-efficient), or JSON when output_format="json"
(the CLI maps the global --json flag onto that argument).
"""

import json
import logging
import sqlite3
import sys
from pathlib import Path

import sqlite_vec

from ccrecall.content import sanitize_fts_term
from ccrecall.db import (
    CHUNK_EMBEDDABLE_BRANCH_FILTER,
    DEFAULT_DB_PATH,
    chunk_vec_queryable,
    escape_like,
    get_db_connection,
)
from ccrecall.embeddings import (
    DEPS_AVAILABLE,
    EMBEDDING_MODEL,
    EMBEDDING_VERSION,
    embed_text,
    model_available,
)
from ccrecall.formatting import (
    apply_scores,
    build_envelope,
    format_card_json,
    format_card_markdown,
    format_result_list_markdown,
    format_snippet_json,
    format_snippet_markdown,
)
from ccrecall.fusion import rrf_scored
from ccrecall.schema import detect_fts_support
from ccrecall.serialization import decode_json_column

_logger = logging.getLogger("ccrecall")

# Upper bound on --max-results, single-sourced here and referenced by the CLI
# validator (cli/commands.py) so the clamp and the validator can't drift apart.
MAX_SEARCH_RESULTS = 10

# Each ranker (FTS, vector KNN) is over-fetched before fusion + per-session dedup,
# so the post-filter top-N still has enough candidates: top_k = max(N * mult, floor).
OVERFETCH_MULTIPLIER = 4
OVERFETCH_FLOOR = 20

# Chunk overfetch factor for A: chunk-KNN returns many chunks per branch, so the
# post-best-chunk-per-branch rollup may collapse N chunks to far fewer branches.
# Multiply the overfetch by this factor so the post-rollup session count fills
# max_results. Start at 8 (generous chunks-per-session estimate).
CHUNK_COLLAPSE_FACTOR = 8


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
            params.append(f"{escape_like(session_id)}%")

        if path:
            sql += " AND s.cwd LIKE ? ESCAPE '\\'"
            params.append(f"%{escape_like(path)}%")

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
            params.append(f"{escape_like(session_id)}%")

        if path:
            sql += " AND s.cwd LIKE ? ESCAPE '\\'"
            params.append(f"%{escape_like(path)}%")

        sql += " ORDER BY b.ended_at DESC LIMIT ?"
        params.append(top_k)

    cursor.execute(sql, params)
    return [row[0] for row in cursor.fetchall()]


def _execute_chunk_knn(
    cursor: sqlite3.Cursor,
    query_vec: list[float],
    top_k: int,
    projects: list[str] | None = None,
    session_id: str | None = None,
    path: str | None = None,
) -> list[tuple[int, int, float]]:
    """Shared chunk-KNN core: run the vec MATCH query, filter to valid chunks, return in KNN order.

    Returns [(chunk_id, branch_id, distance)] without any per-branch rollup —
    both A's rollup and B's no-rollup path build on this single MATCH query.
    Filters to current embedding version + model at the chunk grain (FR#9), so
    a partially re-embedded branch still contributes its already-current chunks.
    Returns empty list on sqlite3.Error so callers can degrade; non-DB bugs propagate.
    """
    try:
        serialized = sqlite_vec.serialize_float32(query_vec)
        knn_rows = cursor.execute(
            "SELECT chunk_id, distance FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (serialized, top_k),
        ).fetchall()
    except sqlite3.Error:
        return []

    if not knn_rows:
        return []

    chunk_ids = [row[0] for row in knn_rows]
    chunk_to_dist: dict[int, float] = {row[0]: row[1] for row in knn_rows}

    placeholders = ",".join("?" * len(chunk_ids))
    filter_sql = f"""
        SELECT ch.id as chunk_id, b.id as branch_id
        FROM chunks ch
        JOIN branches b ON ch.branch_id = b.id
        JOIN sessions s ON b.session_id = s.id
        JOIN projects p ON s.project_id = p.id
        WHERE ch.id IN ({placeholders})
          AND ch.embedding_version = ?
          AND ch.embedding_model = ?
          AND b.is_active = 1
    """
    filter_params: list = [*chunk_ids, EMBEDDING_VERSION, EMBEDDING_MODEL]

    if projects:
        proj_placeholders = ",".join("?" * len(projects))
        filter_sql += f" AND p.name IN ({proj_placeholders})"
        filter_params.extend(projects)

    if session_id:
        filter_sql += " AND s.uuid LIKE ? ESCAPE '\\'"
        filter_params.append(f"{escape_like(session_id)}%")

    if path:
        filter_sql += " AND s.cwd LIKE ? ESCAPE '\\'"
        filter_params.append(f"%{escape_like(path)}%")

    try:
        valid_rows = cursor.execute(filter_sql, filter_params).fetchall()
    except sqlite3.Error:
        return []

    chunk_to_branch: dict[int, int] = {row[0]: row[1] for row in valid_rows}

    # Preserve KNN distance order; exclude filtered-out chunks
    return [(cid, chunk_to_branch[cid], chunk_to_dist[cid]) for cid in chunk_ids if cid in chunk_to_branch]


def _get_vec_chunk_ids(
    cursor: sqlite3.Cursor,
    query_vec: list[float],
    top_k: int,
    projects: list[str] | None = None,
    session_id: str | None = None,
    path: str | None = None,
) -> list[tuple[int, float, int]]:
    """Return ordered (branch_id, distance, chunk_id) from chunk-vec KNN (Entrypoint A).

    Applies best-chunk-per-branch max rollup on top of _execute_chunk_knn, keeping
    the first (closest) chunk per branch in KNN order. Returns empty list on DB
    error so the caller degrades to keyword search; non-DB bugs propagate.
    """
    raw = _execute_chunk_knn(cursor, query_vec, top_k, projects, session_id, path)
    if not raw:
        return []

    seen_branches: set[int] = set()
    result: list[tuple[int, float, int]] = []
    for cid, bid, dist in raw:
        if bid in seen_branches:
            continue  # already have the best-distance chunk for this branch
        seen_branches.add(bid)
        result.append((bid, dist, cid))

    return result


def _hydrate_snippets(
    cursor: sqlite3.Cursor,
    chunk_hits: list[tuple[int, int, float]],
) -> list[dict]:
    """Hydrate Track B snippet dicts from chunk rows + branch/session/project join.

    chunk_hits is [(chunk_id, branch_id, distance)] in score order (closest first).
    Returns one snippet dict per hit preserving order.
    score_raw = 1.0 - distance (L2-normalized vectors, lower distance = better → higher score_raw = better).
    match_terms=[] and matched_role=None because the whole exchange is the vector match unit
    (no discrete term hits on the KNN path; the deferred keyword B path populates these fields).
    """
    if not chunk_hits:
        return []

    chunk_ids = [cid for cid, _bid, _dist in chunk_hits]

    placeholders = ",".join("?" * len(chunk_ids))
    rows = cursor.execute(
        f"""
        SELECT ch.id, ch.exchange_index, ch.timestamp, ch.first_message_uuid,
               ch.user_text, ch.assistant_text,
               s.uuid as session_uuid, s.git_branch, p.name as project
        FROM chunks ch
        JOIN branches b ON ch.branch_id = b.id
        JOIN sessions s ON b.session_id = s.id
        JOIN projects p ON s.project_id = p.id
        WHERE ch.id IN ({placeholders})
        """,
        chunk_ids,
    ).fetchall()

    row_map: dict[int, tuple] = {row[0]: row for row in rows}

    snippets: list[dict] = []
    for cid, _bid, dist in chunk_hits:
        row = row_map.get(cid)
        if row is None:
            continue
        (
            _,
            exchange_index,
            timestamp,
            first_message_uuid,
            user_text,
            assistant_text,
            session_uuid,
            git_branch,
            project,
        ) = row

        handle = session_uuid[:8] if session_uuid else ""
        snippets.append(
            {
                "session_uuid": session_uuid,
                "handle": handle,
                "project": project,
                "git_branch": git_branch,
                "exchange_index": exchange_index,
                "timestamp": timestamp,
                "first_message_uuid": first_message_uuid,
                "user": user_text,
                "assistant": assistant_text,
                "match_terms": [],
                "matched_role": None,
                "score_raw": 1.0 - dist,
            }
        )

    return snippets


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

    branch_to_session: dict[int, int] = {row[0]: row[1] for row in rows}

    seen_sessions: set[int] = set()
    deduped: list[int] = []
    for bid in ordered_branch_ids:
        sess = branch_to_session.get(bid)
        if sess is None:
            continue
        if sess not in seen_sessions:
            seen_sessions.add(sess)
            deduped.append(bid)
    return deduped


def _hydrate_cards(
    cursor: sqlite3.Cursor,
    branch_ids: list[int],
    branch_scores: dict[int, float] | None = None,
) -> list[dict]:
    """Build Track A session-summary card dicts for an ordered list of branch IDs.

    Reads context_summary_json (topic/disposition) and branch/session/project join
    columns. Does NOT call fetch_branch_messages — A renders from summary data only
    (no full transcript hydration, per FR#12).

    Graceful degrade (FR#11): when context_summary_json is absent, topic is
    derived from the first user message via a targeted single-row LIMIT 1 query.
    tool_counts is guarded by a PRAGMA table_info check (absent on pre-column DBs).
    score_raw is taken from branch_scores when provided (ranked path), else None.
    """
    if not branch_ids:
        return []

    # Guard tool_counts column — absent on DBs created before it was added
    cursor.execute("PRAGMA table_info(branches)")
    branch_col_names = {row[1] for row in cursor.fetchall()}
    has_tool_counts = "tool_counts" in branch_col_names
    tool_counts_col = ", b.tool_counts" if has_tool_counts else ""

    placeholders = ",".join("?" * len(branch_ids))
    rows = cursor.execute(
        f"""
        SELECT b.id as _branch_db_id, s.uuid as session_uuid,
               b.started_at, b.ended_at, b.exchange_count,
               b.files_modified, b.commits, s.git_branch,
               p.name as project, b.context_summary_json{tool_counts_col}
        FROM branches b
        JOIN sessions s ON b.session_id = s.id
        JOIN projects p ON s.project_id = p.id
        WHERE b.id IN ({placeholders})
        """,
        branch_ids,
    ).fetchall()

    branch_map: dict[int, tuple] = {row[0]: row for row in rows}

    cards: list[dict] = []
    for bid in branch_ids:
        row = branch_map.get(bid)
        if row is None:
            continue

        if has_tool_counts:
            (
                _branch_db_id,
                session_uuid,
                started_at,
                ended_at,
                exchange_count,
                files_json,
                commits_json,
                git_branch,
                project,
                summary_json,
                tool_counts_json,
            ) = row
        else:
            (
                _branch_db_id,
                session_uuid,
                started_at,
                ended_at,
                exchange_count,
                files_json,
                commits_json,
                git_branch,
                project,
                summary_json,
            ) = row
            tool_counts_json = None

        # Prefer join columns for list metadata; parse summary for topic/disposition
        files_modified: list = decode_json_column(files_json, [])
        commits: list = decode_json_column(commits_json, [])
        tool_counts: dict = decode_json_column(tool_counts_json, {}) if has_tool_counts else {}

        topic: str | None = None
        disposition: str | None = None
        summary = decode_json_column(summary_json, {})
        if summary:
            topic = summary.get("topic") or None
            disposition = summary.get("disposition") or None

        # Graceful degrade (FR#11): no context_summary_json → first user message as topic
        if not topic:
            msg_row = cursor.execute(
                """
                SELECT m.content FROM branch_messages bm
                JOIN messages m ON bm.message_id = m.id
                WHERE bm.branch_id = ? AND m.role = 'user'
                ORDER BY m.timestamp ASC LIMIT 1
                """,
                (bid,),
            ).fetchone()
            if msg_row and msg_row[0]:
                topic = msg_row[0][:200]  # truncate to keep card compact

        handle = session_uuid[:8] if session_uuid else ""
        score_raw = branch_scores.get(bid) if branch_scores else None

        cards.append(
            {
                "session_uuid": session_uuid,
                "handle": handle,
                "project": project,
                "git_branch": git_branch,
                "started_at": started_at,
                "ended_at": ended_at,
                "topic": topic,
                "disposition": disposition,
                "exchange_count": exchange_count or 0,
                "files_modified": files_modified,
                "commits": commits,
                "tool_counts": tool_counts,
                "score_raw": score_raw,
            }
        )

    return cards


def search_sessions(
    conn: sqlite3.Connection,
    query: str,
    fts_level: str | None,
    max_results: int = 5,
    projects: list[str] | None = None,
    session_id: str | None = None,
    path: str | None = None,
    keyword_only: bool = False,
) -> tuple[list[dict], bool]:
    """Search for sessions, returning (cards, ranked).

    ranked=True when FTS/vector fusion was used; ranked=False on the LIKE-only
    fallback (no relevance signal — scores are null, order is by recency).
    When model and vec extension are both available and keyword_only is False,
    results are fused from chunk-KNN and FTS via Reciprocal Rank Fusion.
    Degrades silently to keyword path on any vec/embed error.
    """
    terms = query.split()
    if not terms:
        return [], False

    cursor = conn.cursor()
    # Chunk overfetch: after best-chunk-per-branch rollup many chunks collapse to
    # fewer branches, so fetch more to ensure max_results distinct sessions remain.
    chunk_top_k = max(max_results * OVERFETCH_MULTIPLIER * CHUNK_COLLAPSE_FACTOR, OVERFETCH_FLOOR)
    # FTS overfetch: no rollup, standard window
    fts_top_k = max(max_results * OVERFETCH_MULTIPLIER, OVERFETCH_FLOOR)

    use_fusion = not keyword_only
    query_vec: list[float] | None = None
    _emit_degrade: bool = False

    # Attempt vector path only if model and vec extension are both available
    if use_fusion:
        if not model_available():
            use_fusion = False
        elif not chunk_vec_queryable(conn):
            use_fusion = False
            _emit_degrade = True

    if use_fusion:
        # Deliberately broad: embed_text wraps a third-party model stack
        # (fastembed/onnxruntime) whose failure modes aren't a fixed exception
        # type. Degrade to keyword search and announce it via _emit_degrade.
        try:
            query_vec = embed_text(query)
        except Exception:
            use_fusion = False
            _emit_degrade = True

    if use_fusion and query_vec is not None:
        try:
            fts_ids = _get_fts_branch_ids(cursor, query, fts_level, fts_top_k, projects, session_id, path)
            chunk_results = _get_vec_chunk_ids(cursor, query_vec, chunk_top_k, projects, session_id, path)
            vec_branch_ids = [r[0] for r in chunk_results]

            # Score-returning fusion — branch_id → rrf_score (higher = better)
            scored_pairs = rrf_scored([fts_ids, vec_branch_ids])
            branch_rrf_scores: dict[int, float] = {bid: score for bid, score in scored_pairs}
            scored_branch_ids = [bid for bid, _ in scored_pairs]

            deduped_ids = _dedup_by_session(cursor, scored_branch_ids)
            ordered_ids = deduped_ids[:max_results]

            # Observability: log under-fill so chronic collapse is visible
            if len(ordered_ids) < max_results and chunk_results:
                pre_rollup = len(chunk_results)
                post_rollup = len(ordered_ids)
                ratio = pre_rollup / max(post_rollup, 1)
                _logger.debug(
                    "search under-fill: %d chunks → %d sessions (collapse ratio %.1f); "
                    "consider increasing CHUNK_COLLAPSE_FACTOR",
                    pre_rollup,
                    post_rollup,
                    ratio,
                )

            # Every ordered id is a dedup-subset of the rrf-scored ids, so each is a
            # key in branch_rrf_scores — direct index yields dict[int, float].
            cards = _hydrate_cards(
                cursor,
                ordered_ids,
                branch_scores={bid: branch_rrf_scores[bid] for bid in ordered_ids},
            )
            return cards, True
        except sqlite3.Error:
            # DB-level failure in the fusion path: degrade to keyword search.
            # Bugs in fusion/hydration (rrf, dedup) propagate instead of hiding.
            _emit_degrade = True

    if _emit_degrade and not keyword_only:
        print(
            "search: vector index unavailable, using keyword search",
            file=sys.stderr,
        )

    # Keyword-only path (FTS or LIKE) — unranked, ordered by recency
    branch_ids = _get_fts_branch_ids(cursor, query, fts_level, fts_top_k, projects, session_id, path)
    deduped_ids = _dedup_by_session(cursor, branch_ids)
    ordered_ids = deduped_ids[:max_results]
    cards = _hydrate_cards(cursor, ordered_ids, branch_scores=None)
    return cards, False


def search_messages(
    conn: sqlite3.Connection,
    query: str,
    max_results: int = 5,
    projects: list[str] | None = None,
    session_id: str | None = None,
    path: str | None = None,
) -> tuple[list[dict], bool]:
    """Search for matched exchanges (Entrypoint B), returning (snippets, ranked).

    Uses chunk-KNN via _execute_chunk_knn with NO per-branch rollup — multiple
    matching chunks in one session all appear as separate snippet results (AC#3).
    ranked=False only on the pre-KNN gates (empty query, model unavailable,
    chunk_vec unavailable, or embed_text failure). Once the KNN runs, ranked=True:
    a sqlite3.Error inside _execute_chunk_knn degrades to an empty list that is
    indistinguishable from "no matches" at this layer, so both stay ranked=True.
    There is no keyword fallback for B in this landing — deferred to issue #34.
    """
    if not query.split():
        return [], False

    if not model_available():
        return [], False

    if not chunk_vec_queryable(conn):
        return [], False

    try:
        query_vec = embed_text(query)
    except Exception:  # embed_text wraps a third-party model stack (fastembed/onnxruntime)
        return [], False

    top_k = max(max_results * OVERFETCH_MULTIPLIER, OVERFETCH_FLOOR)
    cursor = conn.cursor()

    raw = _execute_chunk_knn(cursor, query_vec, top_k, projects, session_id, path)
    if not raw:
        # Either no matches or a DB error caught inside _execute_chunk_knn;
        # both cases return ranked=True (vec was available but yielded nothing).
        return [], True

    ordered = raw[:max_results]
    snippets = _hydrate_snippets(cursor, ordered)
    return snippets, True


def format_markdown(cards: list[dict], query: str, ranked: bool, verbose: bool = False) -> str:
    """Format session cards as markdown.

    verbose=True expands each card's files_modified/commits/tool_counts (FR#10);
    the JSON path always carries the full lists regardless.
    """
    if not cards:
        return f"No sessions found for query: {query}"

    normalized = apply_scores(cards, ranked)
    card_markdowns = [format_card_markdown(c, verbose=verbose) for c in normalized]
    return format_result_list_markdown(ranked, card_markdowns)


def format_messages_markdown(snippets: list[dict], query: str, ranked: bool) -> str:
    """Format matched-exchange snippets as markdown (Entrypoint B)."""
    if not snippets:
        return f"No messages found for query: {query}"

    normalized = apply_scores(snippets, ranked)
    snippet_markdowns = [format_snippet_markdown(s) for s in normalized]
    return format_result_list_markdown(ranked, snippet_markdowns)


def run_messages(
    *,
    query: str,
    max_results: int = 5,
    session: str | None = None,
    project: str | None = None,
    path: str | None = None,
    output_format: str = "markdown",
    include_notifications: bool = False,  # noqa: ARG001 — accepted for surface symmetry; moot on B (no fetch)
    db: Path = DEFAULT_DB_PATH,
) -> None:
    """Search matched exchanges (Entrypoint B — chunk-KNN without rollup).

    On a vec0-unavailable machine (or any pre-KNN failure) emits a well-formed
    empty ranked:false envelope and returns normally, so the process exits 0
    rather than erroring (FR#17/AC#14). A missing DB or an unexpected error exits 1.
    """
    max_results = max(1, min(MAX_SEARCH_RESULTS, max_results))
    projects = [p.strip() for p in project.split(",")] if project else None

    if not db.exists():
        if output_format == "json":
            print(json.dumps({"error": "Database not found", "query": query}))
        else:
            print("Error: Database not found. Run memory setup first.")
        sys.exit(1)

    settings = {"db_path": str(db)} if db != DEFAULT_DB_PATH else None

    try:
        conn = get_db_connection(settings, load_vec=True)

        snippets, ranked = search_messages(
            conn,
            query=query,
            max_results=max_results,
            projects=projects,
            session_id=session,
            path=path,
        )
        conn.close()

        if output_format == "json":
            json_snippets = [format_snippet_json(s) for s in snippets]
            envelope = build_envelope(query, ranked, json_snippets)
            print(json.dumps(envelope, indent=2))
        else:
            print(format_messages_markdown(snippets, query, ranked))

    # Deliberately broad: top-level CLI handler — reports the error and exits non-zero.
    except Exception as e:
        if output_format == "json":
            print(json.dumps({"error": str(e), "query": query}))
        else:
            print(f"Error: {e}")
        sys.exit(1)


def print_status(settings: dict | None) -> None:
    """Print diagnostic status and exit 0."""
    # Open one connection with vec loaded; chunk_vec_queryable probes whether
    # the vec table is usable — get_db_connection already loaded the extension
    # iff it could, so the table-existence probe is sufficient.
    try:
        conn = get_db_connection(settings, load_vec=True)
        is_vec = chunk_vec_queryable(conn)
    except (sqlite3.Error, OSError):
        conn = None
        is_vec = False
    print(f"vec extension: {'yes' if is_vec else 'no'}")

    # Model: name + whether the embedding stack imports. Deliberately does not
    # call model_available() — that constructs the fastembed model and would
    # download it (~120 MB) on a cold cache, which a read-only status must not do.
    print(f"model: {EMBEDDING_MODEL} (deps {'available' if DEPS_AVAILABLE else 'missing'})")

    # Chunk coverage (current-version chunks / total chunks) and branch watermark
    if conn is not None:
        try:
            total_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
            current_chunks = conn.execute(
                "SELECT count(*) FROM chunks WHERE embedding_version = ? AND embedding_model = ?",
                (EMBEDDING_VERSION, EMBEDDING_MODEL),
            ).fetchone()[0]
            print(f"chunk coverage: {current_chunks}/{total_chunks} chunks at current version")

            # Branch watermark: branches where all chunks are at current version
            total_branches = conn.execute(
                f"SELECT count(*) FROM branches WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}"
            ).fetchone()[0]
            embedded_branches = conn.execute(
                f"SELECT count(*) FROM branches WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}"
                " AND embedding_version = ? AND embedding_model = ?",
                (EMBEDDING_VERSION, EMBEDDING_MODEL),
            ).fetchone()[0]
            print(f"embedded branches: {embedded_branches}/{total_branches} (watermark)")
        except sqlite3.Error as e:
            print(f"chunk coverage: error ({e})")
            print(f"embedded branches: error ({e})")
        finally:
            conn.close()
    else:
        print("chunk coverage: error (could not open database)")
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
    include_notifications: bool = False,  # noqa: ARG001 — accepted for CLI compat; A-path has no transcript hydration
    db: Path = DEFAULT_DB_PATH,
) -> None:
    """Search conversation sessions (keyword + chunk-vector fusion)."""
    # Validate: exactly one of --query / --status must be provided.
    if not status and not query:
        print("error: one of --query/-q or --status is required", file=sys.stderr)
        sys.exit(2)
    if status and query:
        print("error: --query and --status are mutually exclusive", file=sys.stderr)
        sys.exit(2)

    # Backstop for direct callers; the CLI validator rejects out-of-range
    # --max-results before reaching here. Both sides bound on MAX_SEARCH_RESULTS.
    max_results = max(1, min(MAX_SEARCH_RESULTS, max_results))
    projects = [p.strip() for p in project.split(",")] if project else None

    if not db.exists():
        if status:
            # For --status, report missing DB gracefully rather than hard-exiting.
            # Model identity is independent of the DB, so report it even when the
            # DB is absent (deps check only — no download in a read-only path).
            print("vec extension: no")
            print(f"model: {EMBEDDING_MODEL} (deps {'available' if DEPS_AVAILABLE else 'missing'})")
            print("chunk coverage: error (database not found)")
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

        cards, ranked = search_sessions(
            conn,
            query=query,
            fts_level=fts_level,
            max_results=max_results,
            projects=projects,
            session_id=session,
            path=path,
            keyword_only=keyword_only,
        )
        conn.close()

        if output_format == "json":
            json_cards = [format_card_json(c) for c in cards]
            envelope = build_envelope(query, ranked, json_cards)
            print(json.dumps(envelope, indent=2))
        else:
            print(format_markdown(cards, query, ranked, verbose=verbose))

    # Deliberately broad: top-level CLI handler — reports any error to the user
    # and exits non-zero rather than dumping a traceback.
    except Exception as e:
        if output_format == "json":
            print(json.dumps({"error": str(e), "sessions": [], "query": query}))
        else:
            print(f"Error: {e}")
        sys.exit(1)
