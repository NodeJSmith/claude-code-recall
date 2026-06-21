"""
Database migrations for ccrecall, run by db.get_db_connection on every open:
migrate_db handles the pre-v3 structural migration (nuke-and-recreate),
migrate_columns adds missing columns and runs the versioned DML migrations.

Imports from ccrecall.schema, ccrecall.content, ccrecall.parsing, and
ccrecall.summarizer — never from ccrecall.db (cycle prevention).
"""

import contextlib
import json
import sqlite3
import time
from pathlib import Path

from ccrecall.content import parse_origin
from ccrecall.parsing import build_aggregated_content, extract_session_uuid
from ccrecall.schema import SCHEMA_CORE, SCHEMA_FTS4, SCHEMA_FTS5, detect_fts_support
from ccrecall.summarizer import truncate_mid

BUSY_TIMEOUT_MS = 5000
_MIGRATION_BATCH_SIZE = 50
_MIGRATION_V6_MAX_JSON_BYTES = 50 * 1024


def migrate_db(conn: sqlite3.Connection) -> bool:
    """
    Migrate database to v3 schema (messages-once + branch index).
    Detects old schema by checking if 'branches' table exists.
    If not, deletes the DB file so a fresh import is triggered.
    Returns True if migration was performed (DB was deleted and recreated).
    """
    cursor = conn.cursor()

    # Check if branches table exists (v3 indicator)
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='branches'")
    if cursor.fetchone():
        return False  # Already on v3

    # Check if sessions table exists at all (could be a fresh DB)
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
    if not cursor.fetchone():
        return False  # Fresh DB, no migration needed

    # Old schema detected — backup then nuke and recreate
    db_path = None
    # Get the database file path from connection
    cursor.execute("PRAGMA database_list")
    for row in cursor.fetchall():
        if row[1] == "main" and row[2]:
            db_path = Path(row[2])
            break

    if db_path and db_path.exists():
        # JSONL source files expire after 30 days — data older than that
        # exists only in this DB. Back up before destroying.
        backed_up = _backup_db_before_migration(db_path, "pre-v3-nuke")
        if not backed_up:
            # Backup failed (disk full, permissions, etc.) — refuse to destroy
            # the only copy. Return False so caller uses the old schema as-is.
            return False
        conn.close()
        db_path.unlink()
        # Clean up WAL/SHM files — orphaned WAL replayed into an empty DB
        # causes "database disk image is malformed" errors.
        for suffix in ("-wal", "-shm"):
            wal_path = db_path.with_name(db_path.name + suffix)
            if wal_path.exists():
                wal_path.unlink()

    # Reconnect and create fresh schema
    new_conn = sqlite3.connect(str(db_path) if db_path else ":memory:")
    new_conn.execute("PRAGMA journal_mode = WAL")
    new_conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    fts = detect_fts_support(new_conn)
    new_conn.executescript(SCHEMA_CORE)
    if fts == "fts5":
        new_conn.executescript(SCHEMA_FTS5)
    elif fts == "fts4":
        new_conn.executescript(SCHEMA_FTS4)
    new_conn.commit()

    # We can't return the new connection through the old reference,
    # so we signal that migration happened and caller should reconnect
    new_conn.close()
    return True


def _reaggregate_notification_branches(cursor: sqlite3.Cursor) -> None:
    """Re-aggregate branches that contain notification messages.

    Updates aggregated_content and exchange_count to exclude notifications.
    Called after backfilling is_notification on existing messages.
    """
    cursor.execute("""
        SELECT DISTINCT bm.branch_id
        FROM branch_messages bm
        JOIN messages m ON bm.message_id = m.id
        WHERE m.is_notification = 1
    """)
    affected_branches = [row[0] for row in cursor.fetchall()]
    for bid in affected_branches:
        cursor.execute(
            """
            SELECT m.content FROM branch_messages bm
            JOIN messages m ON bm.message_id = m.id
            WHERE bm.branch_id = ? AND COALESCE(m.is_notification, 0) = 0
            ORDER BY m.timestamp ASC
        """,
            (bid,),
        )
        agg = "\n".join(row[0] for row in cursor.fetchall())
        cursor.execute("UPDATE branches SET aggregated_content = ? WHERE id = ?", (agg, bid))
        cursor.execute(
            """
            SELECT COUNT(*) FROM branch_messages bm
            JOIN messages m ON bm.message_id = m.id
            WHERE bm.branch_id = ? AND m.role = 'user' AND COALESCE(m.is_notification, 0) = 0
        """,
            (bid,),
        )
        human_user_count = cursor.fetchone()[0]
        cursor.execute(
            "UPDATE branches SET exchange_count = ? WHERE id = ?",
            (human_user_count, bid),
        )


def migrate_columns(conn: sqlite3.Connection) -> None:
    """Add missing columns (DDL, idempotent) and run versioned data migrations (DML)."""
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(messages)")
    existing = {row[1] for row in cursor.fetchall()}

    # DDL migrations (column-existence gated, idempotent)
    if "tool_summary" not in existing:
        cursor.execute("ALTER TABLE messages ADD COLUMN tool_summary TEXT")
        conn.commit()
    if "is_notification" not in existing:
        cursor.execute("ALTER TABLE messages ADD COLUMN is_notification INTEGER DEFAULT 0")
        conn.commit()
    if "origin" not in existing:
        cursor.execute("ALTER TABLE messages ADD COLUMN origin TEXT")
        conn.commit()

    # branches DDL migration
    cursor.execute("PRAGMA table_info(branches)")
    branch_cols = {row[1] for row in cursor.fetchall()}
    if "tool_counts" not in branch_cols:
        cursor.execute("ALTER TABLE branches ADD COLUMN tool_counts TEXT")
        conn.commit()
    if "context_summary" not in branch_cols:
        cursor.execute("ALTER TABLE branches ADD COLUMN context_summary TEXT")
    if "context_summary_json" not in branch_cols:
        cursor.execute("ALTER TABLE branches ADD COLUMN context_summary_json TEXT")
    if "summary_version" not in branch_cols:
        cursor.execute("ALTER TABLE branches ADD COLUMN summary_version INTEGER DEFAULT 0")
    if "embedding_version" not in branch_cols:
        cursor.execute("ALTER TABLE branches ADD COLUMN embedding_version INTEGER DEFAULT 0")
    if "embedding_model" not in branch_cols:
        cursor.execute("ALTER TABLE branches ADD COLUMN embedding_model TEXT")
    if "summary_version_at_embed" not in branch_cols:
        cursor.execute("ALTER TABLE branches ADD COLUMN summary_version_at_embed INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_branches_summary_version ON branches(summary_version)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_branches_embedding_version ON branches(embedding_version)")
    conn.commit()

    # Vec schema (branch_vec virtual table + trigger) is only created on
    # load_vec=True connections in get_db_connection. It is not created here
    # because this function runs on every connection, including load_vec=False
    # connections used by recent-chats and token-analytics.

    # token_snapshots table (new table, not a column add)
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='token_snapshots'")
    if not cursor.fetchone():
        cursor.executescript("""
CREATE TABLE IF NOT EXISTS token_snapshots (
  id INTEGER PRIMARY KEY,
  session_uuid TEXT UNIQUE NOT NULL,
  project_path TEXT,
  start_time DATETIME,
  duration_minutes INTEGER,
  user_message_count INTEGER,
  assistant_message_count INTEGER,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  cache_creation_tokens INTEGER DEFAULT 0,
  tool_counts TEXT,
  tool_errors INTEGER DEFAULT 0,
  uses_task_agent INTEGER DEFAULT 0,
  uses_web_search INTEGER DEFAULT 0,
  uses_web_fetch INTEGER DEFAULT 0,
  user_response_times TEXT,
  lines_added INTEGER DEFAULT 0,
  lines_removed INTEGER DEFAULT 0,
  goal_categories TEXT,
  outcome TEXT,
  session_type TEXT,
  friction_counts TEXT,
  brief_summary TEXT,
  imported_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_token_snapshots_session ON token_snapshots(session_uuid);
CREATE INDEX IF NOT EXISTS idx_token_snapshots_start ON token_snapshots(start_time);
""")
        conn.commit()

    # Ensure data_source column exists (added by get-token-insights ingest script)
    cursor.execute("PRAGMA table_info(token_snapshots)")
    snapshot_cols = {row[1] for row in cursor.fetchall()}
    if "data_source" not in snapshot_cols:
        cursor.execute("ALTER TABLE token_snapshots ADD COLUMN data_source TEXT")
        conn.commit()

    # DML migrations (version-gated via PRAGMA user_version, run once)
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    # Resolve db_path for backup operations (PRAGMA database_list returns (seq, name, file))
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])

    if version < 1:
        # v0.5.0: Backfill task-notification messages
        cursor.execute("""
            UPDATE messages SET is_notification = 1
            WHERE role = 'user' AND content LIKE '<task-notification>%' AND is_notification = 0
        """)
        _reaggregate_notification_branches(cursor)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    if version < 2:
        # v0.7.1: Backfill teammate messages as notifications
        cursor.execute("""
            UPDATE messages SET is_notification = 1
            WHERE role = 'user' AND content LIKE '<teammate-message%' AND is_notification = 0
        """)
        _reaggregate_notification_branches(cursor)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()

    if version < 3:
        # v0.8.0: Selective backfill of origin column from JSONL files.
        # Phase 1: UPDATE existing messages with origin data.
        # Phase 2: Nullify file_hash for sessions with channel messages
        #          (isMeta+origin entries previously filtered) so the next
        #          normal import re-processes just those sessions.
        _backfill_origin(conn, cursor)
        conn.execute("PRAGMA user_version = 3")
        conn.commit()

    if version < 4:
        # v0.8.70: Clear stale task-notification values from origin column.
        # parse_origin had a kind-fallback bug that leaked task-notification
        # into origin (reserved for channel sources: telegram, discord, slack).
        _backup_db_before_migration(db_path, "v4")
        cursor.execute("UPDATE messages SET origin = NULL WHERE origin = 'task-notification'")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()

    if version < 5:
        # v5: FTS metadata enrichment — recompute aggregated_content (SET semantics)
        # with file paths and commits for all existing branches.
        # Also renames INTERRUPTED→ABANDONED in stored summaries.
        # Gates _migrate_project_paths() as a pre-pass.
        # Runs a single FTS rebuild after all batch updates.
        _migrate_project_paths(conn)
        _migrate_v5(conn, cursor)

    if version < 6:
        # v6: Truncate oversized context_summary_json entries (>50KB).
        _migrate_v6(conn, cursor)


def _backup_db_before_migration(db_path: Path, label: str) -> bool:
    """Create a timestamped WAL-safe backup using sqlite3.Connection.backup().

    Returns True if backup succeeded and was verified, False otherwise.
    sqlite3.Connection.backup() does a page-level copy that respects WAL journaling.
    """
    if not db_path.name or not db_path.exists():
        return False
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_suffix(f".pre-{label}-{ts}.db")
    src = None
    dst = None
    try:
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(backup_path))
        src.backup(dst)
        # Verify backup is non-empty
        if not backup_path.exists() or backup_path.stat().st_size == 0:
            return False
        return True
    except (sqlite3.Error, OSError):
        return False
    finally:
        if dst:
            dst.close()
        if src:
            src.close()


def _backfill_origin(conn: sqlite3.Connection, cursor: sqlite3.Cursor) -> None:
    """Selectively backfill origin column from JSONL files without full reimport.

    For each file in import_log, scan for entries with origin fields
    and UPDATE existing message rows by session_id + uuid.

    Note: Previously had a Phase 2 that nullified file_hash to force reimport
    of sessions with channel messages. Removed because the reimport path is
    destructive (delete-all-then-insert) and JSONL files expire after 30 days —
    triggering a reimport risks irrecoverable data loss.
    """
    # Guard: sessions table may not exist in minimal test DBs
    tables = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "sessions" not in tables or "import_log" not in tables:
        return

    # Build file_path -> session mapping from import_log + sessions
    file_session_map = {}
    all_import_rows = cursor.execute("SELECT file_path FROM import_log").fetchall()
    for (file_path,) in all_import_rows:
        stem = extract_session_uuid(Path(file_path))
        row = cursor.execute("SELECT id FROM sessions WHERE uuid = ?", (stem,)).fetchone()
        if row:
            file_session_map[file_path] = row[0]

    for file_path, session_id in file_session_map.items():
        p = Path(file_path)
        if not p.exists():
            continue

        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    origin = obj.get("origin")
                    if not origin:
                        continue

                    # UPDATE origin for existing messages
                    uuid = obj.get("uuid")
                    if uuid and obj.get("type") in ("user", "assistant"):
                        origin_value = parse_origin(obj)
                        if origin_value:
                            cursor.execute(
                                "UPDATE messages SET origin = ? WHERE session_id = ? AND uuid = ?",
                                (origin_value, session_id, uuid),
                            )
        except OSError:
            continue

    conn.commit()


def _migrate_project_paths(conn: sqlite3.Connection) -> None:
    """Fix project paths that were incorrectly derived from hyphenated directory keys.

    When projects were first imported, path/name were derived from the Claude project
    directory key (e.g. '-Users-foo-repos-meta-ads-cli') using a lossy replace('-', '/')
    heuristic. For directories with hyphens in their name (e.g. 'meta-ads-cli'), this
    produces wrong paths ('/Users/foo/repos/meta/ads/cli' instead of the real path).

    The sessions.cwd column stores the REAL filesystem path recorded at runtime. This
    migration uses the most-common cwd across each project's sessions to correct the
    project path and name. It also merges duplicate projects that resolve to the same
    real path.

    This is idempotent: after fixing, the project path matches session cwd, so subsequent
    runs find no mismatch and do nothing.
    """
    cursor = conn.cursor()

    # Guard: projects and sessions tables may not exist in minimal test DBs
    tables = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "projects" not in tables or "sessions" not in tables:
        return

    # Find all projects that have at least one session with a non-null cwd
    cursor.execute("""
        SELECT p.id, p.path, p.name,
               s.cwd,
               COUNT(*) AS cwd_count
        FROM projects p
        JOIN sessions s ON s.project_id = p.id
        WHERE s.cwd IS NOT NULL AND s.cwd != ''
        GROUP BY p.id, s.cwd
        ORDER BY p.id, cwd_count DESC
    """)
    rows = cursor.fetchall()
    if not rows:
        return

    # For each project, pick the most common cwd as the authoritative real path
    best_cwd: dict[int, str] = {}
    for proj_id, _path, _name, cwd, _count in rows:
        if proj_id not in best_cwd:
            best_cwd[proj_id] = cwd  # rows ordered by cwd_count DESC, first wins

    # Now check which projects need updating
    cursor.execute("SELECT id, path, name FROM projects")
    projects = cursor.fetchall()

    for proj_id, stored_path, _stored_name in projects:
        real_cwd = best_cwd.get(proj_id)
        if not real_cwd or real_cwd == stored_path:
            continue  # No cwd data or already correct

        real_name = Path(real_cwd).name

        # Check if another project already has real_cwd as its path (merge conflict)
        cursor.execute("SELECT id FROM projects WHERE path = ? AND id != ?", (real_cwd, proj_id))
        existing = cursor.fetchone()

        if existing:
            # Merge: reassign all sessions from this (wrong) project to the existing one
            keeper_id = existing[0]
            cursor.execute(
                "UPDATE sessions SET project_id = ? WHERE project_id = ?",
                (keeper_id, proj_id),
            )
            cursor.execute("DELETE FROM projects WHERE id = ?", (proj_id,))
        else:
            # Simple fix: update path and name
            cursor.execute(
                "UPDATE projects SET path = ?, name = ? WHERE id = ?",
                (real_cwd, real_name, proj_id),
            )

    conn.commit()


def _migrate_v5(conn: sqlite3.Connection, cursor: sqlite3.Cursor) -> None:
    """v5: Recompute aggregated_content (SET semantics) with file paths and commits.

    For each branch in batches of 50:
    - Read files_modified and commits from the branches table.
    - Recompute aggregated_content using the same format as build_aggregated_content()
      in parsing.py (message text + __files__ section + __commits__ section).
    - String-replace **Status:** INTERRUPTED with **Status:** ABANDONED in context_summary.
    - Update disposition field in context_summary_json.

    After all batches, run a single FTS rebuild.  Bumps user_version to 5.
    """
    # Fetch all branch IDs in one pass; process in batches of 50
    cursor.execute("SELECT id, files_modified, commits, context_summary_json FROM branches ORDER BY id")
    all_branches = cursor.fetchall()

    for batch_start in range(0, len(all_branches), _MIGRATION_BATCH_SIZE):
        batch = all_branches[batch_start : batch_start + _MIGRATION_BATCH_SIZE]
        for branch_id, files_json_raw, commits_json_raw, csj_raw in batch:
            # Parse files_modified JSON (may be NULL)
            files: list[str] | None = None
            if files_json_raw:
                try:
                    parsed = json.loads(files_json_raw)
                    if isinstance(parsed, list):
                        files = [str(f) for f in parsed if f]
                except (json.JSONDecodeError, TypeError):
                    pass

            # Parse commits JSON (may be NULL)
            commits: list[str] | None = None
            if commits_json_raw:
                try:
                    parsed = json.loads(commits_json_raw)
                    if isinstance(parsed, list):
                        commits = [str(c) for c in parsed if c]
                except (json.JSONDecodeError, TypeError):
                    pass

            # Recompute aggregated_content — SET semantics (replaces existing value)
            agg_content = build_aggregated_content(cursor, branch_id, files, commits)
            cursor.execute(
                "UPDATE branches SET aggregated_content = ? WHERE id = ?",
                (agg_content, branch_id),
            )

            # Rename INTERRUPTED → ABANDONED in context_summary (markdown)
            cursor.execute(
                "UPDATE branches SET context_summary = "
                "REPLACE(context_summary, '**Status:** INTERRUPTED', '**Status:** ABANDONED') "
                "WHERE id = ? AND context_summary LIKE '%**Status:** INTERRUPTED%'",
                (branch_id,),
            )

            # Update disposition in context_summary_json
            if csj_raw:
                try:
                    summary = json.loads(csj_raw)
                    if summary.get("disposition") == "INTERRUPTED":
                        summary["disposition"] = "ABANDONED"
                        cursor.execute(
                            "UPDATE branches SET context_summary_json = ? WHERE id = ?",
                            (json.dumps(summary), branch_id),
                        )
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass

        conn.commit()

    # Single FTS rebuild after all batches — faster than per-row trigger-driven ops.
    # FTS table may not exist (e.g., FTS disabled SQLite build), so this is best-effort.
    with contextlib.suppress(Exception):
        conn.execute("INSERT INTO branches_fts(branches_fts) VALUES('rebuild')")
        conn.commit()

    conn.execute("PRAGMA user_version = 5")
    conn.commit()


def _migrate_v6(conn: sqlite3.Connection, cursor: sqlite3.Cursor) -> None:
    """v6: Truncate oversized context_summary_json entries (>50KB).

    Queries branches where len(context_summary_json) > 50KB, parses JSON,
    applies truncate_mid() to all exchange text fields (user and assistant in
    first_exchanges and last_exchanges), re-serializes, and updates the row.
    Processes in batches with per-batch commits.  Bumps user_version to 6.
    """
    cursor.execute(
        "SELECT id, context_summary_json FROM branches WHERE length(context_summary_json) > ?",
        (_MIGRATION_V6_MAX_JSON_BYTES,),
    )
    oversized = cursor.fetchall()

    for batch_start in range(0, len(oversized), _MIGRATION_BATCH_SIZE):
        batch = oversized[batch_start : batch_start + _MIGRATION_BATCH_SIZE]
        for branch_id, csj_raw in batch:
            if not csj_raw:
                continue
            try:
                summary = json.loads(csj_raw)
            except (json.JSONDecodeError, TypeError):
                continue

            changed = False
            for exchange_list_key in ("first_exchanges", "last_exchanges"):
                exchanges = summary.get(exchange_list_key)
                if not isinstance(exchanges, list):
                    continue
                for ex in exchanges:
                    if not isinstance(ex, dict):
                        continue
                    for field in ("user", "assistant"):
                        val = ex.get(field)
                        if isinstance(val, str):
                            truncated = truncate_mid(val)
                            if truncated != val:
                                ex[field] = truncated
                                changed = True

            new_json = json.dumps(summary)
            if len(new_json) > _MIGRATION_V6_MAX_JSON_BYTES:
                summary["last_exchanges"] = []
                new_json = json.dumps(summary)
                changed = True
            if changed:
                cursor.execute(
                    "UPDATE branches SET context_summary_json = ? WHERE id = ?",
                    (new_json, branch_id),
                )

        conn.commit()

    conn.execute("PRAGMA user_version = 6")
    conn.commit()
