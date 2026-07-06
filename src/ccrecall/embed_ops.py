"""Per-branch summary and chunk-embedding operations for session sync/import.

``write_branch_summary`` computes and stores the context summary for a
branch. ``embed_branch_chunks`` is the incremental write-path embedder,
implementing the clear-first/set-last watermark protocol described below.
"""

import hashlib
import logging
import sqlite3

from ccrecall.db import write_chunk_embedding
from ccrecall.embeddings import EMBEDDING_MODEL, EMBEDDING_VERSION, cap_for_embedding, embed_text
from ccrecall.models import LOGGER_NAME
from ccrecall.summarizer import SUMMARY_VERSION, build_exchange_pairs, compute_context_summary


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


# Maximum number of exchanges embedded per sync on the write path. Version-stale
# chunks (those only needing an EMBEDDING_VERSION bump) are deliberately left to
# the background backfill — only new or content-changed exchanges are eligible
# here. This cap bounds the detached sync-current process's worst case even for a
# first-sync of a long imported session or a rewind with many fresh exchanges.
MAX_WRITE_PATH_EMBEDS_PER_SYNC = 8


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


def embed_branch_chunks(
    cursor: sqlite3.Cursor,
    branch_db_id: int,
    branch_msgs: list[dict],
    is_active: bool,
    vec_writable: bool,
    max_embeds: int | None = MAX_WRITE_PATH_EMBEDS_PER_SYNC,
) -> int:
    """Embed per-exchange chunks for an active-leaf branch (incremental write path).

    Implements the clear-first/set-last watermark protocol:
    - If any exchanges need embedding (new or content-changed), the branch
      watermark is cleared to 0 BEFORE the embed loop (step 5a), then set to
      EMBEDDING_VERSION only after every exchange has a current-version chunk
      (step 8).
    - Version-stale chunks are deliberately left to the background backfill;
      this path embeds only new or content-changed exchanges.

    ``max_embeds`` bounds how many exchanges this call embeds. It defaults to
    MAX_WRITE_PATH_EMBEDS_PER_SYNC so the detached Stop-sync write path stays
    bounded even right after an EMBEDDING_VERSION bump. The off-hot-path backfill
    passes ``max_embeds=None`` (no cap) so a single call fully embeds a branch of
    any length — otherwise a branch with more exchanges than the cap would stay
    eligible and trip the backfill's no-progress guard.

    Returns the number of exchanges embedded by this call (the inference count) —
    the backfill uses it for accurate progress/ETA without recomputing exchanges.

    Raises on failure — callers (sync_branch) must wrap in
    contextlib.suppress(Exception). Does not commit; the single commit at
    sync_current.py:239 owns the transaction.
    """
    if not (is_active and vec_writable):
        return 0

    exchanges = build_exchange_pairs(branch_msgs)
    if not exchanges:
        # Active, writable branch with no embeddable exchange — e.g. a sub-agent /
        # sidechain branch whose messages are all assistant-role, or one left with
        # only notifications after the notification filter. There is nothing to
        # embed, but the branch is still eligible for the backfill, so stamp the
        # watermark to current: with zero exchanges the "all exchanges embedded"
        # invariant holds trivially. This drops the branch out of the eligible
        # set so the backfill doesn't re-select it forever and abort via the
        # no-progress guard. Self-correcting: if a user turn later lands, the
        # content diff re-clears the watermark and the new exchange is embedded.
        _stamp_branch_watermark(cursor, branch_db_id)
        return 0

    # Step 3 — compute embedded text, content hash, and bounded display text per exchange.
    # Display columns use the same head+tail cap per turn so the shown excerpt aligns
    # with the embedded region (design.md challenge M14).
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

    # Load existing chunk rows for this branch.
    cursor.execute(
        "SELECT exchange_index, content_hash, embedding_version, embedding_model FROM chunks WHERE branch_id = ?",
        (branch_db_id,),
    )
    existing_chunks: dict[int, dict] = {
        row[0]: {"content_hash": row[1], "embedding_version": row[2], "embedding_model": row[3]}
        for row in cursor.fetchall()
    }

    # Step 5 — diff: eligible = no chunk row OR content_hash changed.
    # Version-stale (embedding_version < EMBEDDING_VERSION) but content-unchanged
    # chunks are deliberately excluded — those are backfill's job (design H6).
    current_indices = {ed["index"] for ed in exchange_data}
    needing_embed_full = [
        ed
        for ed in exchange_data
        if ed["index"] not in existing_chunks or existing_chunks[ed["index"]]["content_hash"] != ed["content_hash"]
    ]
    indices_to_prune = set(existing_chunks) - current_indices

    # Early return: nothing to embed and nothing to prune
    if not needing_embed_full and not indices_to_prune:
        # Idempotent watermark repair: set to EMBEDDING_VERSION iff every existing
        # chunk is already version-current (repairs a prior failed step 8).
        if exchange_data and all(
            existing_chunks.get(ed["index"], {}).get("embedding_version") == EMBEDDING_VERSION for ed in exchange_data
        ):
            _stamp_branch_watermark(cursor, branch_db_id)
        return 0

    # Step 5a — clear-first: if any exchange needs embedding, clear the watermark
    # BEFORE the loop so a mid-loop exception leaves the branch stale, never
    # stale-but-true (single commit — sync_current.py:239 — persists this state).
    if needing_embed_full:
        cursor.execute("UPDATE branches SET embedding_version = 0 WHERE id = ?", (branch_db_id,))

    # Cap the embed loop to bound per-sync inference cost (write path); the
    # backfill passes max_embeds=None to embed the whole branch in one call.
    needing_embed = needing_embed_full if max_embeds is None else needing_embed_full[:max_embeds]

    # Step 6 — embed loop: for each needing-embed exchange, upsert the chunks row,
    # embed the text, then write the vector (order invariant: vector FIRST,
    # bookkeeping LAST — so a mid-loop exception leaves the chunk eligible for
    # backfill rather than marked done-without-vector).
    for ed in needing_embed:
        # Upsert chunks row via DELETE+INSERT (vec0 rejects INSERT OR REPLACE)
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
        # Vector FIRST (order invariant), bookkeeping LAST
        vec = embed_text(ed["text"])
        write_chunk_embedding(cursor, chunk_id, vec, EMBEDDING_VERSION, EMBEDDING_MODEL)

    # Step 7 — prune: delete chunks whose exchange_index no longer exists.
    # The chunks_vec_ad cascade trigger removes their chunk_vec rows automatically.
    if indices_to_prune:
        ph = ",".join("?" * len(indices_to_prune))
        cursor.execute(
            f"DELETE FROM chunks WHERE branch_id = ? AND exchange_index IN ({ph})",
            (branch_db_id, *indices_to_prune),
        )

    # Step 8 — set watermark iff every exchange now has a current-version chunk
    # with the correct content_hash. Checks both version AND content_hash so that
    # content-changed exchanges beyond the cap (left for backfill) don't falsely
    # satisfy the predicate.
    embedded_indices = {ed["index"] for ed in needing_embed}
    all_current = True
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
            all_current = False
            break
    if all_current:
        _stamp_branch_watermark(cursor, branch_db_id)

    return len(needing_embed)
