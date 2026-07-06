"""Chunk-vector KNN execution and snippet hydration for conversation search."""

import sqlite3

import sqlite_vec

from ccrecall.embeddings import EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.search_query import scope_filter_clause


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
    Filters to current embedding version + model at the chunk grain, so
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

    scope_sql, scope_params = scope_filter_clause(projects=projects, session_id=session_id, path=path)
    filter_sql += scope_sql
    filter_params.extend(scope_params)

    try:
        valid_rows = cursor.execute(filter_sql, filter_params).fetchall()
    except sqlite3.Error:
        return []

    chunk_to_branch: dict[int, int] = {row[0]: row[1] for row in valid_rows}

    # Preserve KNN distance order; exclude filtered-out chunks
    return [(cid, chunk_to_branch[cid], chunk_to_dist[cid]) for cid in chunk_ids if cid in chunk_to_branch]


def get_vec_chunk_ids(
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


def hydrate_snippets(
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
