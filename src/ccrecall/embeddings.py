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
EMBEDDING_VERSION = 3  # Bumped from 2: per-exchange chunk granularity (was per-branch summary).
EMBEDDING_DIM = 512

# Token-aware cap constants for cap_for_embedding.
# EMBED_CHAR_BUDGET is the initial char split (head + tail each get half).
# MODEL_TOKEN_LIMIT is jina-v2-small's hard context limit; the cap tightens until
# len(tokens) <= MODEL_TOKEN_LIMIT so dense content never trips CONTENT_ERROR.
EMBED_CHAR_BUDGET = 32_000
MODEL_TOKEN_LIMIT = 8192

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
    assert TextEmbedding is not None  # noqa: S101 — type-checker narrowing; the real guard is the RuntimeError above
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


def cap_for_embedding(text: str) -> tuple[str, bool]:
    """Head+tail-cap text to fit within the embedding model's token limit.

    Returns ``(possibly_capped_text, was_capped)``. ``was_capped=False`` means
    the text was already within both the char budget and the token limit and is
    returned unchanged. ``was_capped=True`` means the middle was dropped and the
    returned text is the head+tail-capped form.

    The cap always keeps both the beginning and the end of the text so a single
    large pasted block or tool dump degrades one chunk's signal rather than
    discarding the exchange. The post-check loop tightens the cap until
    ``len(tokens) <= MODEL_TOKEN_LIMIT``, so dense content (base64, minified JSON)
    that is under the char budget but over the token limit cannot reach
    ``embed_text`` and trip ``CONTENT_ERROR``.

    Reaches the tokenizer through ``get_model()`` (the singleton accessor) — no
    second embedding code path is created.
    """
    if not text:
        return text, False

    model = get_model()

    # Fast path: text fits within both budgets as-is
    if len(text) <= EMBED_CHAR_BUDGET and model.token_count([text]) <= MODEL_TOKEN_LIMIT:
        return text, False

    # Determine initial head/tail split.
    # Char-over-budget case: split at the budget boundary.
    # Dense-token case (text <= char budget but token-dense): start at 40 % each
    # side so the middle is genuinely dropped on the first iteration — starting at
    # 50 % each side would reconstruct the full text and make no progress.
    if len(text) > EMBED_CHAR_BUDGET:
        head = EMBED_CHAR_BUDGET // 2
        tail = EMBED_CHAR_BUDGET // 2
    else:
        head = max(len(text) * 2 // 5, 1)
        tail = max(len(text) * 2 // 5, 1)

    capped = text[:head] + "\n\n[...]\n\n" + text[-tail:]

    # Tighten until within token limit
    while model.token_count([capped]) > MODEL_TOKEN_LIMIT:
        head = max(head * 3 // 4, 1)
        tail = max(tail * 3 // 4, 1)
        next_capped = text[:head] + "\n\n[...]\n\n" + text[-tail:]
        if next_capped == capped:
            break  # no further reduction possible; pathological — let embed_text raise
        capped = next_capped

    return capped, True
