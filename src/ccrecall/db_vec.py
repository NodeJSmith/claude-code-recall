"""sqlite-vec (embedding) operations split out of ccrecall.db.

Isolated here so ``ccrecall.db.get_connection`` is importable without pulling
numpy/fastembed/onnxruntime onto the hook hot path (see CLAUDE.md invariant
#3). Anything that needs the vec extension, chunk vectors, or the embedding
constants belongs in this module; ``db.py`` stays vec-free.
"""

import contextlib
import logging
import sqlite3

import sqlite_vec

from ccrecall.db import CHUNK_DRAFT_QUALITY_FILTER, CHUNK_EMBEDDABLE_BRANCH_FILTER
from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION, MODEL_TOKEN_LIMIT
from ccrecall.models import LOGGER_NAME

log = logging.getLogger(LOGGER_NAME)

# Trigger names created by _ensure_vec_schema. Exposed as constants so callers
# that need to probe for the cascade (e.g. import_session's empty-session
# cleanup) stay in sync with the definition site.
TRIGGER_CHUNKS_VEC_AD = "chunks_vec_ad"

# Vec-loaded connections (concurrent embedding writers) wait longer than the
# base BUSY_TIMEOUT_MS on a collision.
VEC_BUSY_TIMEOUT_MS = 30000


def vec_available(conn: sqlite3.Connection) -> bool:
    """Return True iff the sqlite-vec extension can be loaded on this connection.

    On success, disables the SQL load_extension() surface after loading so the
    vec0 module stays registered (queryable) but `load_extension()` from SQL is
    no longer callable — closes a latent injection surface.

    Catches broadly (except Exception, NOT a narrow sqlite3.*) because
    enable_load_extension raises AttributeError on Python builds compiled
    without loadable-extension support — not a sqlite3.OperationalError.
    """
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        log.warning("sqlite-vec extension failed to load", exc_info=True)
        # Re-disable on the failure path too, so a partially-enabled connection
        # doesn't leave the load_extension() SQL surface callable. Suppressed
        # because enable_load_extension itself may be what raised (e.g. builds
        # without loadable-extension support raise AttributeError).
        with contextlib.suppress(Exception):
            conn.enable_load_extension(False)
        return False


def chunk_vec_queryable(conn: sqlite3.Connection) -> bool:
    """Return True iff the chunk_vec virtual table exists and is queryable.

    Used by the write, query, and backfill paths to guard chunk-vector
    operations (the chunk-grain successor to the removed branch_vec probe).

    Scoped to sqlite3.Error so a non-DB bug still surfaces.
    """
    try:
        conn.execute("SELECT 1 FROM chunk_vec LIMIT 1")
        return True
    except sqlite3.Error:
        log.debug("chunk_vec table not queryable", exc_info=True)
        return False


def upsert_chunk_vec(cursor: sqlite3.Cursor, chunk_id: int, embedding: list[float]) -> None:
    """Replace a chunk's vector row (DELETE+INSERT — vec0 rejects INSERT OR REPLACE)."""
    cursor.execute("DELETE FROM chunk_vec WHERE chunk_id = ?", (chunk_id,))
    cursor.execute(
        "INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, sqlite_vec.serialize_float32(embedding)),
    )


def write_chunk_embedding(
    cursor: sqlite3.Cursor,
    chunk_id: int,
    embedding: list[float],
    embedding_version: int,
    embedding_model: str,
) -> None:
    """Persist a chunk's embedding: vector upsert FIRST, version columns LAST (order is load-bearing).

    The chunk row is created by the caller before this is called; this helper
    only writes the vector and bookkeeping columns.
    """
    upsert_chunk_vec(cursor, chunk_id, embedding)
    cursor.execute(
        "UPDATE chunks SET embedding_version = ?, embedding_model = ? WHERE id = ?",
        (embedding_version, embedding_model, chunk_id),
    )


def fetch_branch_messages(cursor: sqlite3.Cursor, branch_id: int, include_notifications: bool) -> list[dict]:
    """Return a branch's messages ordered by timestamp; notifications included only when asked."""
    cursor.execute(
        """
        SELECT m.role, m.content, m.timestamp, COALESCE(m.is_notification, 0) as is_notification, m.uuid,
               m.tool_content
        FROM branch_messages bm
        JOIN messages m ON bm.message_id = m.id
        WHERE bm.branch_id = ?
          AND (? OR COALESCE(m.is_notification, 0) = 0)
        ORDER BY m.timestamp ASC
        """,
        (branch_id, include_notifications),
    )
    return [
        {"role": r, "content": c, "timestamp": t, "is_notification": notif, "uuid": uuid, "tool_content": tc}
        for r, c, t, notif, uuid, tc in cursor.fetchall()
    ]


def _ensure_vec_schema(conn: sqlite3.Connection) -> None:
    """Create vec0 virtual tables and cascade triggers for chunk vectors.

    Caller is responsible for loading the sqlite-vec extension before calling
    this function (via vec_available or equivalent). Does not load the
    extension itself and does not commit — the caller manages the transaction.

    Self-heals a stale embedding dimension for chunk_vec: if the table exists
    at a different float[N] than the current EMBEDDING_DIM (e.g. after an
    embedding model swap), it is dropped and recreated. chunk_vec holds only
    derived vectors, so dropping is lossless — the backfill heal clause and
    embed-on-write repopulate them at the new dimension.

    When chunk_vec is dropped (stale dimension), all branch watermarks are also
    reset to 0 so backfill repopulates the missing vectors. Without the reset,
    watermarks would still read EMBEDDING_VERSION while the vectors are gone.

    The obsolete branch_vec table and its branches_vec_ad trigger are
    unconditionally dropped. This is an explicit, idempotent
    DROP … IF EXISTS — NOT routed through the dimension self-heal, which would
    never fire at the unchanged float[512]. When branch_vec was present,
    watermarks are reset to 0: those values referred to the removed branch-level
    embedding mechanism, so zeroing forces backfill to re-embed at chunk grain.
    """
    # ── branch_vec teardown (unconditional, not via dimension self-heal) ─
    # Check first so the watermark reset fires only when the table actually existed.
    branch_vec_existed = (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='branch_vec'").fetchone() is not None
    )
    conn.execute("DROP TRIGGER IF EXISTS branches_vec_ad")
    conn.execute("DROP TABLE IF EXISTS branch_vec")
    if branch_vec_existed:
        # Old embedding_version values referred to branch-level embeddings (now
        # removed); reset to 0 so backfill re-embeds at chunk grain from scratch.
        conn.execute("UPDATE branches SET embedding_version = 0")

    # ── chunk_vec self-heal ──────────────────────────────────────────────────
    # sqlite_master stores the vec0 CREATE statement verbatim, so a substring
    # check for the current float[N] reliably detects a stale dimension.
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='chunk_vec'").fetchone()
    if row and f"float[{EMBEDDING_DIM}]" not in row[0].lower():
        # Drop the trigger first: SQLite does not cascade-drop a trigger when its
        # target table is dropped, so a surviving chunks_vec_ad would fire against
        # a missing chunk_vec. (DROP TABLE works on virtual tables.)
        conn.execute(f"DROP TRIGGER IF EXISTS {TRIGGER_CHUNKS_VEC_AD}")
        conn.execute("DROP TABLE chunk_vec")
        # Reset branch watermarks: chunk_vec drop leaves branches reporting
        # EMBEDDING_VERSION while their vectors are gone; zero forces backfill
        # to repopulate.
        conn.execute("UPDATE branches SET embedding_version = 0")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec"
        f" USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{EMBEDDING_DIM}])"
    )

    # ── cascade triggers ─────────────────────────────────────────────────────
    # Two-level chain: branches → chunks → chunk_vec
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS branches_chunks_ad"
        " AFTER DELETE ON branches"
        " BEGIN DELETE FROM chunks WHERE branch_id = OLD.id; END"
    )
    conn.execute(
        f"CREATE TRIGGER IF NOT EXISTS {TRIGGER_CHUNKS_VEC_AD}"
        " AFTER DELETE ON chunks"
        " BEGIN DELETE FROM chunk_vec WHERE chunk_id = OLD.id; END"
    )


def ensure_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec and create vec schema. Returns True if vec is available."""
    if vec_available(conn):
        _ensure_vec_schema(conn)
        conn.commit()
        conn.execute(f"PRAGMA busy_timeout = {VEC_BUSY_TIMEOUT_MS}")
        return True
    return False


def branch_embedding_coverage(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Return (embedded_full, embedded_draft, total) embeddable branches.

    `total` is every CHUNK_EMBEDDABLE branch. `embedded_full` is those whose
    watermark (embedding_version/embedding_model) is at the current version
    and model — the write path's clear-first/set-last protocol
    (embed_ops.py:_should_stamp_watermark) deliberately withholds that
    watermark from a branch that still carries a draft-quality (capped) chunk
    (design/specs/015 FR#14), so a branch never reaches embedded_full while
    any of its chunks are below FULL_QUALITY_TOKEN_LIMIT. `embedded_draft` is
    the remainder of the non-full branches that DO have at least one chunk —
    i.e. searchable now, just not at full fidelity; a branch that has neither
    a current watermark nor any capped chunk (no chunk at all yet) counts in
    neither embedded_full nor embedded_draft.

    Vec-free — reads only the branches/chunks tables, whose relevant columns
    live in the base schema — so coverage reports work even where sqlite-vec
    can't load. Shared by `ccrecall status`, `compute_caveat`, and search's
    `print_status` so the surfaces can't drift (see CHUNK_EMBEDDABLE_BRANCH_FILTER).

    This is the watermark view. `backfill embeddings --status` reports a
    stricter, heal-aware count (its eligible set also flags watermark-current
    branches that lost a chunk_vec row), so on a DB with orphaned vectors that
    surface can show slightly fewer embedded branches than this one.
    """
    total = conn.execute(f"SELECT COUNT(*) FROM branches WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}").fetchone()[0]
    embedded_full = conn.execute(
        f"SELECT COUNT(*) FROM branches WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER} "
        "AND embedding_version = ? AND embedding_model = ?",
        (EMBEDDING_VERSION, EMBEDDING_MODEL),
    ).fetchone()[0]
    embedded_draft = conn.execute(
        f"SELECT COUNT(*) FROM branches WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER} "
        "AND NOT (embedding_version = ? AND embedding_model = ?) "
        "AND EXISTS (SELECT 1 FROM chunks WHERE chunks.branch_id = branches.id "
        f"AND {CHUNK_DRAFT_QUALITY_FILTER})",
        (EMBEDDING_VERSION, EMBEDDING_MODEL, MODEL_TOKEN_LIMIT),
    ).fetchone()[0]
    return embedded_full, embedded_draft, total
