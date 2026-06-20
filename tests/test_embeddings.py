"""Tests for the shared embedding module."""

import math
import pytest

from ccrecall.embeddings import (
    DEFAULT_EMBED_THREADS,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    EMBEDDING_VERSION,
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
        """Version is 2 (post bge-m3 swap), dim is 512, model name is jina."""
        assert EMBEDDING_VERSION == 2
        assert EMBEDDING_DIM == 512
        assert EMBEDDING_MODEL == "jinaai/jina-embeddings-v2-small-en"


@pytest.mark.skipif(not model_available(), reason="fastembed model unavailable (jina-v2-small-en)")
class TestEmbedRealModel:
    """Real-model tests — skipped when the model is unavailable."""

    def test_determinism(self):
        """Same input text produces identical vector on two calls (AC#6)."""
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
