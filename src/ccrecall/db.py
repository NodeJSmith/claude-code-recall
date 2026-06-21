"""Database connection, config/settings, vec (embedding) operations, and logging.

Schema constants live in ccrecall.schema; migrations in ccrecall.migrations.
"""

import contextlib
import json
import logging
import sqlite3
from logging.handlers import RotatingFileHandler
from pathlib import Path

import sqlite_vec

from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.migrations import migrate_columns, migrate_db
from ccrecall.models import BUSY_TIMEOUT_MS, LOGGER_NAME
from ccrecall.schema import SCHEMA_CORE, SCHEMA_FTS4, SCHEMA_FTS5, detect_fts_support

# Default paths
DEFAULT_DB_PATH = Path.home() / ".claude-memory" / "conversations.db"
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_LOG_PATH = Path.home() / ".claude-memory" / "memory.log"
CONFIG_PATH = Path.home() / ".claude-memory" / "config.json"

# Hook filenames/prefixes — writer and reader live in different modules and must agree.
CLEAR_HANDOFF_FILENAME = "clear-handoff.json"
SYNC_TEMP_PREFIX = "claude-memory-sync-"

# Shared SQL predicate for "branches that are candidates to embed": active
# leaves (the query path only returns is_active=1) with a usable summary. This
# is the single source of truth for the embedding universe — build_selection()
# (eligibility), count_status() (backfill progress), and search_conversations
# print_status() (diagnostics) all build on it so their counts can't drift.
EMBEDDABLE_BRANCH_FILTER = "is_active = 1 AND context_summary IS NOT NULL AND context_summary != ''"
# Sentinel written to a branch's embedding_version or summary_version when its
# content can't be embedded or summarized (tokenizer overflow, malformed content).
# Excluded from eligibility so it isn't retried forever; counted separately as
# "errored".
CONTENT_ERROR_VERSION = -1

# Default settings
DEFAULT_SETTINGS = {
    "db_path": str(DEFAULT_DB_PATH),
    "auto_inject_context": True,
    "max_context_sessions": 2,
    "exclude_projects": [],
    "logging_enabled": False,
    "sync_on_stop": True,
}

# Keys in config.json that override DEFAULT_SETTINGS
_CONFIG_KEYS = {
    "auto_inject_context",
    "max_context_sessions",
}

CURRENT_ONBOARDING_VERSION = 1

# Vec-loaded connections (concurrent embedding writers) wait longer than the
# base BUSY_TIMEOUT_MS on a collision.
VEC_BUSY_TIMEOUT_MS = 30000

# Rotating memory-log handler sizing.
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 2


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


def branch_vec_queryable(conn: sqlite3.Connection) -> bool:
    """Return True iff the branch_vec virtual table exists and is queryable.

    get_db_connection(load_vec=True) only creates branch_vec when sqlite-vec
    loaded successfully, so a guarded probe is the cheapest way for the write,
    query, and backfill paths to learn whether vector persistence is available
    before spending inference or running branch_vec queries.

    Scoped to sqlite3.Error (the table-missing OperationalError is the expected
    failure) so a non-DB bug — e.g. a bad connection object — still surfaces.
    """
    try:
        conn.execute("SELECT 1 FROM branch_vec LIMIT 1")
        return True
    except sqlite3.Error:
        return False


def upsert_branch_vec(cursor: sqlite3.Cursor, branch_id: int, embedding: list[float]) -> None:
    """Replace a branch's vector row (DELETE+INSERT — vec0 rejects INSERT OR REPLACE)."""
    cursor.execute("DELETE FROM branch_vec WHERE branch_id = ?", (branch_id,))
    cursor.execute(
        "INSERT INTO branch_vec(branch_id, embedding) VALUES (?, ?)",
        (branch_id, sqlite_vec.serialize_float32(embedding)),
    )


def write_branch_embedding(
    cursor: sqlite3.Cursor, branch_id: int, embedding: list[float], summary_version: int
) -> None:
    """Persist a branch's embedding: vector upsert FIRST, version columns LAST (order is load-bearing)."""
    upsert_branch_vec(cursor, branch_id, embedding)
    cursor.execute(
        "UPDATE branches SET embedding_version = ?, embedding_model = ?, summary_version_at_embed = ? WHERE id = ?",
        (EMBEDDING_VERSION, EMBEDDING_MODEL, summary_version, branch_id),
    )


def fetch_branch_messages(cursor: sqlite3.Cursor, branch_id: int, include_notifications: bool) -> list[dict]:
    """Return a branch's messages ordered by timestamp; notifications included only when asked."""
    cursor.execute(
        """
        SELECT m.role, m.content, m.timestamp, COALESCE(m.is_notification, 0) as is_notification
        FROM branch_messages bm
        JOIN messages m ON bm.message_id = m.id
        WHERE bm.branch_id = ?
          AND (? OR COALESCE(m.is_notification, 0) = 0)
        ORDER BY m.timestamp ASC
        """,
        (branch_id, include_notifications),
    )
    return [{"role": r, "content": c, "timestamp": t, "is_notification": notif} for r, c, t, notif in cursor.fetchall()]


def _ensure_vec_schema(conn: sqlite3.Connection) -> None:
    """Create the branch_vec virtual table and orphan-cleanup trigger.

    Caller is responsible for loading the sqlite-vec extension before calling
    this function (via vec_available or equivalent). Does not load the
    extension itself and does not commit — the caller manages the transaction.

    Self-heals a stale embedding dimension: if branch_vec already exists at a
    different float[N] than the current EMBEDDING_DIM (e.g. after an embedding
    model swap), it is dropped and recreated. branch_vec holds only derived
    vectors, so dropping is lossless — the backfill heal clause and embed-on-
    write repopulate it at the new dimension.
    """
    # sqlite_master stores the vec0 CREATE statement verbatim, so a substring
    # check for the current float[N] reliably detects a stale dimension. Lowercase
    # both sides so a hand-created FLOAT[...] table still compares correctly.
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='branch_vec'").fetchone()
    if row and f"float[{EMBEDDING_DIM}]" not in row[0].lower():
        # Drop the trigger first: SQLite does not cascade-drop a trigger when its
        # target table is dropped, so a surviving branches_vec_ad would fire
        # against a missing branch_vec. (DROP TABLE works on virtual tables.)
        conn.execute("DROP TRIGGER IF EXISTS branches_vec_ad")
        conn.execute("DROP TABLE branch_vec")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS branch_vec"
        f" USING vec0(branch_id INTEGER PRIMARY KEY, embedding float[{EMBEDDING_DIM}])"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS branches_vec_ad"
        " AFTER DELETE ON branches"
        " BEGIN DELETE FROM branch_vec WHERE branch_id = OLD.id; END"
    )


def load_config() -> dict:
    """Read ~/.claude-memory/config.json. Returns empty dict on missing/malformed config."""
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
    for key in _CONFIG_KEYS:
        if key in config:
            settings[key] = config[key]
    return settings


def get_db_path(settings: dict | None = None) -> Path:
    """Get database path from settings or default."""
    if settings and "db_path" in settings:
        return Path(settings["db_path"]).expanduser()
    return DEFAULT_DB_PATH


def get_db_connection(settings: dict | None = None, load_vec: bool = False) -> sqlite3.Connection:
    """Get database connection, initializing schema and running migrations if needed.

    Uses the settings-based db_path when provided and applies the base pragmas for
    concurrent-safe access. When ``load_vec`` is True, loads the sqlite-vec extension
    and raises busy_timeout to VEC_BUSY_TIMEOUT_MS — use it for connections that query
    or write branch_vec (search, write path, backfill). Default False keeps the
    extension unloaded, cheaper for recent-chats, token analytics, and setup paths
    that never touch branch_vec.
    """
    db_path = get_db_path(settings)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    apply_base_pragmas(conn)

    # Check if migration needed (old schema -> v3)
    migrated = migrate_db(conn)
    if migrated:
        # Connection was closed during migration, reconnect
        conn = sqlite3.connect(db_path)
        apply_base_pragmas(conn)

    if not migrated:
        # Apply schema (handles fresh databases, idempotent)
        fts = detect_fts_support(conn)
        conn.executescript(SCHEMA_CORE)
        if fts == "fts5":
            conn.executescript(SCHEMA_FTS5)
        elif fts == "fts4":
            conn.executescript(SCHEMA_FTS4)
        conn.commit()

    # Add any missing columns and run versioned data migrations (v1-v6).
    migrate_columns(conn)

    if load_vec and vec_available(conn):
        # First and only place the vec extension is loaded for this connection.
        # _ensure_vec_schema creates branch_vec + trigger, then we commit and
        # raise busy_timeout for concurrent vec writers.
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
