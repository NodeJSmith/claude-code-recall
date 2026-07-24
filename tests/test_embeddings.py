"""Tests for the shared embedding module."""

import math
from unittest.mock import MagicMock

import pytest

from ccrecall.embeddings import (
    DEFAULT_EMBED_THREADS,
    EMBED_CHAR_BUDGET,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    EMBEDDING_VERSION,
    cap_for_embedding,
    embed_batch,
    embed_text,
    embed_texts,
    model_available,
    resolve_thread_count,
)


class TestResolveThreadCount:
    def test_default_when_none(self):
        assert resolve_thread_count(None) == DEFAULT_EMBED_THREADS

    def test_passthrough(self):
        assert resolve_thread_count(4) == 4

    def test_floor_at_one(self):
        assert resolve_thread_count(0) == 1

    def test_negative_floored(self):
        assert resolve_thread_count(-3) == 1


class TestModelAvailable:
    def test_deps_unavailable(self, monkeypatch):
        """model_available returns False when fastembed isn't importable."""
        monkeypatch.setattr("ccrecall.embeddings.DEPS_AVAILABLE", False)
        monkeypatch.setattr("ccrecall.embeddings._model", None)
        assert model_available() is False

    def test_construction_failure_returns_false(self, monkeypatch):
        """model_available returns False (no raise) when the model can't construct."""

        def boom(*args, **kwargs):
            raise RuntimeError("download failed")

        monkeypatch.setattr("ccrecall.embeddings.DEPS_AVAILABLE", True)
        monkeypatch.setattr("ccrecall.embeddings.TextEmbedding", boom)
        monkeypatch.setattr("ccrecall.embeddings._model", None)
        assert model_available() is False

    def test_no_raise_on_error(self, monkeypatch):
        """model_available never propagates; any failure becomes False."""

        def boom(*args, **kwargs):
            raise OSError("native lib missing")

        monkeypatch.setattr("ccrecall.embeddings.DEPS_AVAILABLE", True)
        monkeypatch.setattr("ccrecall.embeddings.TextEmbedding", boom)
        monkeypatch.setattr("ccrecall.embeddings._model", None)
        # Must not raise
        assert model_available() is False


class TestConstants:
    def test_embedding_constants(self):
        """Version is 3 (per-exchange chunk granularity), dim is 512, model name is jina."""
        assert EMBEDDING_VERSION == 3
        assert EMBEDDING_DIM == 512
        assert EMBEDDING_MODEL == "jinaai/jina-embeddings-v2-small-en"


class TestCapForEmbedding:
    """Tests for cap_for_embedding — the token-aware head+tail cap."""

    def _make_model(self, *token_counts):
        """Return a mock model whose token_count returns the given values in sequence."""
        mock = MagicMock()
        mock.token_count.side_effect = list(token_counts)
        return mock

    def test_short_text_passes_through_unchanged(self, monkeypatch):
        """Text within char budget and token limit is returned unchanged (was_capped=False)."""
        text = "hello world"
        mock = self._make_model(5)  # well within MODEL_TOKEN_LIMIT
        monkeypatch.setattr("ccrecall.embeddings._model", mock)

        result, was_capped = cap_for_embedding(text)

        assert result == text
        assert was_capped is False

    def test_over_char_budget_head_tail_capped(self, monkeypatch):
        """Text exceeding EMBED_CHAR_BUDGET is head+tail-capped; both ends present, middle dropped."""
        text = "A" * (EMBED_CHAR_BUDGET // 2) + "MIDDLE" + "Z" * (EMBED_CHAR_BUDGET // 2)
        # After char cap, token count is fine — loop exits immediately
        mock = self._make_model(100)  # within MODEL_TOKEN_LIMIT, for the while-loop check
        monkeypatch.setattr("ccrecall.embeddings._model", mock)

        result, was_capped = cap_for_embedding(text)

        assert was_capped is True
        # Head portion present
        assert result.startswith("A" * (EMBED_CHAR_BUDGET // 2))
        # Tail portion present
        assert result.endswith("Z" * (EMBED_CHAR_BUDGET // 2))
        # Middle dropped — separator in result
        assert "\n\n[...]\n\n" in result
        # "MIDDLE" marker is gone (it lived in the middle of the original text)
        assert "MIDDLE" not in result

    def test_dense_content_tightened_until_token_limit_fits(self, monkeypatch):
        """Dense text (under char budget but over token limit) is tightened until it fits.

        This exercises the cap mechanism that prevents CONTENT_ERROR on dense exchanges.
        The capped form still carries head and tail signal and produces a usable vector.
        """
        # Text is short enough to pass the char budget check but mocked to be over
        # the token limit until the loop BODY shrinks the head/tail at least once.
        text = "x" * 1000  # well under EMBED_CHAR_BUDGET

        # Call sequence for token_count (three responses force the loop body to run):
        #   1st: fast-path check on full text → 9000 (over limit → proceed to cap)
        #   2nd: while-loop check on the initial cap → 9500 (STILL over → body runs,
        #        shrinking head/tail from 400 to 300 each)
        #   3rd: while-loop check on the tightened cap → 3000 (under limit → exit)
        mock = self._make_model(9000, 9500, 3000)
        monkeypatch.setattr("ccrecall.embeddings._model", mock)

        # Initial cap (before any loop-body shrink): head=tail=2*1000//5=400.
        initial_cap = text[:400] + "\n\n[...]\n\n" + text[-400:]

        result, was_capped = cap_for_embedding(text)

        assert was_capped is True
        # Head and tail of original text are present (tail never dropped)
        assert result.startswith(text[:1])
        assert result.endswith(text[-1:])
        # Separator marks the dropped middle
        assert "\n\n[...]\n\n" in result
        # The loop body actually ran: result is shorter than the *initial* cap,
        # proving head/tail were tightened (not merely the first cap returned).
        assert len(result) < len(initial_cap)
        # token_count was called all three times (fast-path + two loop checks)
        assert mock.token_count.call_count == 3

    def test_empty_string_passthrough(self, monkeypatch):
        """Empty string returns (empty_string, False) — no model call needed."""
        mock = self._make_model()
        monkeypatch.setattr("ccrecall.embeddings._model", mock)

        result, was_capped = cap_for_embedding("")

        assert result == ""
        assert was_capped is False
        mock.token_count.assert_not_called()


@pytest.mark.skipif(not model_available(), reason="fastembed model unavailable (jina-v2-small-en)")
class TestEmbedRealModel:
    """Real-model tests — skipped when the model is unavailable."""

    def test_determinism(self):
        """Same input text produces identical vector on two calls."""
        text = "hello world"
        assert embed_text(text) == embed_text(text)

    def test_dim(self):
        """embed_text returns a list of EMBEDDING_DIM floats."""
        v = embed_text("testing embedding dimension")
        assert len(v) == EMBEDDING_DIM
        assert all(isinstance(x, float) for x in v)

    def test_normalized(self):
        """embed_text returns an L2-normalized vector (magnitude ≈ 1.0)."""
        v = embed_text("normalization check")
        magnitude = math.sqrt(sum(x * x for x in v))
        assert abs(magnitude - 1.0) < 1e-4

    def test_batch(self):
        """embed_texts returns one vector per input, each EMBEDDING_DIM and normalized."""
        texts = ["first text", "second text", "third text"]
        results = embed_texts(texts)
        assert len(results) == len(texts)
        for v in results:
            assert len(v) == EMBEDDING_DIM
            magnitude = math.sqrt(sum(x * x for x in v))
            assert abs(magnitude - 1.0) < 1e-4

    def test_batch_matches_single(self):
        """embed_texts produces the same vectors as calling embed_text individually."""
        texts = ["alpha", "beta"]
        assert embed_texts(texts) == [embed_text(t) for t in texts]

    def test_different_texts_differ(self):
        """Different texts produce different vectors."""
        assert embed_text("cat") != embed_text("quantum mechanics")

    def test_embed_batch_matches_single(self):
        """embed_batch produces the same vectors as calling embed_text individually."""
        texts = ["alpha", "beta", "gamma"]
        batch_results = embed_batch(texts)
        single_results = [embed_text(t) for t in texts]
        assert batch_results == single_results

    def test_embed_batch_empty(self):
        """embed_batch with empty list returns empty list."""
        assert embed_batch([]) == []
