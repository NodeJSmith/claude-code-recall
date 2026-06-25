"""Tests for the pure RRF fusion function."""

from ccrecall.fusion import RRF_K, rrf, rrf_scored


class TestRrf:
    def test_basic_merge(self):
        """A shared id ranks above singletons from a single list."""
        # id 1 appears in both lists (rank 0+0), should outrank ids only in one
        result = rrf([[1, 2, 3], [1, 4, 5]])
        assert result[0] == 1  # id 1 is top because it appears in both

    def test_disjoint_lists(self):
        """Fully disjoint lists return all ids with no error."""
        result = rrf([[1, 2], [3, 4]])
        assert set(result) == {1, 2, 3, 4}
        assert len(result) == 4

    def test_empty_list(self):
        """An empty ranked_lists returns an empty result."""
        assert rrf([]) == []

    def test_single_empty_ranked_list(self):
        """A list containing one empty list returns an empty result."""
        assert rrf([[]]) == []

    def test_mixed_empty(self):
        """Empty list mixed with non-empty list is handled gracefully."""
        assert rrf([[], [1, 2, 3]]) == [1, 2, 3]

    def test_deterministic(self):
        """Same input always produces same output."""
        lists = [[1, 2, 3], [2, 3, 4], [3, 4, 5]]
        assert rrf(lists) == rrf(lists)

    def test_shared_id_outranks_singletons(self):
        """Id shared across more lists should rank above ids in fewer lists."""
        # id 10 appears in 3 lists at rank 0 each; id 20 appears only once
        result = rrf([[10, 20], [10, 30], [10, 40]])
        assert result[0] == 10

    def test_additive_score_multi_list_id_ranks_first(self):
        """An id in two lists (additive score 2/(k+1)) outranks an id in one (1/(k+1))."""
        # id 2 appears in both lists at rank 0; id 1 and id 3 each in one list.
        result = rrf([[2, 1], [2, 3]], k=RRF_K)
        assert result[0] == 2

    def test_custom_k(self):
        """Custom k parameter is respected."""
        result = rrf([[1, 2], [2, 3]], k=10)
        # id 2 appears in both lists → should be first
        assert result[0] == 2

    def test_single_list(self):
        """Single ranked list preserves original order."""
        assert rrf([[5, 3, 1]]) == [5, 3, 1]

    def test_returns_all_ids(self):
        """Result contains every id that appeared in any list."""
        lists = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        result = rrf(lists)
        assert set(result) == {1, 2, 3, 4, 5, 6, 7, 8, 9}


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

    def test_same_id_order_as_rrf(self):
        """rrf_scored produces the same id ordering as rrf."""
        lists = [[1, 2, 3], [2, 3, 4], [3, 4, 5]]
        ids_only = rrf(lists)
        ids_from_scored = [id_ for id_, _ in rrf_scored(lists)]
        assert ids_only == ids_from_scored

    def test_custom_k(self):
        """Custom k parameter is respected in score calculation."""
        result_60 = rrf_scored([[1, 2]], k=60)
        result_10 = rrf_scored([[1, 2]], k=10)
        # Both should have the same order but different absolute scores
        assert [id_ for id_, _ in result_60] == [id_ for id_, _ in result_10]
        # Score with k=10 should be higher than with k=60 (smaller denominator)
        score_60 = dict(result_60)[1]
        score_10 = dict(result_10)[1]
        assert score_10 > score_60

    def test_rrf_unchanged(self):
        """rrf still returns ids-only (unchanged behavior)."""
        result = rrf([[1, 2], [2, 3]])
        assert all(isinstance(x, int) for x in result)
