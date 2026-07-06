"""Search orchestrators: FTS5/FTS4/LIKE fused with chunk-KNN via RRF.

Returns cards (Entrypoint A, session-level) or snippets (Entrypoint B,
exchange-level) for the CLI layer (search_cli.py) to format and print.
"""

import logging
import sqlite3

from ccrecall.db import branch_embedding_coverage, chunk_vec_queryable
from ccrecall.embeddings import embed_text, model_available
from ccrecall.fusion import rrf_scored
from ccrecall.health import RECALL_CAVEAT_COVERAGE_THRESHOLD
from ccrecall.search_hydrate import dedup_by_session, hydrate_cards
from ccrecall.search_query import get_fts_branch_ids
from ccrecall.search_vector import execute_chunk_knn, get_vec_chunk_ids, hydrate_snippets

_logger = logging.getLogger("ccrecall")

# Each ranker (FTS, vector KNN) is over-fetched before fusion + per-session dedup,
# so the post-filter top-N still has enough candidates: top_k = max(N * mult, floor).
OVERFETCH_MULTIPLIER = 4
OVERFETCH_FLOOR = 20

# Chunk overfetch factor for A: chunk-KNN returns many chunks per branch, so the
# post-best-chunk-per-branch rollup may collapse N chunks to far fewer branches.
# Multiply the overfetch by this factor so the post-rollup session count fills
# max_results. Start at 8 (generous chunks-per-session estimate).
CHUNK_COLLAPSE_FACTOR = 8


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

    ranked=True when a relevance signal exists: RRF fusion (chunk-KNN + FTS), or
    the fts5/BM25 keyword rung on the degraded path. ranked=False on the LIKE rung
    (unranked, recency order) and — as a deferred gap (issue #35) — the fts4
    rung, which is recency-ordered rather than matchinfo-ranked in this landing.
    When model and vec extension are both available and keyword_only is False,
    results are fused from chunk-KNN and FTS via Reciprocal Rank Fusion. Degrades
    silently to the keyword path on any vec/embed error.
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

    # Attempt vector path only if model and vec extension are both available
    if use_fusion:
        if not model_available() or not chunk_vec_queryable(conn):
            use_fusion = False

    if use_fusion:
        # Deliberately broad: embed_text wraps a third-party model stack
        # (fastembed/onnxruntime) whose failure modes aren't a fixed exception
        # type. Degrade to keyword search; compute_caveat in run() surfaces the
        # degradation to the user.
        try:
            query_vec = embed_text(query)
        except Exception:
            use_fusion = False

    if use_fusion and query_vec is not None:
        try:
            fts_ids = [
                bid
                for bid, _score in get_fts_branch_ids(cursor, query, fts_level, fts_top_k, projects, session_id, path)
            ]
            chunk_results = get_vec_chunk_ids(cursor, query_vec, chunk_top_k, projects, session_id, path)
            vec_branch_ids = [r[0] for r in chunk_results]

            # Score-returning fusion — branch_id → rrf_score (higher = better)
            scored_pairs = rrf_scored([fts_ids, vec_branch_ids])
            branch_rrf_scores: dict[int, float] = {bid: score for bid, score in scored_pairs}
            scored_branch_ids = [bid for bid, _ in scored_pairs]

            deduped_ids = dedup_by_session(cursor, scored_branch_ids)
            ordered_ids = deduped_ids[:max_results]

            # Observability: log under-fill so chronic collapse is visible
            if len(ordered_ids) < max_results and chunk_results:
                pre_rollup = len(chunk_results)
                post_rollup = len(ordered_ids)
                ratio = pre_rollup / max(post_rollup, 1)
                _logger.info(
                    "search under-fill: %d chunks → %d sessions (collapse ratio %.1f); "
                    "consider increasing CHUNK_COLLAPSE_FACTOR",
                    pre_rollup,
                    post_rollup,
                    ratio,
                )

            # Every ordered id is a dedup-subset of the rrf-scored ids, so each is a
            # key in branch_rrf_scores — direct index yields dict[int, float].
            cards = hydrate_cards(
                cursor,
                ordered_ids,
                branch_scores={bid: branch_rrf_scores[bid] for bid in ordered_ids},
            )
            return cards, True
        except sqlite3.Error:
            # DB-level failure in the fusion path: degrade to keyword search
            # (falls through to the keyword path below; compute_caveat surfaces it).
            # Bugs in fusion/hydration (rrf, dedup) propagate instead of hiding.
            use_fusion = False

    # Keyword-only path. The fts5 rung ranks by BM25 (a relevance signal →
    # ranked:true with scores). The LIKE fallback is unranked by the contract
    # (null scores, recency order). The fts4 rung is recency-ordered with no
    # relevance score in this landing, so it is also surfaced as unranked — a
    # deferred Track A gap (issue #35), not the contract's end state.
    fts_rows = get_fts_branch_ids(cursor, query, fts_level, fts_top_k, projects, session_id, path)
    ranked = fts_level == "fts5" and bool(fts_rows)
    branch_scores = {bid: score for bid, score in fts_rows if score is not None} if ranked else None
    deduped_ids = dedup_by_session(cursor, [bid for bid, _score in fts_rows])
    ordered_ids = deduped_ids[:max_results]
    cards = hydrate_cards(cursor, ordered_ids, branch_scores=branch_scores)
    return cards, ranked


def search_messages(
    conn: sqlite3.Connection,
    query: str,
    max_results: int = 5,
    projects: list[str] | None = None,
    session_id: str | None = None,
    path: str | None = None,
) -> tuple[list[dict], bool]:
    """Search for matched exchanges (Entrypoint B), returning (snippets, ranked).

    Uses chunk-KNN via execute_chunk_knn with NO per-branch rollup — multiple
    matching chunks in one session all appear as separate snippet results.
    ranked=False only on the pre-KNN gates (empty query, model unavailable,
    chunk_vec unavailable, or embed_text failure). Once the KNN runs, ranked=True:
    a sqlite3.Error inside execute_chunk_knn degrades to an empty list that is
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

    raw = execute_chunk_knn(cursor, query_vec, top_k, projects, session_id, path)
    if not raw:
        # Either no matches or a DB error caught inside execute_chunk_knn;
        # both cases return ranked=True (vec was available but yielded nothing).
        return [], True

    ordered = raw[:max_results]
    snippets = hydrate_snippets(cursor, ordered)
    return snippets, True


def compute_caveat(conn: sqlite3.Connection) -> str | None:
    """Return a one-line recall caveat or None when results are unimpaired.

    Returns a caveat string when embeddings are unavailable (results degraded to
    keyword-only) or branch coverage is below RECALL_CAVEAT_COVERAGE_THRESHOLD.
    Returns None at/above threshold on a healthy install, or when total == 0
    (nothing embedded yet — not a degradation). A failure computing coverage
    degrades to None so a broken probe never breaks the recall itself.
    """
    try:
        if not chunk_vec_queryable(conn):
            return "embeddings unavailable — keyword-only results"
        embedded, total = branch_embedding_coverage(conn)
        if total > 0 and embedded / total < RECALL_CAVEAT_COVERAGE_THRESHOLD:
            pct = int(100 * embedded / total)
            return f"{pct}% of history embedded; results may be partial"
        return None
    except Exception:
        _logger.exception("caveat computation failed")
        return None
