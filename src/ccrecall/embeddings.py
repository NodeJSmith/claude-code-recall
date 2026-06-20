"""Shared embedding module — the single source of truth for vectors.

Both the write path and the query path must import from here. No second
embedding code path may exist.
"""

import numpy as np

# fastembed is a hard dep, but guard the import so model_available() can degrade
# on a machine where the wheel won't import (ABI mismatch, missing native lib)
# instead of raising at import time.
try:
    from fastembed import TextEmbedding
except (ImportError, OSError):
    # OSError too: a native wheel that imports but can't load its shared library
    # (ABI mismatch, missing system lib) raises OSError, not ImportError — catch
    # both so import-time degrades instead of crashing.
    TextEmbedding = None

DEPS_AVAILABLE = TextEmbedding is not None

EMBEDDING_MODEL = "jinaai/jina-embeddings-v2-small-en"
EMBEDDING_VERSION = 2  # Bumped from 1 (bge-m3): different model and vector space.
EMBEDDING_DIM = 512

# fastembed defaults its inference parallelism to every CPU core, so each
# single inference briefly saturates the whole machine. Embedding here is always
# a single short text (write/query/backfill all call embed_one per text), so a
# low thread count costs interactive paths almost nothing while keeping the
# opt-in backfill — which can run ~1.9k active-leaf inferences in one go — from
# thrashing constrained machines. The backfill exposes `--threads` to raise this
# on an idle machine; interactive write/query paths always use the default.
DEFAULT_EMBED_THREADS = 1

# Module-level singleton — lazily constructed, reused within a process.
_model = None


def resolve_thread_count(threads: int | None) -> int:
    """Clamp a requested inference thread count to >= 1, defaulting when None."""
    if threads is None:
        return DEFAULT_EMBED_THREADS
    return max(1, threads)


def get_model(threads: int | None = None):
    """Return the cached fastembed model, constructing on first call.

    ``threads`` caps the model's inference parallelism (passed through to
    fastembed), applied only when the model is first constructed and ignored
    once it is cached. None means
    DEFAULT_EMBED_THREADS. The first construction downloads the model (~120 MB)
    into the fastembed cache if it isn't already present. Raises on failure —
    callers wrap in their own guard.
    """
    global _model
    if _model is not None:
        return _model

    if not DEPS_AVAILABLE:
        raise RuntimeError("fastembed not importable")

    # DEPS_AVAILABLE is True only when the fastembed import bound TextEmbedding; the
    # assert restates that invariant so the type checker sees a non-None constructor.
    assert TextEmbedding is not None
    _model = TextEmbedding(model_name=EMBEDDING_MODEL, threads=resolve_thread_count(threads))
    return _model


def model_available(threads: int | None = None) -> bool:
    """Return True iff the embedding model can be loaded and run.

    Constructs (and caches) the fastembed model, downloading it on first call
    if not already cached. Pass ``threads`` to set the thread count when this
    call is the one that warms the singleton. Never raises — returns False on
    any failure (deps missing, download failure, ABI mismatch).
    """
    if not DEPS_AVAILABLE:
        return False
    try:
        get_model(threads)
        return True
    except Exception:
        return False


def normalize(vec: np.ndarray) -> np.ndarray:
    """L2-normalize a 1-D float array (no-op for a zero/degenerate norm)."""
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        return vec / norm
    return vec


def embed_one(model, text: str) -> list[float]:
    """Embed one text with an already-constructed model.

    fastembed already L2-normalizes its output, but we normalize again so the
    unit-vector invariant lives here regardless of any upstream default change.
    """
    # model.embed returns a generator yielding one vector per input; pull the
    # single result for our one-element batch.
    vec = next(iter(model.embed([text])))
    return normalize(vec.astype(np.float32)).tolist()


def embed_text(text: str) -> list[float]:
    """Embed a single text string, returning a 512-dim L2-normalized vector.

    Raises on failure — callers should wrap in their own guard when needed.
    """
    return embed_one(get_model(), text)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts sequentially (one inference call per text), reusing the model.

    Not true batched inference — it loops, but avoids per-call model
    construction and keeps batch results bit-identical to per-text calls (no
    padding-dependent drift). Convenience batch wrapper; the backfill hot path
    calls embed_text per-row directly. Raises on failure — callers should wrap
    in their own guard.
    """
    model = get_model()
    return [embed_one(model, text) for text in texts]
