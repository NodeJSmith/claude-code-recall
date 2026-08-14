"""Embedding-free DB connection and migration helpers for LLM summary workers.

This module intentionally avoids sqlite-vec, fastembed, onnxruntime, and
ccrecall.db so detached LLM-summary workers can open and migrate the
conversation DB without pulling in embedding dependencies.
"""

import contextlib
import sqlite3
from collections.abc import Callable
from urllib.parse import quote

from ccrecall.config import ensure_parent_dir, get_db_path
from ccrecall.models import BUSY_TIMEOUT_MS
from ccrecall.schema import SCHEMA_CORE, SCHEMA_FTS4, SCHEMA_FTS5, detect_fts_support

# Current schema version. Bump when adding a migration and wire the new DDL
# delta into _apply_migrations (see _migrate_to_v1 for the version-1 shape).
SCHEMA_VERSION = 9

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


RECAP_TABLES = {
    "session_recap_jobs",
    "session_recap_attempts",
    "session_recap_runtime",
    "session_recap_provider_health",
    "session_recap_runs",
    "session_recap_run_candidates",
    "session_recap_quarantine",
}
RECAP_INDEXES = {
    "idx_recap_jobs_ready",
    "idx_recap_jobs_lease",
    "idx_recap_attempts_job_latest",
    "idx_recap_attempts_input",
    "idx_recap_attempts_status",
    "idx_recap_attempts_live",
    "idx_recap_attempts_lineage",
    "idx_recap_runs_started",
}
V8_RECAP_INDEXES = RECAP_INDEXES - {"idx_recap_attempts_lineage"}
RECAP_BRANCH_COLUMNS = {
    "recap_input_hash",
    "recap_input_contract_version",
    "recap_eligibility_policy_version",
    "summary_enrichment_input_hash",
    "summary_enrichment_input_contract_version",
    "summary_enrichment_policy_version",
}
RECAP_BRANCH_COLUMN_DECLARATIONS = {
    "recap_input_hash": "TEXT",
    "recap_input_contract_version": "INTEGER",
    "recap_eligibility_policy_version": "INTEGER",
    "summary_enrichment_input_hash": "TEXT",
    "summary_enrichment_input_contract_version": "INTEGER",
    "summary_enrichment_policy_version": "INTEGER",
}
RECAP_TABLE_COLUMNS = {
    "session_recap_jobs": {
        "session_id",
        "requested_input_hash",
        "trigger",
        "state",
        "reason",
        "claim_token",
        "lease_expires_at",
        "active_attempt_id",
        "next_eligible_at",
        "requested_at",
        "updated_at",
        "retry_lineage",
    },
    "session_recap_attempts": {
        "id",
        "session_id",
        "job_session_id",
        "input_hash",
        "input_contract_version",
        "policy_version",
        "recap_contract_version",
        "claim_token",
        "trigger",
        "model",
        "max_budget_usd",
        "timeout_seconds",
        "state",
        "reason",
        "diagnostic",
        "packet_path",
        "packet_nonce",
        "owner_pid",
        "process_group_id",
        "process_started_at",
        "cleanup_state",
        "started_at",
        "finished_at",
        "created_at",
        "retry_lineage",
        "provider_token",
    },
    "session_recap_runtime": {"singleton", "claim_token", "owner_pid", "lease_expires_at", "heartbeat_at"},
    "session_recap_provider_health": {
        "singleton",
        "reason",
        "consecutive_failures",
        "diagnostic",
        "last_failed_at",
        "retry_after",
        "probe_token",
        "probe_active",
        "probe_session_id",
        "probe_claim_token",
    },
    "session_recap_runs": {"id", "trigger", "selector_json", "started_at", "finished_at", "state", "attempt_limit"},
    "session_recap_run_candidates": {
        "run_id",
        "session_id",
        "input_hash",
        "initial_disposition",
        "final_disposition",
        "started_attempt_id",
    },
    "session_recap_quarantine": {
        "attempt_id",
        "path",
        "nonce",
        "byte_size",
        "process_group_id",
        "process_started_at",
        "cleanup_state",
        "created_at",
    },
}
V8_RECAP_TABLE_COLUMNS = {
    table: columns
    - {"retry_lineage", "provider_token", "probe_token", "probe_active", "probe_session_id", "probe_claim_token"}
    for table, columns in RECAP_TABLE_COLUMNS.items()
}


def _has_foreign_key(conn: sqlite3.Connection, table: str, column: str, target: str, target_column: str) -> bool:
    return any(
        row[3] == column and row[2] == target and row[4] == target_column
        for row in conn.execute(f"PRAGMA foreign_key_list({table})")
    )


def _has_index(conn: sqlite3.Connection, table: str, name: str, columns: tuple[str, ...], unique: bool = False) -> bool:
    rows = [row for row in conn.execute(f"PRAGMA index_list({table})") if row[1] == name]
    if not rows or bool(rows[0][2]) != unique:
        return False
    return tuple(row[2] for row in conn.execute(f"PRAGMA index_info({name})")) == columns


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.lower().split())


def _ownership_trigger_sql(name: str) -> str:
    return (
        f"CREATE TRIGGER {name} "
        f"BEFORE {'INSERT' if name.endswith('session') else 'UPDATE OF active_attempt_id'} "
        "ON session_recap_jobs "
        "WHEN NEW.active_attempt_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM session_recap_attempts "
        "WHERE id = NEW.active_attempt_id AND session_id = NEW.session_id "
        "AND job_session_id = NEW.session_id"
        ") BEGIN SELECT RAISE(ABORT, 'active attempt must belong to job session'); END"
    )


OWNERSHIP_TRIGGERS = (
    "session_recap_jobs_active_attempt_session",
    "session_recap_jobs_active_attempt_session_update",
)
# Idempotent so the same statements serve first creation and later repair. Only
# idx_recap_attempts_live's SQL text is inspected by _recap_schema_complete, and
# its WHERE clause survives the IF NOT EXISTS.
V8_RECAP_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_recap_jobs_ready ON session_recap_jobs(state, next_eligible_at)",
    "CREATE INDEX IF NOT EXISTS idx_recap_jobs_lease ON session_recap_jobs(lease_expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_recap_attempts_job_latest ON session_recap_attempts(job_session_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_recap_attempts_input ON session_recap_attempts(session_id, input_hash)",
    "CREATE INDEX IF NOT EXISTS idx_recap_attempts_status ON session_recap_attempts(state, finished_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_recap_attempts_live ON session_recap_attempts(job_session_id) "
    "WHERE state IN ('reserved', 'running')",
    "CREATE INDEX IF NOT EXISTS idx_recap_runs_started ON session_recap_runs(started_at DESC)",
)
V9_RECAP_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_recap_attempts_lineage "
    "ON session_recap_attempts(job_session_id, retry_lineage, id)",
)


def _recreate_recap_indexes_and_triggers(conn: sqlite3.Connection) -> None:
    """Restore missing recap indexes and triggers without touching a single row."""
    for statement in (*V8_RECAP_INDEX_DDL, *V9_RECAP_INDEX_DDL):
        conn.execute(statement)
    for trigger in OWNERSHIP_TRIGGERS:
        # Recreate unconditionally: _recap_schema_complete compares trigger SQL
        # exactly, so a trigger that merely drifted must be replaced, not kept.
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(_ownership_trigger_sql(trigger))


def _repair_recap_objects_in_place(conn: sqlite3.Connection) -> bool:
    """Try to restore completeness by recreating objects; report whether it worked.

    Leaves the schema exactly as found when it cannot, so the caller can fall
    back to the destructive rebuild without inheriting a half-applied repair.
    """
    conn.execute("SAVEPOINT recap_repair_in_place")
    try:
        _recreate_recap_indexes_and_triggers(conn)
        repaired = _recap_schema_complete(conn)
    except sqlite3.DatabaseError:
        # A missing column the index needs, for instance: the shape is wrong in
        # a way only the rebuild can fix.
        repaired = False
    if not repaired:
        conn.execute("ROLLBACK TO SAVEPOINT recap_repair_in_place")
    conn.execute("RELEASE SAVEPOINT recap_repair_in_place")
    return repaired


def _recap_schema_complete(conn: sqlite3.Connection, *, v8: bool = False) -> bool:
    required_indexes = V8_RECAP_INDEXES if v8 else RECAP_INDEXES
    required_columns = V8_RECAP_TABLE_COLUMNS if v8 else RECAP_TABLE_COLUMNS
    objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
    if not (objects >= RECAP_TABLES and objects >= required_indexes):
        return False
    if any(
        not columns <= {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for table, columns in required_columns.items()
    ):
        return False
    branch_columns = {row[1] for row in conn.execute("PRAGMA table_info(branches)")}
    link_columns = {row[1] for row in conn.execute("PRAGMA table_info(branch_messages)")}
    if not (branch_columns >= RECAP_BRANCH_COLUMNS and "position" in link_columns):
        return False
    required_foreign_keys = (
        ("session_recap_jobs", "session_id", "sessions", "id"),
        ("session_recap_jobs", "active_attempt_id", "session_recap_attempts", "id"),
        ("session_recap_attempts", "session_id", "sessions", "id"),
        ("session_recap_attempts", "job_session_id", "session_recap_jobs", "session_id"),
        ("session_recap_run_candidates", "run_id", "session_recap_runs", "id"),
        ("session_recap_run_candidates", "started_attempt_id", "session_recap_attempts", "id"),
        ("session_recap_quarantine", "attempt_id", "session_recap_attempts", "id"),
    )
    if not all(_has_foreign_key(conn, *foreign_key) for foreign_key in required_foreign_keys):
        return False
    indexes = (
        ("session_recap_jobs", "idx_recap_jobs_ready", ("state", "next_eligible_at"), False),
        ("session_recap_jobs", "idx_recap_jobs_lease", ("lease_expires_at",), False),
        ("session_recap_attempts", "idx_recap_attempts_job_latest", ("job_session_id", "id"), False),
        ("session_recap_attempts", "idx_recap_attempts_input", ("session_id", "input_hash"), False),
        ("session_recap_attempts", "idx_recap_attempts_status", ("state", "finished_at"), False),
        ("session_recap_attempts", "idx_recap_attempts_live", ("job_session_id",), True),
        ("session_recap_runs", "idx_recap_runs_started", ("started_at",), False),
    )
    if not v8:
        indexes += (
            ("session_recap_attempts", "idx_recap_attempts_lineage", ("job_session_id", "retry_lineage", "id"), False),
        )
    if not all(_has_index(conn, *index) for index in indexes):
        return False
    sql = {
        row[0]: _normalize_sql(row[1] or "")
        for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')")
    }
    ownership_triggers = (
        "session_recap_jobs_active_attempt_session",
        "session_recap_jobs_active_attempt_session_update",
    )
    return (
        "unique (branch_id, position)" in sql.get("branch_messages", "")
        and "check (state in ('pending', 'claimed', 'current', 'excluded', 'blocked'))"
        in sql.get("session_recap_jobs", "")
        and "check (singleton = 1)" in sql.get("session_recap_runtime", "")
        and "check (singleton = 1)" in sql.get("session_recap_provider_health", "")
        and "where state in ('reserved', 'running')" in sql.get("idx_recap_attempts_live", "")
        and all(sql.get(name) == _normalize_sql(_ownership_trigger_sql(name)) for name in ownership_triggers)
    )


def recap_schema_capability(conn: sqlite3.Connection) -> str:
    """Report recap-schema readiness without altering the database."""
    objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
    branch_columns = {row[1] for row in conn.execute("PRAGMA table_info(branches)")}
    link_columns = {row[1] for row in conn.execute("PRAGMA table_info(branch_messages)")}
    if not (objects & RECAP_TABLES) and "position" not in link_columns and not (branch_columns & RECAP_BRANCH_COLUMNS):
        return "unavailable"
    if not _recap_schema_complete(conn):
        return "partial"
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    return "ready" if version >= SCHEMA_VERSION else "out_of_date"


def _migrate_to_v8(conn: sqlite3.Connection) -> None:
    """Create the complete claim-capable recap schema in one transaction."""
    ddl = """
        CREATE TABLE branch_messages_new (
          branch_id INTEGER NOT NULL REFERENCES branches(id),
          message_id INTEGER NOT NULL REFERENCES messages(id),
          position INTEGER NOT NULL,
          PRIMARY KEY (branch_id, message_id),
          UNIQUE (branch_id, position)
        );
        INSERT INTO branch_messages_new (branch_id, message_id, position)
        SELECT bm.branch_id, bm.message_id,
               ROW_NUMBER() OVER (PARTITION BY bm.branch_id ORDER BY julianday(m.timestamp), m.id) - 1
        FROM branch_messages bm JOIN messages m ON m.id = bm.message_id;
        DROP TABLE branch_messages;
        ALTER TABLE branch_messages_new RENAME TO branch_messages;
        CREATE INDEX idx_branch_messages_message ON branch_messages(message_id);

        CREATE TABLE session_recap_jobs (
          session_id INTEGER PRIMARY KEY REFERENCES sessions(id), requested_input_hash TEXT,
          trigger TEXT NOT NULL,
          state TEXT NOT NULL CHECK (state IN ('pending', 'claimed', 'current', 'excluded', 'blocked')),
           reason TEXT, claim_token INTEGER NOT NULL DEFAULT 0 CHECK (claim_token >= 0),
           lease_expires_at TEXT, active_attempt_id INTEGER REFERENCES session_recap_attempts(id),
           next_eligible_at TEXT,
          requested_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE session_recap_attempts (
          id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id),
          job_session_id INTEGER NOT NULL REFERENCES session_recap_jobs(session_id), input_hash TEXT NOT NULL,
          input_contract_version INTEGER NOT NULL, policy_version INTEGER NOT NULL,
          recap_contract_version INTEGER NOT NULL,
          claim_token INTEGER NOT NULL, trigger TEXT NOT NULL, model TEXT, max_budget_usd REAL, timeout_seconds INTEGER,
          state TEXT NOT NULL CHECK (state IN (
            'reserved', 'running', 'succeeded', 'stale_discarded', 'timeout',
            'budget_exceeded', 'unusable_output', 'global_abort', 'cleanup_failed',
            'abandoned', 'cancelled_before_launch'
          )),
          reason TEXT, diagnostic TEXT, packet_path TEXT, packet_nonce TEXT,
          owner_pid INTEGER, process_group_id INTEGER, process_started_at TEXT, cleanup_state TEXT,
          started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE session_recap_runtime (
          singleton INTEGER PRIMARY KEY CHECK (singleton = 1), claim_token INTEGER NOT NULL DEFAULT 0,
          owner_pid INTEGER, lease_expires_at TEXT, heartbeat_at TEXT
        );
        CREATE TABLE session_recap_provider_health (
          singleton INTEGER PRIMARY KEY CHECK (singleton = 1), reason TEXT,
          consecutive_failures INTEGER NOT NULL DEFAULT 0,
          diagnostic TEXT, last_failed_at TEXT, retry_after TEXT
        );
        CREATE TABLE session_recap_runs (
          id INTEGER PRIMARY KEY, trigger TEXT NOT NULL, selector_json TEXT, started_at TEXT NOT NULL,
          finished_at TEXT, state TEXT NOT NULL, attempt_limit INTEGER
        );
        CREATE TABLE session_recap_run_candidates (
          run_id INTEGER NOT NULL REFERENCES session_recap_runs(id),
          session_id INTEGER NOT NULL REFERENCES sessions(id),
          input_hash TEXT, initial_disposition TEXT NOT NULL, final_disposition TEXT,
          started_attempt_id INTEGER REFERENCES session_recap_attempts(id), PRIMARY KEY (run_id, session_id)
        );
        CREATE TABLE session_recap_quarantine (
          attempt_id INTEGER PRIMARY KEY REFERENCES session_recap_attempts(id), path TEXT NOT NULL,
          nonce TEXT NOT NULL, byte_size INTEGER, process_group_id INTEGER,
          process_started_at TEXT, cleanup_state TEXT NOT NULL,
          created_at TEXT NOT NULL, CHECK (byte_size IS NULL OR byte_size >= 0)
        );
    """
    branch_columns = {row[1] for row in conn.execute("PRAGMA table_info(branches)")}
    for column, declaration in RECAP_BRANCH_COLUMN_DECLARATIONS.items():
        if column not in branch_columns:
            conn.execute(f"ALTER TABLE branches ADD COLUMN {column} {declaration}")
    # executescript commits any active transaction before running its SQL. Keep
    # every claim object inside the caller's BEGIN IMMEDIATE instead.
    for statement in ddl.split(";"):
        if statement.strip():
            conn.execute(statement)
    for statement in V8_RECAP_INDEX_DDL:
        conn.execute(statement)
    for trigger in OWNERSHIP_TRIGGERS:
        conn.execute(_ownership_trigger_sql(trigger))
    if not _recap_schema_complete(conn, v8=True):
        raise sqlite3.OperationalError("recap migration postconditions failed")


def _repair_v8_schema(conn: sqlite3.Connection) -> None:
    """Replace an unshipped incomplete v8 recap shape within its migration transaction."""
    for trigger in (
        "session_recap_jobs_active_attempt_session",
        "session_recap_jobs_active_attempt_session_update",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for index in RECAP_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    for table in (
        "session_recap_quarantine",
        "session_recap_run_candidates",
        "session_recap_attempts",
        "session_recap_jobs",
        "session_recap_runtime",
        "session_recap_provider_health",
        "session_recap_runs",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    _migrate_to_v8(conn)


def _migrate_to_v9(conn: sqlite3.Connection) -> None:
    """Add recap lifecycle fencing without replacing committed v8 tables."""
    additions = (
        ("session_recap_jobs", "retry_lineage", "INTEGER NOT NULL DEFAULT 0"),
        ("session_recap_attempts", "retry_lineage", "INTEGER NOT NULL DEFAULT 0"),
        ("session_recap_attempts", "provider_token", "INTEGER"),
        ("session_recap_provider_health", "probe_token", "INTEGER NOT NULL DEFAULT 0"),
        ("session_recap_provider_health", "probe_active", "INTEGER NOT NULL DEFAULT 0"),
        ("session_recap_provider_health", "probe_session_id", "INTEGER"),
        ("session_recap_provider_health", "probe_claim_token", "INTEGER"),
    )
    for table, column, declaration in additions:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recap_attempts_lineage "
        "ON session_recap_attempts(job_session_id, retry_lineage, id)"
    )
    if not _recap_schema_complete(conn):
        raise sqlite3.OperationalError("recap v9 migration postconditions failed")


def _has_vec_objects(conn: sqlite3.Connection) -> bool:
    """Return True if this database carries vec0 virtual tables.

    Reads sqlite_master as text, which needs no extension — the point is to find
    out whether a reshape would need one before attempting the reshape.
    """
    query = "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND sql LIKE '%USING vec0%'"
    return conn.execute(query).fetchone()[0] > 0


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

    if current >= SCHEMA_VERSION and _recap_schema_complete(conn):
        return
    if prepare is None and _has_vec_objects(conn):
        # This sits below v3-v7 on purpose: those only add columns, which never
        # makes SQLite reparse a vec0 table definition. The v8/v9 reshapes do.
        #
        # Every reshape below reparses the schema, and that parse needs the vec0
        # module registered on this connection. This boundary deliberately imports
        # none of the vec stack (see the transitive-import tests), so it cannot
        # supply it. Leave the schema exactly as it is and report an unmigrated
        # database — recap_schema_capability() already tells every caller here to
        # stand down. Attempting the reshape instead raised on every single open,
        # which is what took the drainer down on each run.
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current < SCHEMA_VERSION:
                # Before any migration, not just v1. Every one of these reshapes
                # a table, and a reshape reparses the schema — which fails on a
                # DB carrying vec0 objects unless the extension is loaded. A user
                # who has run embeddings has those objects, so without this the
                # whole database becomes unopenable rather than merely unmigrated.
                if prepare is not None:
                    prepare(conn)
                if current < 1:
                    migrate_to_v1(conn)
                if current < 2:
                    migrate_to_v2(conn)
                if current < 8:
                    _migrate_to_v8(conn)
                if current < 9:
                    if not _recap_schema_complete(conn, v8=True):
                        _repair_v8_schema(conn)
                    _migrate_to_v9(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif not _recap_schema_complete(conn):
                if current == 8 and _recap_schema_complete(conn, v8=True):
                    _migrate_to_v9(conn)
                elif not _repair_recap_objects_in_place(conn):
                    # Only a genuinely wrong table or column shape earns the
                    # rebuild: it drops every recap table, and the quarantine
                    # rows it takes with them are the only record of packets
                    # still awaiting cleanup.
                    _repair_v8_schema(conn)
                    _migrate_to_v9(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _open_connection(
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


def open_no_migrate_connection(settings: dict | None = None, *, busy_timeout_ms: int = 100) -> sqlite3.Connection:
    """Open an existing writable DB for a bounded hook operation without migration."""
    db_path = get_db_path(settings)
    # '?' and '#' are reserved in a URI, so a path containing either would be
    # read as the start of the query or fragment and open the wrong database.
    conn = sqlite3.connect(f"file:{quote(str(db_path))}?mode=rw", uri=True)
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextlib.contextmanager
def get_connection(settings: dict | None = None):
    """Get a lightweight DB connection: commit-on-success, rollback-on-exception, always-close."""
    conn = _open_connection(settings)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
