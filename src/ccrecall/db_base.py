"""Embedding-free DB connection and migration helpers — the base layer under db.py.

Owns SCHEMA_VERSION, the version-gated migration ladder, the base pragmas, and
a connection that opens and migrates the conversation DB without sqlite-vec,
fastembed, onnxruntime, or ccrecall.db. db.py layers the vec-aware surface on
top; keeping that split is what lets a caller migrate the DB without paying the
~1800ms embedding-stack import cost. A test asserts this module imports none of
those, so keep new heavy dependencies out of it.
"""

import logging
import sqlite3
from collections.abc import Callable

from ccrecall.config import ensure_parent_dir, get_db_path
from ccrecall.models import BUSY_TIMEOUT_MS, LOGGER_NAME
from ccrecall.schema import SCHEMA_CORE, SCHEMA_FTS4, SCHEMA_FTS5, detect_fts_support

log = logging.getLogger(LOGGER_NAME)

# Current schema version. Bump when adding a migration and wire the new DDL
# delta into _apply_migrations (see _migrate_to_v1 for the version-1 shape).
SCHEMA_VERSION = 7

V7_BRANCH_COLUMNS = {
    "summary_enrichment_json": "TEXT",
    "summary_enrichment_version": "INTEGER DEFAULT 0",
    "summary_enrichment_source_hash": "TEXT",
    "summary_enrichment_status": "TEXT",
    "summary_enrichment_error": "TEXT",
    "summary_enrichment_updated_at": "DATETIME",
    "summary_source_hash": "TEXT",
}


def apply_base_pragmas(conn: sqlite3.Connection) -> None:
    """Set WAL mode, busy_timeout, and foreign-key enforcement for concurrent-safe access."""
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")


def _recreate_branches_indexes_and_fts(conn: sqlite3.Connection) -> None:
    """Re-create indexes and FTS sync triggers after a branches table rebuild."""
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


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    """Version-1 migration: purge dead branch rows, drop messages_fts, rebuild branches."""
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
          summary_enrichment_json TEXT,
          summary_enrichment_version INTEGER DEFAULT 0,
          summary_enrichment_source_hash TEXT,
          summary_enrichment_status TEXT,
          summary_enrichment_error TEXT,
          summary_enrichment_updated_at DATETIME,
          summary_source_hash TEXT,
          UNIQUE(session_id)
        )
    """)
    conn.execute("""
        INSERT INTO branches_new (
          id, session_id, leaf_uuid, fork_point_uuid, is_active, started_at, ended_at,
          exchange_count, files_modified, commits, tool_counts, aggregated_content,
          context_summary, context_summary_json, summary_version, embedding_version,
          embedding_model, summary_version_at_embed, summary_enrichment_json,
          summary_enrichment_version, summary_enrichment_source_hash,
          summary_enrichment_status, summary_enrichment_error,
          summary_enrichment_updated_at, summary_source_hash
        )
        SELECT
          id, session_id, leaf_uuid, fork_point_uuid, is_active, started_at, ended_at,
          exchange_count, files_modified, commits, tool_counts, aggregated_content,
          context_summary, context_summary_json, summary_version, embedding_version,
          embedding_model, summary_version_at_embed, NULL, 0, NULL, NULL, NULL, NULL, NULL
        FROM branches
    """)
    conn.execute("DROP TABLE branches")
    conn.execute("ALTER TABLE branches_new RENAME TO branches")

    _recreate_branches_indexes_and_fts(conn)


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Version-2 migration: purge orphan messages, drop the dead fork_point_uuid column."""
    conn.execute("DELETE FROM messages WHERE id NOT IN (SELECT DISTINCT message_id FROM branch_messages)")

    conn.execute("""
        CREATE TABLE branches_new (
          id INTEGER PRIMARY KEY,
          session_id INTEGER NOT NULL REFERENCES sessions(id),
          leaf_uuid TEXT NOT NULL,
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
          summary_enrichment_json TEXT,
          summary_enrichment_version INTEGER DEFAULT 0,
          summary_enrichment_source_hash TEXT,
          summary_enrichment_status TEXT,
          summary_enrichment_error TEXT,
          summary_enrichment_updated_at DATETIME,
          summary_source_hash TEXT,
          UNIQUE(session_id)
        )
    """)
    conn.execute("""
        INSERT INTO branches_new (
          id, session_id, leaf_uuid, is_active, started_at, ended_at,
          exchange_count, files_modified, commits, tool_counts,
          aggregated_content, context_summary, context_summary_json,
          summary_version, embedding_version, embedding_model,
          summary_version_at_embed, summary_enrichment_json,
          summary_enrichment_version, summary_enrichment_source_hash,
          summary_enrichment_status, summary_enrichment_error,
          summary_enrichment_updated_at, summary_source_hash
        )
        SELECT
          id, session_id, leaf_uuid, is_active, started_at, ended_at,
          exchange_count, files_modified, commits, tool_counts,
          aggregated_content, context_summary, context_summary_json,
          summary_version, embedding_version, embedding_model,
          summary_version_at_embed, summary_enrichment_json,
          summary_enrichment_version, summary_enrichment_source_hash,
          summary_enrichment_status, summary_enrichment_error,
          summary_enrichment_updated_at, summary_source_hash
        FROM branches
    """)
    conn.execute("DROP TABLE branches")
    conn.execute("ALTER TABLE branches_new RENAME TO branches")

    _recreate_branches_indexes_and_fts(conn)


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """Version-3 migration: add stat-based fast skip columns to import_log."""
    for column, decl in (("file_size", "INTEGER"), ("file_mtime", "REAL")):
        try:
            conn.execute(f"ALTER TABLE import_log ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError as e:  # noqa: PERF203
            if "duplicate column name" not in str(e):
                raise


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """Version-4 migration: add tool_content column and eligibility index to messages."""
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN tool_content TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_tool_content_null ON messages(session_id) WHERE tool_content IS NULL"
    )


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    """Version-5 migration: add the ingestion check cache table."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ingestion_check_cache ("
        "session_uuid TEXT PRIMARY KEY, "
        "source_fingerprint TEXT NOT NULL, "
        "db_coverage_fingerprint TEXT NOT NULL DEFAULT '', "
        "checked_at DATETIME DEFAULT CURRENT_TIMESTAMP"
        ")"
    )


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    """Version-6 migration: add DB coverage fingerprint to ingestion cache."""
    try:
        conn.execute("ALTER TABLE ingestion_check_cache ADD COLUMN db_coverage_fingerprint TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise


def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    """Version-7 migration: add LLM summary enrichment columns to branches."""
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(branches)")}
    if V7_BRANCH_COLUMNS.keys() <= existing_columns:
        return

    for column, decl in V7_BRANCH_COLUMNS.items():
        if column in existing_columns:
            continue
        try:
            conn.execute(f"ALTER TABLE branches ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise


def _apply_migrations(
    conn: sqlite3.Connection,
    *,
    prepare: Callable[[sqlite3.Connection], object] | None = None,
    migrate_to_v1: Callable[[sqlite3.Connection], None] = _migrate_to_v1,
    migrate_to_v2: Callable[[sqlite3.Connection], None] = _migrate_to_v2,
) -> None:
    """Apply version-gated schema migrations up to SCHEMA_VERSION, atomically."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]

    _migrate_to_v3(conn)
    _migrate_to_v4(conn)
    _migrate_to_v5(conn)
    _migrate_to_v6(conn)
    _migrate_to_v7(conn)
    conn.commit()

    if current >= SCHEMA_VERSION:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current < SCHEMA_VERSION:
                # Before any migration, not just v1. Every one of these reshapes
                # a table, and a reshape reparses the schema — which fails with
                # "no such module: vec0" on a database carrying vec0 objects
                # unless the extension is loaded on this connection. A user who
                # has run embeddings has those objects, so gating this on
                # `current < 1` means it never fires again once they are past v1,
                # and the next reshaping migration makes their database
                # unopenable rather than merely unmigrated.
                if prepare is not None:
                    prepare(conn)
                if current < 1:
                    migrate_to_v1(conn)
                if current < 2:
                    migrate_to_v2(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
            if current < SCHEMA_VERSION:
                log.debug("migrated schema from v%d to v%d", current, SCHEMA_VERSION)
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def open_connection(
    settings: dict | None = None,
    *,
    apply_migrations_callback: Callable[[sqlite3.Connection], None] | None = None,
) -> sqlite3.Connection:
    """Open a raw connection with schema initialization and light migrations applied."""
    db_path = get_db_path(settings)
    ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path)
    apply_base_pragmas(conn)

    fts = detect_fts_support(conn)
    conn.executescript(SCHEMA_CORE)
    if fts == "fts5":
        conn.executescript(SCHEMA_FTS5)
    elif fts == "fts4":
        conn.executescript(SCHEMA_FTS4)
    conn.commit()

    (apply_migrations_callback or _apply_migrations)(conn)
    return conn
