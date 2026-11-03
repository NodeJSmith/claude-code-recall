"""Per-branch summary and chunk-embedding operations for session sync/import.

``write_branch_summary`` computes and stores the context summary for a
branch. ``embed_branch_chunks`` is the one embed protocol, shared by sync and
backfill: it owns its transactions so inference runs with no lock held.
"""

import hashlib
import logging
import sqlite3
from typing import NamedTuple

from ccrecall import job_lock
from ccrecall.config import JOB_KEY_BACKFILL_EMBEDDINGS
from ccrecall.db_base import commit_single_write, is_database_locked
from ccrecall.db_vec import fetch_branch_messages, write_chunk_embedding
from ccrecall.embeddings import (
    EMBEDDING_MODEL,
    EMBEDDING_VERSION,
    MODEL_TOKEN_LIMIT,
    SYNC_PATH_TOKEN_LIMIT,
    cap_for_embedding,
    embed_batch,
)
from ccrecall.models import LOGGER_NAME
from ccrecall.summarizer import SUMMARY_VERSION, build_exchange_pairs, compute_context_summary

log = logging.getLogger(LOGGER_NAME)

# Maximum number of exchanges embedded per sync on the write path. Version-stale
# chunks (those only needing an EMBEDDING_VERSION bump) are deliberately left to
# the background backfill — only new or content-changed exchanges are eligible
# here. This cap bounds the detached sync-current process's worst case even for a
# first-sync of a long imported session or a rewind with many fresh exchanges.
MAX_WRITE_PATH_EMBEDS_PER_SYNC = 8


class EmbedOutcome(NamedTuple):
    """What one ``embed_branch_chunks`` call left behind."""

    embedded: int  # vectors written
    stamped: bool  # the watermark now says this branch is fully embedded


def resolve_sync_cap(raw_setting: object) -> int:
    """Clamp the raw ``sync_path_token_limit`` setting to ``[1, MODEL_TOKEN_LIMIT]``; a non-int gets the default."""
    if isinstance(raw_setting, bool) or not isinstance(raw_setting, int):
        return SYNC_PATH_TOKEN_LIMIT
    return min(max(raw_setting, 1), MODEL_TOKEN_LIMIT)


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
        log.exception("sync: summary write failed for branch %s", branch_db_id)
        summary_md = None
    return summary_md


def _stamp_branch_watermark(cursor: sqlite3.Cursor, branch_db_id: int) -> None:
    """Set a branch's embedding watermark to the current version + model.

    Meaning: every current exchange of this branch has a current-version chunk
    vector. Only the write step sets it, after re-checking that this is true.
    """
    cursor.execute(
        "UPDATE branches SET embedding_version = ?, embedding_model = ? WHERE id = ?",
        (EMBEDDING_VERSION, EMBEDDING_MODEL, branch_db_id),
    )


def _branch_is_stamped(cursor: sqlite3.Cursor, branch_db_id: int) -> bool:
    """Does the watermark hold what ``_stamp_branch_watermark`` writes?"""
    cursor.execute("SELECT embedding_version, embedding_model FROM branches WHERE id = ?", (branch_db_id,))
    return cursor.fetchone() == (EMBEDDING_VERSION, EMBEDDING_MODEL)


def _combined_text(exchange: dict) -> str:
    return f"{exchange.get('user') or ''}\n\n{exchange.get('assistant') or ''}"


def _content_hash(exchange: dict) -> str:
    """Identity of an exchange's raw text. Needs no tokenizer, so it's cheap enough to run under the write lock."""
    return hashlib.sha256(_combined_text(exchange).encode()).hexdigest()


def _prepare_exchange_data(exchanges: list[dict], cap_limit: int = MODEL_TOKEN_LIMIT) -> list[dict]:
    """Compute embedded text, content hash, and bounded display text per exchange.

    ``content_hash`` is derived from the raw, uncapped ``combined`` text — not
    from the post-cap output — so content identity is stable across cap tiers
    (sync at 4096 vs backfill at 8192). Hashing the capped text would make the
    same exchange hash differently depending on which path embedded it,
    causing sync/backfill to ping-pong re-embedding each other's work forever.
    See design/specs/015.

    Display columns use the same head+tail cap per turn (at ``cap_limit``) so
    the shown excerpt aligns with the embedded region (design.md challenge M14).
    """
    exchange_data = []
    for ex in exchanges:
        text, was_capped = cap_for_embedding(_combined_text(ex), max_tokens=cap_limit)
        user_text, _ = cap_for_embedding(ex.get("user") or "", max_tokens=cap_limit)
        assistant_text, _ = cap_for_embedding(ex.get("assistant") or "", max_tokens=cap_limit)
        exchange_data.append(
            {
                "index": ex["index"],
                "text": text,
                "content_hash": _content_hash(ex),
                "timestamp": ex.get("timestamp"),
                "first_message_uuid": ex.get("first_message_uuid"),
                "user_text": user_text,
                "assistant_text": assistant_text,
                # NULL unless the text was actually truncated — an untruncated
                # exchange is full-quality regardless of which cap was applied.
                # See design/specs/015 (Edge Cases).
                "cap_tokens": cap_limit if was_capped else None,
            }
        )
    return exchange_data


def _read_chunks(cursor: sqlite3.Cursor, branch_db_id: int) -> dict[int, dict]:
    """A branch's chunks by exchange index, with whether each has a stored vector."""
    cursor.execute(
        """
        SELECT c.exchange_index, c.content_hash, c.embedding_version, c.embedding_model, c.cap_tokens,
               EXISTS (SELECT 1 FROM chunk_vec WHERE chunk_id = c.id)
        FROM chunks c WHERE c.branch_id = ?
        """,
        (branch_db_id,),
    )
    return {
        row[0]: {
            "content_hash": row[1],
            "embedding_version": row[2],
            "embedding_model": row[3],
            "cap_tokens": row[4],
            "has_vector": bool(row[5]),
        }
        for row in cursor.fetchall()
    }


def count_exchanges_needing_chunks(cursor: sqlite3.Cursor, branch_db_id: int) -> int:
    """How many of the branch's exchanges have no chunk, or a chunk embedded from different text.

    Read-only and vec-free: it never touches ``chunk_vec``, so a vector's
    presence is judged separately.
    """
    exchanges = build_exchange_pairs(fetch_branch_messages(cursor, branch_db_id, include_notifications=False))
    cursor.execute("SELECT exchange_index, content_hash FROM chunks WHERE branch_id = ?", (branch_db_id,))
    stored = dict(cursor.fetchall())
    return sum(1 for ex in exchanges if stored.get(ex["index"]) != _content_hash(ex))


def _chunk_is_current(chunk: dict, content_hash: str) -> bool:
    """The chunk holds a current-model vector of exactly this text. Quality tier is judged separately."""
    return (
        chunk["content_hash"] == content_hash
        and chunk["embedding_version"] == EMBEDDING_VERSION
        and chunk["embedding_model"] == EMBEDDING_MODEL
        and chunk["has_vector"]
    )


def _chunk_at_least_as_good(chunk: dict, prepared_exchange: dict) -> bool:
    """A stored chunk this new vector shouldn't replace: current, at equal or better quality.

    An untruncated vector's cap_tokens is None, which counts as full quality
    whatever cap it was prepared under.
    """
    stored_cap = effective_cap_tokens(chunk["cap_tokens"])
    new_cap = effective_cap_tokens(prepared_exchange["cap_tokens"])
    return _chunk_is_current(chunk, prepared_exchange["content_hash"]) and stored_cap >= new_cap


def _diff_exchanges(
    exchange_data: list[dict],
    existing_chunks: dict[int, dict],
    target_cap: int = MODEL_TOKEN_LIMIT,
    replace_stale: bool = False,
) -> list[dict]:
    """Exchanges needing inference: no chunk row, content_hash changed, OR cap-tokens upgrade needed.

    Version-stale but content-unchanged chunks are excluded unless
    ``replace_stale`` is set: those are backfill's job (design H6). With it set,
    every chunk that isn't current is selected, and the write step replaces it,
    so a failed embed leaves the old chunk and vector intact.

    A chunk is also flagged when its stored cap_tokens is below ``target_cap``,
    even if content_hash matches — this is how backfill detects draft-quality
    chunks needing an upgrade. ``target_cap`` is the caller's own token limit
    (SYNC_PATH_TOKEN_LIMIT on sync, MODEL_TOKEN_LIMIT on backfill), so the sync
    path never flags a chunk already embedded at a higher backfill cap for downgrade.
    """
    needing_embed = []
    for ed in exchange_data:
        existing = existing_chunks.get(ed["index"])
        if (
            existing is None
            or existing["content_hash"] != ed["content_hash"]
            or effective_cap_tokens(existing.get("cap_tokens")) < target_cap
            or (replace_stale and not _chunk_is_current(existing, ed["content_hash"]))
        ):
            needing_embed.append(ed)
    return needing_embed


def _write_embedded_chunks(cursor: sqlite3.Cursor, branch_db_id: int, needing_embed: list[dict], vecs: list) -> None:
    """Per chunk: upsert the chunk row, then write its vector.

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
                timestamp, user_text, assistant_text, cap_tokens,
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
                ed["cap_tokens"],
            ),
        )
        chunk_id = cursor.lastrowid
        assert chunk_id is not None  # noqa: S101 — lastrowid is non-None after a successful INSERT
        write_chunk_embedding(cursor, chunk_id, vec, EMBEDDING_VERSION, EMBEDDING_MODEL)


def _prune_stale_chunks(cursor: sqlite3.Cursor, branch_db_id: int, indices_to_prune: set[int]) -> None:
    """Delete chunks whose exchange_index no longer exists.

    The chunks_vec_ad cascade trigger removes their chunk_vec rows automatically.
    """
    if not indices_to_prune:
        return
    placeholders = ",".join("?" * len(indices_to_prune))
    cursor.execute(
        f"DELETE FROM chunks WHERE branch_id = ? AND exchange_index IN ({placeholders})",
        (branch_db_id, *indices_to_prune),
    )


def _should_stamp_watermark(exchange_hashes: dict[int, str], chunks: dict[int, dict]) -> bool:
    """Is every exchange's chunk current at full quality?

    ``chunks`` is the state after the write, so one rule covers chunks written
    just now and chunks another process wrote. A draft-quality chunk
    (cap_tokens < MODEL_TOKEN_LIMIT) withholds the stamp: that is how backfill's
    selection finds it for upgrade.
    """
    for index, content_hash in exchange_hashes.items():
        chunk = chunks.get(index)
        if (
            chunk is None
            or not _chunk_is_current(chunk, content_hash)
            or effective_cap_tokens(chunk["cap_tokens"]) < MODEL_TOKEN_LIMIT
        ):
            return False
    return True


def _apply_write(cursor: sqlite3.Cursor, branch_db_id: int, prepared: list[dict], vecs: list) -> EmbedOutcome:
    """Re-read the branch and write whatever still applies. Caller holds the write lock.

    The branch can change between the read and this write, since no lock is held
    during inference. A vector whose exchange text changed is discarded, and one
    that would replace an equal or better chunk (say, backfill's 8192 vector
    arriving first) is skipped.
    """
    if cursor.execute("SELECT 1 FROM branches WHERE id = ?", (branch_db_id,)).fetchone() is None:
        log.info("embed write skipped: branch no longer exists", extra={"branch_id": branch_db_id})
        return EmbedOutcome(embedded=0, stamped=False)

    exchanges = build_exchange_pairs(fetch_branch_messages(cursor, branch_db_id, include_notifications=False))
    exchange_hashes = {ex["index"]: _content_hash(ex) for ex in exchanges}
    existing = _read_chunks(cursor, branch_db_id)

    to_write: list[dict] = []
    to_write_vecs: list = []
    discarded = 0
    for ed, vec in zip(prepared, vecs, strict=True):
        if exchange_hashes.get(ed["index"]) != ed["content_hash"]:
            discarded += 1
            continue
        chunk = existing.get(ed["index"])
        if chunk is not None and _chunk_at_least_as_good(chunk, ed):
            continue
        to_write.append(ed)
        to_write_vecs.append(vec)
    if discarded:
        log.info(
            "embed write discarded vectors for changed exchanges",
            extra={"branch_id": branch_db_id, "discarded": discarded},
        )

    _write_embedded_chunks(cursor, branch_db_id, to_write, to_write_vecs)
    _prune_stale_chunks(cursor, branch_db_id, set(existing) - set(exchange_hashes))

    stamped = _should_stamp_watermark(exchange_hashes, _read_chunks(cursor, branch_db_id))
    if stamped:
        _stamp_branch_watermark(cursor, branch_db_id)
    else:
        log.info("embedding watermark withheld", extra={"branch_id": branch_db_id})
    return EmbedOutcome(embedded=len(to_write), stamped=stamped)


def _write_step(conn: sqlite3.Connection, branch_db_id: int, prepared: list[dict], vecs: list) -> EmbedOutcome:
    """Run the one write transaction for a branch's embed.

    ``BEGIN IMMEDIATE`` comes before the re-read, so ``busy_timeout`` waits for
    the lock and nothing can commit between the check and the stamp. A deferred
    transaction here would fail outright when another writer committed between
    its read and its first write.
    """
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    try:
        outcome = _apply_write(cursor, branch_db_id, prepared, vecs)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return outcome


def _read_branch_state(cursor: sqlite3.Cursor, branch_db_id: int) -> tuple[list[dict], dict[int, dict]]:
    """Read a branch's hashed exchanges and stored chunks, with no write transaction."""
    exchanges = build_exchange_pairs(fetch_branch_messages(cursor, branch_db_id, include_notifications=False))
    hashed = [{**ex, "content_hash": _content_hash(ex)} for ex in exchanges]
    return hashed, _read_chunks(cursor, branch_db_id)


def _finish_without_inference(
    conn: sqlite3.Connection, branch_db_id: int, exchanges: list[dict], chunks: dict[int, dict]
) -> EmbedOutcome:
    """Take the write lock only if the write step would change something.

    Anything it would write is re-checked there, so a stale read here can at
    worst delay a stamp or prune until the next call.
    """
    stamped = _branch_is_stamped(conn.cursor(), branch_db_id)
    hashes = {ex["index"]: ex["content_hash"] for ex in exchanges}
    needs_prune = bool(set(chunks) - set(hashes))
    needs_stamp = not stamped and _should_stamp_watermark(hashes, chunks)
    if needs_prune or needs_stamp:
        return _write_step(conn, branch_db_id, [], [])
    return EmbedOutcome(embedded=0, stamped=stamped)


def embed_branch_chunks(
    conn: sqlite3.Connection,
    branch_db_id: int,
    *,
    max_embeds: int | None = MAX_WRITE_PATH_EMBEDS_PER_SYNC,
    cap_limit: int = MODEL_TOKEN_LIMIT,
    replace_stale: bool = False,
    yield_to_backfill: bool = False,
) -> EmbedOutcome:
    """Bring one branch's chunks up to date, holding no lock during inference.

    The caller must have no transaction open, since this commits on ``conn``:

    1. Read and diff, with no write transaction.
    2. If any exchange needs embedding, commit the watermark clear, so a crash
       during inference can't leave a false stamp.
    3. With ``yield_to_backfill``, stop here while a backfill runs, so the two
       inference peaks don't stack. The clear stands, which is what keeps the
       branch eligible.
    4. Infer, with no transaction open.
    5. Run the write transaction (``_write_step``). With nothing to infer, this
       runs only if it would stamp or prune, so a stamp is only ever set by
       re-checking the current state, and a no-op takes no lock.

    ``max_embeds`` bounds per-call inference cost; backfill passes None so one
    call fully embeds a branch. ``cap_limit`` bounds the per-exchange token cap
    (see ``cap_for_embedding``) and the batch attention budget (see
    ``embed_batch``). ``replace_stale`` also selects chunks that aren't current
    (see ``_diff_exchanges``).

    Raises on failure, leaving existing chunks and vectors intact. A lock timeout
    is a ``sqlite3.OperationalError`` that ``is_database_locked`` recognizes, and
    a clear that times out means no inference runs for this call. An open
    transaction raises ``sqlite3.ProgrammingError`` rather than being committed.
    """
    if conn.in_transaction:
        # A sqlite3.Error, so backfill aborts rather than marking a healthy branch as a content error.
        raise sqlite3.ProgrammingError("embed_branch_chunks needs no transaction open: it commits on this connection")

    exchanges, chunks = _read_branch_state(conn.cursor(), branch_db_id)
    needing_embed = _diff_exchanges(exchanges, chunks, target_cap=cap_limit, replace_stale=replace_stale)
    if not needing_embed:
        return _finish_without_inference(conn, branch_db_id, exchanges, chunks)

    commit_single_write(conn, "UPDATE branches SET embedding_version = 0 WHERE id = ?", (branch_db_id,))

    if yield_to_backfill:
        backfill = job_lock.probe(JOB_KEY_BACKFILL_EMBEDDINGS)
        if backfill is not None and backfill.running:
            log.info(
                "skipping inference while a backfill runs",
                extra={"branch_id": branch_db_id, "backfill_pid": backfill.pid},
            )
            return EmbedOutcome(embedded=0, stamped=False)

    selected = needing_embed if max_embeds is None else needing_embed[:max_embeds]
    prepared = _prepare_exchange_data(selected, cap_limit=cap_limit)

    # Path-aware cap-exceeded warning: cap_tokens is non-None when
    # cap_for_embedding actually truncated this exchange's text at cap_limit.
    # Scoped to exchanges selected for embedding this call, so an exchange that
    # was capped once but is already embedded doesn't re-warn on every sync.
    for ed in prepared:
        if ed["cap_tokens"] is not None:
            log.warning("exchange truncated for embedding", extra={"cap": cap_limit})

    # Before inference, so an OOM kill's last log line names the branch.
    log.info("embedding branch", extra={"branch_id": branch_db_id, "selected": len(prepared)})
    vecs = embed_batch([ed["text"] for ed in prepared], max_token_cap=cap_limit)
    try:
        return _write_step(conn, branch_db_id, prepared, vecs)
    except sqlite3.OperationalError as exc:
        if is_database_locked(exc):
            log.warning(
                "embed write lost to a lock; inference discarded",
                extra={"branch_id": branch_db_id, "selected": len(prepared)},
            )
        raise
