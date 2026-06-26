"""Database connection, config/settings, vec (embedding) operations, and logging.

Schema constants live in ccrecall.schema.
"""

import contextlib
import json
import logging
import sqlite3
from logging.handlers import RotatingFileHandler
from pathlib import Path

import sqlite_vec

from ccrecall.embeddings import EMBEDDING_DIM
from ccrecall.models import BUSY_TIMEOUT_MS, LOGGER_NAME
from ccrecall.schema import SCHEMA_CORE, SCHEMA_FTS4, SCHEMA_FTS5, detect_fts_support

# Default paths
DEFAULT_DB_PATH = Path.home() / ".ccrecall" / "conversations.db"
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_LOG_PATH = Path.home() / ".ccrecall" / "ccrecall.log"
CONFIG_PATH = Path.home() / ".ccrecall" / "config.json"

# Hook filenames/prefixes — writer and reader live in different modules and must agree.
CLEAR_HANDOFF_FILENAME = "clear-handoff.json"
SYNC_TEMP_PREFIX = "ccrecall-sync-"

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

# Default settings. Every key here is user-overridable from config.json —
# load_settings() merges any of these present in the file over the defaults.
# (db_path is deliberately absent: it is not a config key. The CLI --db flag
# injects it into the settings dict, which get_db_path reads; without it,
# get_db_path falls back to DEFAULT_DB_PATH. The settings dict thus doubles as
# the transport for that programmatic override.)
DEFAULT_SETTINGS = {
    "auto_inject_context": True,
    "max_context_sessions": 2,
    "exclude_projects": [],
    "logging_enabled": False,
}

CURRENT_ONBOARDING_VERSION = 1

# Vec-loaded connections (concurrent embedding writers) wait longer than the
# base BUSY_TIMEOUT_MS on a collision.
VEC_BUSY_TIMEOUT_MS = 30000

# Rotating memory-log handler sizing.
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 2

# PID sentinel file permissions: owner read/write only.
PID_FILE_MODE = 0o600


def apply_base_pragmas(conn: sqlite3.Connection) -> None:
    """Set WAL mode, busy_timeout, and foreign-key enforcement for concurrent-safe access.

    WAL lets readers and writers proceed without blocking each other; busy_timeout
    waits instead of failing on a writer-writer collision; foreign_keys=ON prevents
    orphaned rows.
    """
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")


def pid_file_path(pid_key: str) -> Path:
    """Path to a background job's PID sentinel (lives beside the DB)."""
    return DEFAULT_DB_PATH.parent / f".pid-{pid_key}"


def remove_pid_file(pid_key: str) -> None:
    """Delete a job's PID sentinel so the next session can spawn again (best-effort)."""
    with contextlib.suppress(OSError):
        pid_file_path(pid_key).unlink(missing_ok=True)


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
    unconditionally dropped (T06 teardown). This is an explicit, idempotent
    DROP … IF EXISTS — NOT routed through the dimension self-heal, which would
    never fire at the unchanged float[512]. When branch_vec was present,
    watermarks are reset to 0: those values referred to the removed branch-level
    embedding mechanism, so zeroing forces backfill to re-embed at chunk grain.
    """
    # ── branch_vec teardown (T06: unconditional, not via dimension self-heal) ─
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


def load_config() -> dict:
    """Read ~/.ccrecall/config.json. Returns empty dict on missing/malformed config."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        result = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # OSError = read failure; ValueError = malformed JSON (JSONDecodeError /
        # UnicodeDecodeError). A real bug surfaces instead of masking as "no config".
        return {}
    return result if isinstance(result, dict) else {}


def load_settings() -> dict:
    """Return settings with config.json overrides merged on top of defaults."""
    settings = DEFAULT_SETTINGS.copy()
    config = load_config()
    for key in DEFAULT_SETTINGS:
        if key in config:
            settings[key] = config[key]
    return settings


def get_db_path(settings: dict | None = None) -> Path:
    """Get database path from settings or default."""
    if settings and "db_path" in settings:
        return Path(settings["db_path"]).expanduser()
    return DEFAULT_DB_PATH


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
    db_path.parent.mkdir(parents=True, exist_ok=True)
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


def setup_logging(settings: dict | None = None) -> logging.Logger:
    """Set up logging with rotation. Returns a null logger if logging is disabled."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()

    if not settings or not settings.get("logging_enabled", False):
        logger.addHandler(logging.NullHandler())
        return logger

    log_path = DEFAULT_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    return logger


def log_hook_exception(context: str) -> None:
    """Best-effort: route the active exception to the memory log without ever raising.

    Top-level hook guards must never crash the session, but a bare ``except: pass``
    also hides every failure. This logs the in-flight exception (a no-op unless
    logging_enabled) while suppressing any error from logging itself, so the guard
    stays crash-proof and failures become observable when logging is turned on.
    """
    with contextlib.suppress(Exception):
        setup_logging(load_settings()).exception("%s hook failed", context)
