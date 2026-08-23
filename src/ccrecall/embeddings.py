"""Shared embedding module — the single source of truth for vectors.

Both the write path and the query path must import from here. No second
embedding code path may exist.
"""

import logging
import os
import tempfile
from pathlib import Path

import numpy as np

from ccrecall.models import LOGGER_NAME

log = logging.getLogger(LOGGER_NAME)

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

# fastembed's supported-model registry serves jina-v2-small from the xenova/ HF
# mirror (differs from EMBEDDING_MODEL's jinaai/ model-card prefix; there is no
# programmatic map between the two, so this source repo is tracked explicitly and
# must be updated alongside EMBEDDING_MODEL). The on-disk cache subdir follows
# HuggingFace's models--{org}--{name} snapshot convention.
EMBEDDING_MODEL_HF_SOURCE = "xenova/jina-embeddings-v2-small-en"
EMBEDDING_MODEL_CACHE_SUBDIR = "models--" + EMBEDDING_MODEL_HF_SOURCE.replace("/", "--")

# Token-aware cap constants for cap_for_embedding.
# EMBED_CHAR_BUDGET is the initial char split (head + tail each get half).
# MODEL_TOKEN_LIMIT is jina-v2-small's hard context limit; the cap tightens until
# len(tokens) <= MODEL_TOKEN_LIMIT so dense content never trips CONTENT_ERROR.
EMBED_CHAR_BUDGET = 32_000
MODEL_TOKEN_LIMIT = 8192

# Token cap applied on the interactive sync-current write path — tighter than
# MODEL_TOKEN_LIMIT to bound per-sync inference cost. See design/specs/015.
SYNC_PATH_TOKEN_LIMIT = 4096

# Marker spliced between the kept head and tail when the middle is dropped.
# Both build sites in cap_for_embedding must agree, so the literal lives here once.
_CAP_MARKER = "\n\n[...]\n\n"

# fastembed defaults its inference parallelism to every CPU core, so each
# inference call briefly saturates the whole machine. Interactive query/write
# paths embed one or a few texts at a time; the opt-in backfill runs ~1.9k
# active-leaf inferences in one go (planned by embed_batch — see below). A low
# thread count costs interactive paths almost nothing while keeping the
# backfill from thrashing constrained machines. The backfill exposes
# `--threads` to raise this on an idle machine; interactive paths always use
# the default.
DEFAULT_EMBED_THREADS = 1

# Inference batching bounds for embed_batch. fastembed's own default is
# batch_size=256, and it pads every batch to its longest text; with texts capped
# at MODEL_TOKEN_LIMIT (8192), one 256x8192 fp32 batch needs >20 GB of
# onnxruntime activations — enough to OOM-kill the process and, on WSL, take
# down the whole VM (observed: 24 GB RAM + 6 GB swap, repeatedly).
#
# The dominant term is QUADRATIC in padded sequence length: jina-v2-small's
# ALiBi attention is not fused in the ONNX export, so onnxruntime materializes
# the full seq x seq score matrix per head (plus softmax/workspace copies at
# ~5x the raw tensor). Measured single-text peaks (batch_size=1): ~1 GB at
# ~2.2k tokens, ~4 GB at ~4.5k tokens, ~8.4 GB at the 8192 cap. embed_batch
# therefore plans its own batches on the attention-area budget below: no
# model.embed() call may have texts-in-batch x longest-text-in-batch**2
# exceeding EMBED_BATCH_ATTENTION_BUDGET (nor more than EMBED_BATCH_MAX_TEXTS
# texts). Setting the budget to one max-length text's area means packing can
# never exceed the worst case a single capped text already has (a lone 8192
# token text) — that residual ~8.4 GB peak is a known, accepted tradeoff of
# keeping the 8192 cap (lowering it would change vectors and force a full
# re-embed; revisit if it ever bites outside backfill). get_model disables
# onnxruntime's CPU arena so the transient is returned to the OS after each
# call instead of ratcheting RSS across a backfill.
EMBED_BATCH_ATTENTION_BUDGET = MODEL_TOKEN_LIMIT * MODEL_TOKEN_LIMIT
EMBED_BATCH_MAX_TEXTS = 32

# cap_for_embedding tuning: dense-token texts start at DENSE_SPLIT_RATIO of
# total length for head and tail each (40% + 40% = 80%, dropping the middle
# 20%). The SHRINK_FACTOR tightens on each iteration when still over the token
# limit, keeping 75% of the previous head/tail each pass.
DENSE_SPLIT_NUMERATOR = 2
DENSE_SPLIT_DENOMINATOR = 5
SHRINK_NUMERATOR = 3
SHRINK_DENOMINATOR = 4

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
    _model = TextEmbedding(
        model_name=EMBEDDING_MODEL,
        threads=resolve_thread_count(threads),
        # onnxruntime's CPU arena retains each run's peak workspace for reuse
        # and never returns it to the OS, so back-to-back capped texts (8192
        # tokens, ~8.4 GB transient each) ratchet RSS until the machine OOMs —
        # malloc_trim can't touch the arena. With the arena off, allocations go
        # through glibc and the transient is returned after every call
        # (measured: RSS falls back to ~0.3 GB between calls, HWM flat at
        # ~8.4 GB across a run of capped texts). Allocation strategy only —
        # computed vectors are identical.
        enable_cpu_mem_arena=False,
    )
    return _model


def is_model_cached_on_disk() -> bool:
    """Return True iff the fastembed model's cache directory exists on disk.

    A True result means get_model() will load from disk (fast — milliseconds).
    A False result means get_model() may trigger a ~120 MB network download.

    Cache root: $FASTEMBED_CACHE_PATH env var, or <tempdir>/fastembed_cache/.
    Model subdir: EMBEDDING_MODEL_CACHE_SUBDIR (see its definition for how the
    HuggingFace snapshot name is derived from the model's HF source repo).
    """
    try:
        default_cache = os.path.join(tempfile.gettempdir(), "fastembed_cache")
        cache_root = Path(os.environ.get("FASTEMBED_CACHE_PATH", default_cache))
        return (cache_root / EMBEDDING_MODEL_CACHE_SUBDIR).exists()
    except Exception:
        log.debug("embedding cache check failed", exc_info=True)
        return False


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
        log.warning("embedding model failed to load", exc_info=True)
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


def _plan_embed_batches(token_counts: list[int]) -> list[list[int]]:
    """Group text indices into inference batches under the memory bounds above.

    Longest-first ordering packs similar lengths together (tight padding — short
    texts never ride a batch padded to a long outlier) and makes each batch's
    longest text its first element, so the attention-area budget check is simply
    ``(len(batch) + 1) * longest**2 > EMBED_BATCH_ATTENTION_BUDGET``. Every index
    appears in exactly one batch; a single text larger than the budget gets a
    batch to itself (cap_for_embedding keeps it <= MODEL_TOKEN_LIMIT, so a lone
    text is always inferable).
    """
    order = sorted(range(len(token_counts)), key=lambda i: token_counts[i], reverse=True)
    batches: list[list[int]] = []
    current: list[int] = []
    longest = 0
    for i in order:
        # order is descending, so token_counts[i] <= longest for any non-empty
        # batch: longest**2 already covers the candidate's area contribution.
        next_area = (len(current) + 1) * longest * longest
        if current and (len(current) >= EMBED_BATCH_MAX_TEXTS or next_area > EMBED_BATCH_ATTENTION_BUDGET):
            batches.append(current)
            current = []
        if not current:
            longest = token_counts[i]
        current.append(i)
    if current:
        batches.append(current)
    return batches


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts, returning vectors in the same order as the input.

    Plans memory-bounded batches (see EMBED_BATCH_ATTENTION_BUDGET /
    EMBED_BATCH_MAX_TEXTS) rather than handing fastembed the full list at its
    default batch_size=256 — the padding-to-longest behavior of a batch that
    size at up to 8192 tokens per text allocates tens of GB in onnxruntime.
    Texts are token-counted, grouped longest-first, and embedded one planned
    group per model.embed() call with batch_size=len(group) so fastembed does
    not re-slice the group into larger batches. Batching changes nothing about
    the output: each text's vector is computed independently (padding is masked
    out) and restored to input position here. Raises on failure.
    """
    if not texts:
        return []
    model = get_model()
    # One token_count call per text before inference (the model tokenizes again
    # during embed). Exact counts beat a char heuristic: dense/CJK text can
    # exceed 1 token per 2 chars, and an underestimated count is a
    # memory-budget violation.
    token_counts = [model.token_count([text]) for text in texts]
    vectors: dict[int, list[float]] = {}
    for batch in _plan_embed_batches(token_counts):
        batch_texts = [texts[i] for i in batch]
        for i, vec in zip(batch, model.embed(batch_texts, batch_size=len(batch_texts)), strict=True):
            vectors[i] = normalize(vec.astype(np.float32)).tolist()
    return [vectors[i] for i in range(len(texts))]


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
        head = max(len(text) * DENSE_SPLIT_NUMERATOR // DENSE_SPLIT_DENOMINATOR, 1)
        tail = max(len(text) * DENSE_SPLIT_NUMERATOR // DENSE_SPLIT_DENOMINATOR, 1)

    capped = text[:head] + _CAP_MARKER + text[-tail:]

    while model.token_count([capped]) > MODEL_TOKEN_LIMIT:
        head = max(head * SHRINK_NUMERATOR // SHRINK_DENOMINATOR, 1)
        tail = max(tail * SHRINK_NUMERATOR // SHRINK_DENOMINATOR, 1)
        next_capped = text[:head] + _CAP_MARKER + text[-tail:]
        if next_capped == capped:
            break  # no further reduction possible; pathological — let embed_text raise
        capped = next_capped

    return capped, True
