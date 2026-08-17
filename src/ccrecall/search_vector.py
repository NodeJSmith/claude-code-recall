"""Chunk-vector KNN execution and snippet hydration for conversation search."""

import logging
import sqlite3

import sqlite_vec

from ccrecall.embeddings import EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.models import LOGGER_NAME
from ccrecall.search_query import EMPTY_SCOPE, ScopeFilter, scope_filter_clause

log = logging.getLogger(LOGGER_NAME)

KNN_RETRY_MULTIPLIER = 4
MAX_SQL_BOUND_PARAMS = 900
FILTER_CHUNK_ID_BATCH_SIZE = 500


def _run_chunk_knn(cursor: sqlite3.Cursor, serialized: bytes, k: int) -> list[tuple[int, float]]:
    return cursor.execute(
        "SELECT chunk_id, distance FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (serialized, k),
    ).fetchall()


def _scoped_current_chunk_filters(scope: ScopeFilter) -> tuple[str, str, list]:
    joins_sql = """
        JOIN branches b ON ch.branch_id = b.id
        JOIN sessions s ON b.session_id = s.id
        JOIN projects p ON s.project_id = p.id
    """
    where_sql = """
        AND ch.embedding_version = ?
        AND ch.embedding_model = ?
        AND b.is_active = 1
    """
    params: list = [EMBEDDING_VERSION, EMBEDDING_MODEL]
    scope_sql, scope_filter_params = scope_filter_clause(scope)
    where_sql += scope_sql
    params.extend(scope_filter_params)
    return joins_sql, where_sql, params


def _filter_chunk_knn_rows(
    cursor: sqlite3.Cursor,
    knn_rows: list[tuple[int, float]],
    scope: ScopeFilter,
) -> list[tuple[int, int, float]]:
    if not knn_rows:
        return []

    chunk_ids = [row[0] for row in knn_rows]
    chunk_to_dist: dict[int, float] = {row[0]: row[1] for row in knn_rows}
    joins_sql, where_sql, filter_tail_params = _scoped_current_chunk_filters(scope)

    chunk_to_branch: dict[int, int] = {}
    param_budget = MAX_SQL_BOUND_PARAMS - len(filter_tail_params)
    if param_budget <= 0:
        return []

    batch_size = min(FILTER_CHUNK_ID_BATCH_SIZE, param_budget)
    for offset in range(0, len(chunk_ids), batch_size):
        batch_ids = chunk_ids[offset : offset + batch_size]
        placeholders = ",".join("?" * len(batch_ids))
        filter_sql = (
            f"SELECT ch.id as chunk_id, b.id as branch_id FROM chunks ch {joins_sql}"
            f"WHERE ch.id IN ({placeholders}) {where_sql}"
        )
        filter_params: list = [*batch_ids, *filter_tail_params]
        valid_rows = cursor.execute(filter_sql, filter_params).fetchall()
        chunk_to_branch.update((row[0], row[1]) for row in valid_rows)

    return [(cid, chunk_to_branch[cid], chunk_to_dist[cid]) for cid in chunk_ids if cid in chunk_to_branch]


def _count_total_vec_candidates(cursor: sqlite3.Cursor) -> int:
    row = cursor.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()
    return row[0] if row is not None else 0


def _count_eligible_scoped_current_chunks(
    cursor: sqlite3.Cursor,
    scope: ScopeFilter,
) -> int:
    joins_sql, where_sql, params = _scoped_current_chunk_filters(scope)
    sql = f"SELECT COUNT(*) FROM chunks ch JOIN chunk_vec cv ON cv.chunk_id = ch.id {joins_sql} WHERE 1=1 {where_sql}"
    row = cursor.execute(sql, params).fetchone()
    return row[0] if row is not None else 0


def execute_chunk_knn(
    cursor: sqlite3.Cursor,
    query_vec: list[float],
    top_k: int,
    scope: ScopeFilter = EMPTY_SCOPE,
    target_results: int | None = None,
) -> list[tuple[int, int, float]]:
    """Shared chunk-KNN core: adaptively retry vec MATCH until filtered results fill the target.

    Returns [(chunk_id, branch_id, distance)] without any per-branch rollup —
    both A's rollup and B's no-rollup path build on this single MATCH query.
    When target_results is provided, retry growth continues until that many
    scoped/current chunks are recovered or the global vector corpus is exhausted.
    Filters to current embedding version + model at the chunk grain, so
    a partially re-embedded branch still contributes its already-current chunks.
    Returns empty list on sqlite3.Error so callers can degrade; non-DB bugs propagate.
    """
    requested_results = target_results if target_results is not None else top_k
    if top_k <= 0 or requested_results <= 0:
        return []

    try:
        serialized = sqlite_vec.serialize_float32(query_vec)
    except sqlite3.Error:
        log.exception("vec serialize failed")
        return []

    total_candidates: int | None = None
    eligible_candidates: int | None = None
    current_k = top_k

    while True:
        try:
            knn_rows = _run_chunk_knn(cursor, serialized, current_k)
        except sqlite3.Error:
            log.exception("chunk KNN query failed")
            return []

        if not knn_rows:
            return []

        try:
            filtered_rows = _filter_chunk_knn_rows(cursor, knn_rows, scope)
        except sqlite3.Error:
            log.exception("chunk KNN filter failed")
            return []

        if len(filtered_rows) >= requested_results:
            return filtered_rows[:requested_results]

        if eligible_candidates is None:
            try:
                eligible_candidates = _count_eligible_scoped_current_chunks(cursor, scope)
            except sqlite3.Error:
                log.exception("eligible chunk count query failed")
                return []

        if eligible_candidates == 0:
            return []

        max_possible_results = min(requested_results, eligible_candidates)
        if len(filtered_rows) >= max_possible_results:
            return filtered_rows[:max_possible_results]

        vec_returned_less_than_requested = len(knn_rows) < current_k
        if vec_returned_less_than_requested:
            return filtered_rows[:max_possible_results]

        if total_candidates is None:
            try:
                total_candidates = _count_total_vec_candidates(cursor)
            except sqlite3.Error:
                log.exception("total vec candidate count query failed")
                return []

        reached_global_corpus_ceiling = total_candidates == 0 or current_k >= total_candidates
        if reached_global_corpus_ceiling:
            return filtered_rows[:max_possible_results]

        next_k = min(current_k * KNN_RETRY_MULTIPLIER, total_candidates)
        retry_window_cannot_grow = next_k == current_k
        if retry_window_cannot_grow:
            return filtered_rows[:max_possible_results]
        current_k = next_k


def get_vec_chunk_ids(
    cursor: sqlite3.Cursor,
    query_vec: list[float],
    top_k: int,
    scope: ScopeFilter = EMPTY_SCOPE,
) -> list[tuple[int, float, int]]:
    """Return ordered (branch_id, distance, chunk_id) from chunk-vec KNN (Entrypoint A).

    Applies best-chunk-per-branch max rollup on top of execute_chunk_knn, keeping
    the first (closest) chunk per branch in KNN order. Returns empty list on DB
    error so the caller degrades to keyword search; non-DB bugs propagate.
    """
    raw = execute_chunk_knn(cursor, query_vec, top_k, scope=scope)
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
