"""Tests for the pure RRF fusion function."""

from ccrecall.fusion import rrf_scored


def _ids(result: list[tuple[int, float]]) -> list[int]:
    return [id_ for id_, _ in result]


class TestRrfScored:
    def test_returns_id_score_tuples(self):
        """rrf_scored returns a list of (id, score) tuples."""
        result = rrf_scored([[1, 2, 3], [1, 4, 5]])
        assert len(result) > 0
        assert isinstance(result[0], tuple)
        assert len(result[0]) == 2

    def test_descending_score_order(self):
        """Result is ordered by descending score."""
        result = rrf_scored([[1, 2], [1, 3]])
        scores = [s for _, s in result]
        assert scores == sorted(scores, reverse=True)

    def test_shared_id_scores_higher(self):
        """An id appearing in two lists has a higher score than ids in one list."""
        result = rrf_scored([[1, 2], [1, 3]])
        score_by_id = dict(result)
        assert score_by_id[1] > score_by_id[2]
        assert score_by_id[1] > score_by_id[3]

    def test_empty_ranked_lists(self):
        """Empty ranked_lists returns empty list."""
        assert rrf_scored([]) == []

    def test_single_empty_list(self):
        """A list containing one empty list returns empty."""
        assert rrf_scored([[]]) == []

    def test_disjoint_lists_all_ids_present(self):
        """Disjoint lists produce all ids in the result."""
        result = rrf_scored([[1], [2], [3]])
        ids = {id_ for id_, _ in result}
        assert ids == {1, 2, 3}

    def test_disjoint_equal_scores(self):
        """Ids at the same rank in disjoint lists have identical scores."""
        result = rrf_scored([[1], [2]])
        scores = [s for _, s in result]
        assert scores[0] == scores[1]

    def test_scores_are_floats(self):
        """All scores are float values."""
        result = rrf_scored([[1, 2, 3]])
        for _, score in result:
            assert isinstance(score, float)

    def test_custom_k(self):
        """Custom k parameter is respected in score calculation."""
        result_60 = rrf_scored([[1, 2]], k=60)
        result_10 = rrf_scored([[1, 2]], k=10)
        assert _ids(result_60) == _ids(result_10)
        score_60 = dict(result_60)[1]
        score_10 = dict(result_10)[1]
        assert score_10 > score_60

    def test_basic_merge(self):
        """A shared id ranks above singletons from a single list."""
        result = rrf_scored([[1, 2, 3], [1, 4, 5]])
        assert _ids(result)[0] == 1

    def test_mixed_empty(self):
        """Empty list mixed with non-empty list is handled gracefully."""
        assert _ids(rrf_scored([[], [1, 2, 3]])) == [1, 2, 3]

    def test_single_list_preserves_order(self):
        """Single ranked list preserves original order."""
        assert _ids(rrf_scored([[5, 3, 1]])) == [5, 3, 1]

    def test_deterministic(self):
        """Same input always produces same output."""
        lists = [[1, 2, 3], [2, 3, 4], [3, 4, 5]]
        assert rrf_scored(lists) == rrf_scored(lists)

    def test_shared_id_outranks_singletons(self):
        """Id shared across more lists ranks above ids in fewer lists."""
        result = rrf_scored([[10, 20], [10, 30], [10, 40]])
        assert _ids(result)[0] == 10
