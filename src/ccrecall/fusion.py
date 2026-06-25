"""Reciprocal Rank Fusion — pure function, no model dependency."""

# At small top-K its exact value barely matters; 60 is the standard default.
RRF_K = 60


def rrf_score_dict(ranked_lists: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    """Compute raw RRF scores for all ids, returning a {id: score} mapping.

    Shared building block for rrf() (ids-only) and rrf_scored() (id+score pairs).
    Higher score = better ranked.
    """
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, id_ in enumerate(ranked):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (k + rank + 1)
    return scores


def rrf(ranked_lists: list[list[int]], k: int = RRF_K) -> list[int]:
    """Standard Reciprocal Rank Fusion over any number of ranked id-lists.

    Returns fused ids in descending score order. Handles empty lists and
    fully disjoint lists without error.
    """
    scores = rrf_score_dict(ranked_lists, k)
    return sorted(scores, key=lambda id_: scores[id_], reverse=True)


def rrf_scored(ranked_lists: list[list[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Score-returning Reciprocal Rank Fusion over any number of ranked id-lists.

    Returns (id, score) pairs in descending score order, where score is the raw
    fused RRF value (higher is better). This is the card's score_raw.

    The presented score field (normalized to [0,1] within the bounded result set)
    is computed at render time by normalize_scores() in formatting.py — not here.
    """
    scores = rrf_score_dict(ranked_lists, k)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)
