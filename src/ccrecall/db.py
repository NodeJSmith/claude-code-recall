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

# Current schema version. Bump when adding a migration and wire the new DDL
# delta into _apply_migrations (see _migrate_to_v1 for the version-1 shape).
SCHEMA_VERSION = 1

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


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """Version-1 migration: purge dead branch rows, drop messages_fts, rebuild branches.

    Runs inside the caller's BEGIN IMMEDIATE transaction (_apply_migrations).

    1. Delete `is_active = 0` branches and dependents in FK-safe order
       (branch_messages, then chunks — the vec cascade trigger, when present,
       handles chunk_vec — then branches) *before* the rebuild in step 3, or
       leftover duplicate session_id values would violate its new UNIQUE.
    2. Drop `messages_fts` and its messages_ai/ad/au triggers. A real upgrade
       DB may still carry these triggers even though their definitions are
       already gone from schema.py; a dangling trigger referencing the
       just-dropped table breaks the ALTER TABLE RENAME in step 3 (SQLite
       revalidates every trigger body during a rename).
    3. Rebuild `branches` to change `UNIQUE(session_id, leaf_uuid)` to
       `UNIQUE(session_id)` — SQLite has no DROP CONSTRAINT, so this is the
       standard create-copy-drop-rename sequence. That drop also takes every
       trigger and index defined on the old table with it — including the
       live `branches_fts` sync triggers and the `idx_branches_*` indexes —
       so both are re-created below via individual `conn.execute` calls, not
       `conn.executescript` (which implicitly commits, ending this
       transaction early and leaving the final version bump with nothing to
       commit). `branches_fts` content itself is untouched — ids survive the
       `INSERT INTO branches_new SELECT * FROM branches` copy — only its
       sync triggers need re-creating.

    Table rebuilds where another table holds a foreign key to the table being
    dropped require `PRAGMA foreign_keys = OFF` for the whole connection —
    the caller (_apply_migrations) sets this outside any transaction, since
    toggling it here would be a no-op once a transaction is open.
    """
    conn.execute("DELETE FROM branch_messages WHERE branch_id IN (SELECT id FROM branches WHERE is_active = 0)")
    conn.execute("DELETE FROM chunks WHERE branch_id IN (SELECT id FROM branches WHERE is_active = 0)")
    conn.execute("DELETE FROM branches WHERE is_active = 0")

    conn.execute("DROP TRIGGER IF EXISTS messages_ai")
    conn.execute("DROP TRIGGER IF EXISTS messages_ad")
    conn.execute("DROP TRIGGER IF EXISTS messages_au")
    conn.execute("DROP TABLE IF EXISTS messages_fts")

    conn.execute("""
        CREATE TABLE branches_new (
          id INTEGER PRIMARY KEY,
          session_id INTEGER NOT NULL REFERENCES sessions(id),
          leaf_uuid TEXT NOT NULL,
          fork_point_uuid TEXT,
          is_active INTEGER DEFAULT 1,
          started_at DATETIME,
          ended_at DATETIME,
          exchange_count INTEGER DEFAULT 0,
          files_modified TEXT,
          commits TEXT,
          tool_counts TEXT,
          aggregated_content TEXT,
          context_summary TEXT,
          context_summary_json TEXT,
          summary_version INTEGER DEFAULT 0,
          embedding_version INTEGER DEFAULT 0,
          embedding_model TEXT,
          summary_version_at_embed INTEGER,
          UNIQUE(session_id)
        )
    """)
    conn.execute("INSERT INTO branches_new SELECT * FROM branches")
    conn.execute("DROP TABLE branches")
    conn.execute("ALTER TABLE branches_new RENAME TO branches")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_branches_session ON branches(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_branches_active ON branches(is_active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_branches_summary_version ON branches(summary_version)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_branches_embedding_version ON branches(embedding_version)")

    fts = detect_fts_support(conn)
    if fts == "fts5":
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS branches_fts USING fts5("
            "aggregated_content, content=branches, content_rowid=id, tokenize='porter unicode61')"
        )
    elif fts == "fts4":
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS branches_fts USING fts4("
            "aggregated_content, content=branches, tokenize=porter)"
        )
    if fts in ("fts5", "fts4"):
        # Trigger bodies are identical across the FTS5/FTS4 variants (only the
        # CREATE VIRTUAL TABLE statement above differs) — one copy suffices.
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS branches_ai AFTER INSERT ON branches BEGIN"
            " INSERT INTO branches_fts(rowid, aggregated_content) VALUES (new.id, new.aggregated_content); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS branches_ad AFTER DELETE ON branches BEGIN"
            " INSERT INTO branches_fts(branches_fts, rowid, aggregated_content)"
            " VALUES('delete', old.id, old.aggregated_content); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS branches_au AFTER UPDATE ON branches BEGIN"
            " INSERT INTO branches_fts(branches_fts, rowid, aggregated_content)"
            " VALUES('delete', old.id, old.aggregated_content);"
            " INSERT INTO branches_fts(rowid, aggregated_content) VALUES (new.id, new.aggregated_content); END"
        )


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply version-gated schema migrations up to SCHEMA_VERSION, atomically.

    Must run after SCHEMA_CORE/FTS creation (their CREATE TABLE/TRIGGER IF NOT
    EXISTS statements are always safe to re-run) and before _ensure_vec_schema
    — the vec self-heal checks stay outside the version gate, running on
    every vec-loaded connection regardless of schema version.

    On a fresh install user_version starts at 0 and every migration DML step
    is a no-op against the schema just created. On failure the whole thing
    rolls back, user_version stays at its prior value, and the next
    connection retries from scratch — every step is written to tolerate that.

    A real upgrade DB (any installation with prior chunk-embedding activity)
    already carries the chunks_vec_ad cascade trigger on disk regardless of
    whether *this* connection asked for load_vec — CREATE TRIGGER persists
    independently of which connection loaded the vec0 module. _migrate_to_v1's
    purge deletes from branches/chunks, which fires that trigger's
    ``DELETE FROM chunk_vec``; without vec0 registered on this connection that
    raises ``sqlite3.OperationalError: no such module: vec0`` and crashes the
    (usually load_vec=False) caller. Loading vec here — before the purge, and
    only once migration is confirmed necessary — registers vec0 so the cascade
    can fire, with the side benefit of correctly purging the now-orphaned
    chunk_vec rows for deleted chunks instead of erroring. A no-op when
    sqlite-vec isn't installed/loadable at all (vec_available fails closed),
    which matches the case where chunk_vec/its cascade trigger were never
    created in the first place.

    foreign_keys is toggled OFF for the duration: the branches rebuild in
    _migrate_to_v1 drops and recreates a table that branch_messages and
    chunks reference via foreign key, and SQLite only allows that when
    enforcement is disabled *outside* any transaction (toggling mid-
    transaction is a no-op, and leaving it on fails the COMMIT with a
    foreign-key error even though ``PRAGMA foreign_key_check`` reports no
    violations — see sqlite.org/lang_altertable.html). Restored in
    ``finally`` on every path so foreign_keys is ON whenever this returns.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read under the write lock: another connection may have raced
            # this one to BEGIN IMMEDIATE and already committed the migration
            # while this connection was blocked waiting for the lock — the
            # `current` read above happened before the lock was held.
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current < SCHEMA_VERSION:
                if current < 1:
                    # Load vec only now that migration is confirmed necessary
                    # under the lock — enable_load_extension/sqlite_vec.load
                    # are C-API calls, not SQL, so they are unaffected by
                    # transaction state and safe to call here.
                    vec_available(conn)
                    _migrate_to_v1(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _open_connection(settings: dict | None = None, load_vec: bool = False) -> sqlite3.Connection:
    """Open a raw database connection, initializing the schema on first use (idempotent).

    Uses the settings-based db_path when provided and applies the base pragmas for
    concurrent-safe access. When ``load_vec`` is True, loads the sqlite-vec extension
    and raises busy_timeout to VEC_BUSY_TIMEOUT_MS — use it for connections that query
    or write chunk_vec (search, write path, backfill). Default False keeps
    the extension unloaded, cheaper for recent-chats and setup paths that never
    touch the vec tables.

    Callers should not use this directly — use ``get_connection`` (the
    context-manager wrapper) instead, which guarantees the connection is
    committed on success, rolled back on exception, and always closed.
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

    # Version-gated schema migrations run after the idempotent schema creation
    # above (tables must exist before migration DML runs) and before the vec
    # self-heal below (which is not version-gated — see _apply_migrations).
    _apply_migrations(conn)

    if load_vec and vec_available(conn):
        # First and only place the vec extension is loaded for this connection.
        # _ensure_vec_schema tears down the obsolete branch_vec, creates chunk_vec
        # and its cascade triggers, then we commit and raise busy_timeout for
        # concurrent vec writers.
        _ensure_vec_schema(conn)
        conn.commit()
        conn.execute(f"PRAGMA busy_timeout = {VEC_BUSY_TIMEOUT_MS}")

    return conn


@contextlib.contextmanager
def get_connection(settings: dict | None = None, load_vec: bool = False):
    """Get a database connection as a context manager: commit-on-success, rollback-on-exception, always-close.

    Use as ``with get_connection(settings) as conn:``. On normal exit from the
    ``with`` block, the connection is committed then closed. On an exception
    propagating out of the block, the connection is rolled back (so partial
    work isn't silently persisted) then closed, and the exception re-raises.
    Either way the connection is guaranteed to be closed — this replaces the
    old raw-connection pattern (``conn = _open_connection(...)``, previously
    a public helper of the same name) that leaked the connection whenever the
    caller's work raised before reaching an explicit ``conn.close()``.
    """
    conn = _open_connection(settings, load_vec)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
