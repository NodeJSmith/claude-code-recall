"""Database connection, vec (embedding) operations, and schema-adjacent utilities.

Schema constants live in ccrecall.schema. Paths, config/settings loading, PID
files, and logging live in ccrecall.config — imported below for this module's
own use (get_db_path, ensure_parent_dir, DEFAULT_DB_PATH, ...).
"""

import contextlib
import sqlite3
from pathlib import Path

import sqlite_vec

from ccrecall.config import DEFAULT_DB_PATH, ensure_parent_dir, get_db_path
from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.models import BUSY_TIMEOUT_MS
from ccrecall.schema import SCHEMA_CORE, SCHEMA_FTS4, SCHEMA_FTS5, detect_fts_support

# Default paths
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Shared SQL predicate for "branches that are candidates to embed": active
# leaves (the query path only returns is_active=1) with a usable summary. This
# is the single source of truth for the embedding universe — build_selection()
# (eligibility), count_status() (backfill progress), and search_conversations
# print_status() (diagnostics) all build on it so their counts can't drift.
EMBEDDABLE_BRANCH_FILTER = "is_active = 1 AND context_summary IS NOT NULL AND context_summary != ''"
# Chunk-path universe: active leaf with at least one message. Wider than
# EMBEDDABLE_BRANCH_FILTER because chunk embedding reads raw exchange text, not
# the summary — branches with NULL context_summary still have embeddable content.
# Keep EMBEDDABLE_BRANCH_FILTER for any summary-dependent caller; don't remove it.
CHUNK_EMBEDDABLE_BRANCH_FILTER = "is_active = 1 AND EXISTS(SELECT 1 FROM branch_messages WHERE branch_id = branches.id)"
# Sentinel written to a branch's embedding_version or summary_version when its
# content can't be embedded or summarized (tokenizer overflow, malformed content).
# Excluded from eligibility so it isn't retried forever; counted separately as
# "errored".
CONTENT_ERROR_VERSION = -1

# Vec-loaded connections (concurrent embedding writers) wait longer than the
# base BUSY_TIMEOUT_MS on a collision.
VEC_BUSY_TIMEOUT_MS = 30000


def apply_base_pragmas(conn: sqlite3.Connection) -> None:
    """Set WAL mode, busy_timeout, and foreign-key enforcement for concurrent-safe access.

    WAL lets readers and writers proceed without blocking each other; busy_timeout
    waits instead of failing on a writer-writer collision; foreign_keys=ON prevents
    orphaned rows.
    """
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")


def escape_like(value: str) -> str:
    """Escape SQLite LIKE wildcards so a user value matches literally (pair with ESCAPE '\\')."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_project_filter(project: str | None) -> list[str] | None:
    """Split a comma-separated --project value into a stripped list (None if unset).

    Shared by the search and recent CLI paths so the parsing can't drift.
    """
    return [p.strip() for p in project.split(",")] if project else None


def resolve_db_settings(db: Path) -> dict | None:
    """Build the settings dict carrying a non-default --db path (None for the default).

    Shared by the search and recent CLI paths so the override transport stays single-sourced.
    """
    return {"db_path": str(db)} if db != DEFAULT_DB_PATH else None


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
        SELECT m.role, m.content, m.timestamp, COALESCE(m.is_notification, 0) as is_notification, m.uuid
        FROM branch_messages bm
        JOIN messages m ON bm.message_id = m.id
        WHERE bm.branch_id = ?
          AND (? OR COALESCE(m.is_notification, 0) = 0)
        ORDER BY m.timestamp ASC
        """,
        (branch_id, include_notifications),
    )
    return [
        {"role": r, "content": c, "timestamp": t, "is_notification": notif, "uuid": uuid}
        for r, c, t, notif, uuid in cursor.fetchall()
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
    bv_existed = (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='branch_vec'").fetchone() is not None
    )
    conn.execute("DROP TRIGGER IF EXISTS branches_vec_ad")
    conn.execute("DROP TABLE IF EXISTS branch_vec")
    if bv_existed:
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
        conn.execute("DROP TRIGGER IF EXISTS chunks_vec_ad")
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
        "CREATE TRIGGER IF NOT EXISTS chunks_vec_ad"
        " AFTER DELETE ON chunks"
        " BEGIN DELETE FROM chunk_vec WHERE chunk_id = OLD.id; END"
    )


def get_db_connection(settings: dict | None = None, load_vec: bool = False) -> sqlite3.Connection:
    """Get database connection, initializing the schema on first use (idempotent).

    Uses the settings-based db_path when provided and applies the base pragmas for
    concurrent-safe access. When ``load_vec`` is True, loads the sqlite-vec extension
    and raises busy_timeout to VEC_BUSY_TIMEOUT_MS — use it for connections that query
    or write chunk_vec (search, write path, backfill). Default False keeps
    the extension unloaded, cheaper for recent-chats, token analytics, and setup paths
    that never touch the vec tables.
    """
    db_path = get_db_path(settings)
    ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path)
    apply_base_pragmas(conn)

    # Apply schema (handles fresh databases and existing ones idempotently).
    fts = detect_fts_support(conn)
    conn.executescript(SCHEMA_CORE)
    if fts == "fts5":
        conn.executescript(SCHEMA_FTS5)
    elif fts == "fts4":
        conn.executescript(SCHEMA_FTS4)
    conn.commit()

    if load_vec and vec_available(conn):
        # First and only place the vec extension is loaded for this connection.
        # _ensure_vec_schema tears down the obsolete branch_vec, creates chunk_vec
        # and its cascade triggers, then we commit and raise busy_timeout for
        # concurrent vec writers.
        _ensure_vec_schema(conn)
        conn.commit()
        conn.execute(f"PRAGMA busy_timeout = {VEC_BUSY_TIMEOUT_MS}")

    return conn


def branch_embedding_coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return (embedded, total) embeddable branches by watermark.

    `total` is every CHUNK_EMBEDDABLE branch; `embedded` is those whose
    watermark (embedding_version/embedding_model) is at the current version and
    model. Vec-free — reads only the branches table, whose embedding columns
    live in the base schema — so coverage reports work even where sqlite-vec
    can't load. Shared by `ccrecall stats` and search's `print_status` so the
    two surfaces can't drift (see CHUNK_EMBEDDABLE_BRANCH_FILTER).

    This is the watermark view. `backfill embeddings --status` reports a
    stricter, heal-aware count (its eligible set also flags watermark-current
    branches that lost a chunk_vec row), so on a DB with orphaned vectors that
    surface can show slightly fewer embedded branches than this one.
    """
    total = conn.execute(f"SELECT COUNT(*) FROM branches WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}").fetchone()[0]
    embedded = conn.execute(
        f"SELECT COUNT(*) FROM branches WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER} "
        "AND embedding_version = ? AND embedding_model = ?",
        (EMBEDDING_VERSION, EMBEDDING_MODEL),
    ).fetchone()[0]
    return embedded, total
