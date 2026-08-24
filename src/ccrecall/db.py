"""Database connection and schema-adjacent utilities.

Schema constants live in ccrecall.schema. Paths, config/settings loading, PID
files, and logging live in ccrecall.config — imported below for this module's
own use (get_db_path, ensure_parent_dir, DEFAULT_DB_PATH, ...). Vec (embedding)
operations live in ccrecall.db_vec — kept out of this module so
get_connection() stays importable without pulling numpy/fastembed/onnxruntime
onto the hook hot path (see CLAUDE.md invariant #3).
"""

import contextlib
import sqlite3
from pathlib import Path

from ccrecall import db_base
from ccrecall.config import DEFAULT_DB_PATH
from ccrecall.config import DEFAULT_PROJECTS_DIR as DEFAULT_PROJECTS_DIR

# Current schema version. Re-exported from the embedding-free connection layer so
# both DB boundaries apply the same migrations.
SCHEMA_VERSION = db_base.SCHEMA_VERSION

# Shared SQL predicate for "branches that are candidates to embed": active
# leaves (the query path only returns is_active=1) with a usable summary. This
# is the single source of truth for the embedding universe — build_selection()
# (eligibility), count_status() (backfill progress), and search_cli
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
# Chunk-grain draft-quality predicate: a chunk embedded under a cap below the
# model's full token limit. Single source of truth for backfill_query,
# backfill_status, and db_vec — binds MODEL_TOKEN_LIMIT as a parameter (?).
# health.py deliberately uses its own literal (FULL_QUALITY_TOKEN_LIMIT) to
# avoid importing embeddings.py on the hot path; a cross-check test keeps them
# in sync.
CHUNK_DRAFT_QUALITY_FILTER = "cap_tokens IS NOT NULL AND cap_tokens < ?"


def apply_base_pragmas(conn: sqlite3.Connection) -> None:
    """Set WAL mode, busy_timeout, and foreign-key enforcement for concurrent-safe access.

    WAL lets readers and writers proceed without blocking each other; busy_timeout
    waits instead of failing on a writer-writer collision; foreign_keys=ON prevents
    orphaned rows.
    """
    db_base.apply_base_pragmas(conn)


def escape_like(value: str) -> str:
    """Escape SQLite LIKE wildcards so a user value matches literally (pair with ESCAPE '\\')."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def has_tool_counts(cursor: sqlite3.Cursor) -> bool:
    """Check if the branches table has a tool_counts column (absent on old DBs)."""
    cursor.execute("PRAGMA table_info(branches)")
    return "tool_counts" in {row[1] for row in cursor.fetchall()}


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


_migrate_to_v1 = db_base._migrate_to_v1
_migrate_to_v2 = db_base._migrate_to_v2
_migrate_to_v3 = db_base._migrate_to_v3
_migrate_to_v4 = db_base._migrate_to_v4
_migrate_to_v5 = db_base._migrate_to_v5
_migrate_to_v6 = db_base._migrate_to_v6
_migrate_to_v7 = db_base._migrate_to_v7
_migrate_to_v8 = db_base._migrate_to_v8


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply the shared migrations, preserving db.py's vec-aware v1 compatibility.

    ``prepare`` is a deferred-import closure rather than a direct reference to
    ``ccrecall.db_vec.vec_available`` — importing db_vec at module scope would
    reintroduce the numpy import chain this split exists to avoid. The closure
    only imports numpy when it actually fires, i.e. when
    ``user_version < SCHEMA_VERSION`` (a one-time event per DB version bump,
    not on every connection). On a fully migrated DB this is never called, so
    the hook hot-path invariant (CLAUDE.md #3) holds.
    """

    def _prepare_vec(c: sqlite3.Connection) -> bool:
        # Deferred so this only fires on a version-bump migration, not on
        # every connection (hook hot-path invariant, CLAUDE.md #3).
        # lazy-import: db_vec imports numpy transitively
        from ccrecall.db_vec import vec_available

        return vec_available(c)

    db_base._apply_migrations(
        conn,
        prepare=_prepare_vec,
        migrate_to_v1=_migrate_to_v1,
        migrate_to_v2=_migrate_to_v2,
    )


def _open_connection(settings: dict | None = None, load_vec: bool = False) -> sqlite3.Connection:
    """Open a raw database connection, initializing the schema on first use (idempotent).

    Uses the settings-based db_path when provided and applies the base pragmas for
    concurrent-safe access. When ``load_vec`` is True, loads the sqlite-vec extension
    and raises busy_timeout to db_vec.VEC_BUSY_TIMEOUT_MS — use it for connections
    that query or write chunk_vec (search, write path, backfill). Default False
    keeps the extension unloaded, cheaper for recent-chats and setup paths that
    never touch the vec tables.

    Callers should not use this directly — use ``get_connection`` (the
    context-manager wrapper) instead, which guarantees the connection is
    committed on success, rolled back on exception, and always closed.
    """
    conn = db_base.open_connection(settings, apply_migrations_callback=_apply_migrations)

    if load_vec:
        # Deferred so get_connection() itself stays numpy-free for callers
        # that don't opt into vec (hook hot-path invariant, CLAUDE.md #3).
        # lazy-import: db_vec imports numpy transitively
        from ccrecall.db_vec import ensure_vec

        ensure_vec(conn)

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
