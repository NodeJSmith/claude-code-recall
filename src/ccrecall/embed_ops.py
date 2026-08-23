"""Per-branch summary and chunk-embedding operations for session sync/import.

``write_branch_summary`` computes and stores the context summary for a
branch. ``embed_branch_chunks`` is the incremental write-path embedder,
implementing the clear-first/set-last watermark protocol described below.
"""

import hashlib
import logging
import sqlite3

from ccrecall.db_vec import write_chunk_embedding
from ccrecall.embeddings import EMBEDDING_MODEL, EMBEDDING_VERSION, MODEL_TOKEN_LIMIT, cap_for_embedding, embed_batch
from ccrecall.models import LOGGER_NAME
from ccrecall.summarizer import SUMMARY_VERSION, build_exchange_pairs, compute_context_summary

# Maximum number of exchanges embedded per sync on the write path. Version-stale
# chunks (those only needing an EMBEDDING_VERSION bump) are deliberately left to
# the background backfill — only new or content-changed exchanges are eligible
# here. This cap bounds the detached sync-current process's worst case even for a
# first-sync of a long imported session or a rewind with many fresh exchanges.
MAX_WRITE_PATH_EMBEDS_PER_SYNC = 8


def effective_cap_tokens(cap_tokens: int | None) -> int:
    """Resolve a chunk's stored cap_tokens to a comparable int.

    A NULL cap_tokens means the chunk was embedded before this column existed
    (or under no cap), which is equivalent to the model's own hard limit.
    Every comparison site must go through this helper instead of comparing
    cap_tokens directly, to avoid TypeError on `None < int`.
    """
    if cap_tokens is None:
        return MODEL_TOKEN_LIMIT
    return cap_tokens


def write_branch_summary(cursor: sqlite3.Cursor, branch_db_id: int) -> str | None:
    """Compute and store context summary for a branch; return summary_md or None.

    Classifies failures three ways — moved wholesale from sync_session:
    - (ValueError, TypeError, KeyError): content error — skip without logging.
    - sqlite3.Error: infra error — log and skip.
    - Any other exception: propagates (genuine bug, not masked).
    """
    summary_md = None
    try:
        summary_md, summary_json = compute_context_summary(cursor, branch_db_id)
        cursor.execute(
            """
            UPDATE branches SET context_summary = ?, context_summary_json = ?, summary_version = ?
            WHERE id = ?
            """,
            (summary_md, summary_json, SUMMARY_VERSION, branch_db_id),
        )
    except (ValueError, TypeError, KeyError):
        # Content error (malformed summary data) — same classification as
        # backfill_summaries: skip this branch's summary without failing the
        # sync/import. A real bug (e.g. AttributeError) still propagates.
        summary_md = None
    except sqlite3.Error:
        # Infra error (locked/failed DB write): log and skip the summary
        # rather than aborting the whole import (this runs per branch with no
        # outer handler in the import loop). The branch stays eligible for
        # backfill, and the failure is observable in the log instead of being
        # silently swallowed.
        logging.getLogger(LOGGER_NAME).exception("sync: summary write failed for branch %s", branch_db_id)
        summary_md = None
    return summary_md


def _stamp_branch_watermark(cursor: sqlite3.Cursor, branch_db_id: int) -> None:
    """Set a branch's embedding watermark to the current version + model.

    Meaning: every current exchange of this branch has a current-version chunk
    vector. Written at the three points that establish that invariant — the
    zero-exchange case, the idempotent repair, and the step-8 success path.
    """
    cursor.execute(
        "UPDATE branches SET embedding_version = ?, embedding_model = ? WHERE id = ?",
        (EMBEDDING_VERSION, EMBEDDING_MODEL, branch_db_id),
    )


def _prepare_exchange_data(exchanges: list[dict]) -> list[dict]:
    """Step 3 — compute embedded text, content hash, and bounded display text per exchange.

    Display columns use the same head+tail cap per turn so the shown excerpt aligns
    with the embedded region (design.md challenge M14).
    """
    exchange_data = []
    for ex in exchanges:
        user = ex.get("user") or ""
        assistant = ex.get("assistant") or ""
        combined = f"{user}\n\n{assistant}"
        text, was_capped = cap_for_embedding(combined)
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        user_text, _ = cap_for_embedding(user)
        assistant_text, _ = cap_for_embedding(assistant)
        exchange_data.append(
            {
                "index": ex["index"],
                "text": text,
                "was_capped": was_capped,
                "content_hash": content_hash,
                "timestamp": ex.get("timestamp"),
                "first_message_uuid": ex.get("first_message_uuid"),
                "user_text": user_text,
                "assistant_text": assistant_text,
            }
        )
    return exchange_data


def _diff_exchanges(exchange_data: list[dict], existing_chunks: dict[int, dict]) -> tuple[list[dict], set[int]]:
    """Step 5 — diff: eligible = no chunk row OR content_hash changed.

    Version-stale (embedding_version < EMBEDDING_VERSION) but content-unchanged
    chunks are deliberately excluded — those are backfill's job (design H6).
    """
    current_indices = {ed["index"] for ed in exchange_data}
    needing_embed = [
        ed
        for ed in exchange_data
        if ed["index"] not in existing_chunks or existing_chunks[ed["index"]]["content_hash"] != ed["content_hash"]
    ]
    indices_to_prune = set(existing_chunks) - current_indices
    return needing_embed, indices_to_prune


def _write_embedded_chunks(cursor: sqlite3.Cursor, branch_db_id: int, needing_embed: list[dict], vecs: list) -> None:
    """Step 6b — per chunk: upsert the chunk row, then write its vector.

    Order invariant: vector FIRST, bookkeeping LAST — so a mid-loop exception
    leaves that chunk eligible for backfill rather than marked
    done-without-vector.
    """
    for ed, vec in zip(needing_embed, vecs, strict=True):
        cursor.execute(
            "DELETE FROM chunks WHERE branch_id = ? AND exchange_index = ?",
            (branch_db_id, ed["index"]),
        )
        cursor.execute(
            """
            INSERT INTO chunks (
                branch_id, exchange_index, content_hash, first_message_uuid,
                timestamp, user_text, assistant_text, was_capped,
                embedding_version, embedding_model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
            """,
            (
                branch_db_id,
                ed["index"],
                ed["content_hash"],
                ed["first_message_uuid"],
                ed["timestamp"],
                ed["user_text"],
                ed["assistant_text"],
                int(ed["was_capped"]),
            ),
        )
        chunk_id = cursor.lastrowid
        assert chunk_id is not None  # noqa: S101 — lastrowid is non-None after a successful INSERT
        write_chunk_embedding(cursor, chunk_id, vec, EMBEDDING_VERSION, EMBEDDING_MODEL)


def _prune_stale_chunks(cursor: sqlite3.Cursor, branch_db_id: int, indices_to_prune: set[int]) -> None:
    """Step 7 — prune: delete chunks whose exchange_index no longer exists.

    The chunks_vec_ad cascade trigger removes their chunk_vec rows automatically.
    """
    if not indices_to_prune:
        return
    placeholders = ",".join("?" * len(indices_to_prune))
    cursor.execute(
        f"DELETE FROM chunks WHERE branch_id = ? AND exchange_index IN ({placeholders})",
        (branch_db_id, *indices_to_prune),
    )


def _should_stamp_watermark(
    exchange_data: list[dict], embedded_indices: set[int], existing_chunks: dict[int, dict]
) -> bool:
    """Step 8 — every exchange now has a current-version chunk with the correct content_hash?

    Checks both version AND content_hash so that content-changed exchanges
    beyond the cap (left for backfill) don't falsely satisfy the predicate.
    """
    for ed in exchange_data:
        idx = ed["index"]
        if idx in embedded_indices:
            continue  # just embedded at EMBEDDING_VERSION with correct content_hash
        existing = existing_chunks.get(idx)
        if (
            existing is None
            or existing["embedding_version"] != EMBEDDING_VERSION
            or existing["content_hash"] != ed["content_hash"]
        ):
            return False
    return True


def embed_branch_chunks(
    cursor: sqlite3.Cursor,
    branch_db_id: int,
    branch_msgs: list[dict],
    is_active: bool,
    vec_writable: bool,
    max_embeds: int | None = MAX_WRITE_PATH_EMBEDS_PER_SYNC,
) -> int:
    """Embed per-exchange chunks for an active-leaf branch (incremental write path).

    Implements the clear-first/set-last watermark protocol: watermark clears to 0
    before the embed loop (step 5a) when exchanges need embedding, then sets to
    EMBEDDING_VERSION only once every exchange has a current-version chunk (step 8).
    Version-stale chunks are left to the background backfill.

    ``max_embeds`` bounds per-call inference cost (defaults to
    MAX_WRITE_PATH_EMBEDS_PER_SYNC for the write path; backfill passes None so a
    single call fully embeds a branch, avoiding backfill's no-progress guard).

    Returns the inference count (exchanges embedded). Raises on failure — callers
    (sync_branch) must wrap in contextlib.suppress(Exception). Does not commit; the
    single commit at sync_current.py:239 owns the transaction.
    """
    if not (is_active and vec_writable):
        return 0

    exchanges = build_exchange_pairs(branch_msgs)
    if not exchanges:
        # No embeddable exchange (e.g. an all-assistant sub-agent branch). Stamp
        # the watermark trivially-true so backfill doesn't re-select it forever;
        # self-correcting if a user turn later lands (content diff re-clears it).
        _stamp_branch_watermark(cursor, branch_db_id)
        return 0

    exchange_data = _prepare_exchange_data(exchanges)

    cursor.execute(
        "SELECT exchange_index, content_hash, embedding_version, embedding_model FROM chunks WHERE branch_id = ?",
        (branch_db_id,),
    )
    existing_chunks: dict[int, dict] = {
        row[0]: {"content_hash": row[1], "embedding_version": row[2], "embedding_model": row[3]}
        for row in cursor.fetchall()
    }

    needing_embed_full, indices_to_prune = _diff_exchanges(exchange_data, existing_chunks)

    if not needing_embed_full and not indices_to_prune:
        # Idempotent watermark repair: repairs a prior failed step 8.
        if exchange_data and _should_stamp_watermark(exchange_data, set(), existing_chunks):
            _stamp_branch_watermark(cursor, branch_db_id)
        return 0

    # Clear-first (step 5a): clear the watermark BEFORE the embed loop so a
    # mid-loop exception leaves the branch stale, never stale-but-true.
    if needing_embed_full:
        cursor.execute("UPDATE branches SET embedding_version = 0 WHERE id = ?", (branch_db_id,))

    # max_embeds bounds per-sync inference cost; backfill passes None (no cap).
    needing_embed = needing_embed_full if max_embeds is None else needing_embed_full[:max_embeds]

    # Embed-before-write (step 6): batch-embed all texts BEFORE any DB write,
    # so a failed embed_batch call leaves every existing chunk row/vector intact.
    texts = [ed["text"] for ed in needing_embed]
    vecs = embed_batch(texts)

    _write_embedded_chunks(cursor, branch_db_id, needing_embed, vecs)
    _prune_stale_chunks(cursor, branch_db_id, indices_to_prune)

    embedded_indices = {ed["index"] for ed in needing_embed}
    if _should_stamp_watermark(exchange_data, embedded_indices, existing_chunks):
        _stamp_branch_watermark(cursor, branch_db_id)

    return len(needing_embed)
