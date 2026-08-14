"""Recap migrations against a database that carries vec0 objects.

Every user who has run embeddings has a `chunks`/`chunk_vec` pair and the triggers
that bind them. Reshaping a table reparses the whole schema, and that parse fails
with "no such module: vec0" unless the extension is loaded on the connection —
so the recap migrations behave differently depending on which connection boundary
opened the database. Only `db.get_connection` supplies a vec loader; the
lightweight `llm_summary_db.get_connection` deliberately imports none of the vec
stack, and must therefore decline rather than corrupt or crash.
"""

import sqlite3

import pytest
import sqlite_vec
from conftest import VEC_SKIP

from ccrecall import db, llm_summary_db
from ccrecall.hooks import drain_session_recaps

pytestmark = VEC_SKIP


def _legacy_vec_db(path) -> None:
    """Build the pre-recap database an upgrading embeddings user actually has."""
    with db.get_connection({"db_path": str(path)}) as conn:
        conn.execute("SELECT 1")
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # The chunk_vec table and its triggers are what make a schema reparse need
    # the extension; a database that never embedded anything has neither.
    db._ensure_vec_schema(conn)
    conn.commit()
    recap = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'session_recap%'")
    ]
    for table in recap:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'chunk_vec'").fetchone()[0] == 1
    conn.close()


def test_vec_aware_connection_migrates_a_legacy_embedding_database(tmp_path):
    """db.get_connection loads vec0 first, so the v8 reshapes parse cleanly."""
    path = tmp_path / "legacy.db"
    _legacy_vec_db(path)

    with db.get_connection({"db_path": str(path)}) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == llm_summary_db.SCHEMA_VERSION


def test_lightweight_connection_declines_instead_of_crashing_on_vec_objects(tmp_path):
    """The drainer opened these databases through the lightweight path and died.

    The reshape cannot run without vec0, and this boundary must not import it, so
    the only correct answer is to leave the schema alone and report an unmigrated
    database. Raising here took the whole drain run down on every single pass.
    """
    path = tmp_path / "legacy.db"
    _legacy_vec_db(path)

    with llm_summary_db.get_connection({"db_path": str(path)}) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] < llm_summary_db.SCHEMA_VERSION
        assert llm_summary_db.recap_schema_capability(conn) != "ready"


def test_declining_does_not_poison_a_later_successful_migration(tmp_path):
    """Declining must leave nothing half-applied behind it.

    The lightweight open goes first and declines; the vec-aware open then has to
    find a database it can still migrate, with its embedding objects untouched.
    """
    path = tmp_path / "legacy.db"
    _legacy_vec_db(path)

    with llm_summary_db.get_connection({"db_path": str(path)}):
        pass

    with db.get_connection({"db_path": str(path)}) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'chunk_vec'").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == llm_summary_db.SCHEMA_VERSION


def test_lightweight_connection_still_migrates_a_database_without_vec_objects(tmp_path):
    """Declining is scoped to vec0-carrying databases, not every lightweight open."""
    path = tmp_path / "plain.db"

    with llm_summary_db.get_connection({"db_path": str(path)}) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == llm_summary_db.SCHEMA_VERSION
        assert llm_summary_db.recap_schema_capability(conn) == "ready"


@pytest.mark.parametrize("start_version", [7, 8])
def test_drainer_connection_migrates_a_legacy_embedding_database(tmp_path, start_version):
    """The drainer already loads the whole vec stack, so it uses the vec-aware boundary.

    This is what makes recaps reachable at all for an embeddings user: something
    on the recap path has to be able to complete the migration.
    """
    path = tmp_path / "legacy.db"
    _legacy_vec_db(path)
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(f"PRAGMA user_version = {start_version}")
    conn.commit()
    conn.close()

    with drain_session_recaps.get_connection({"db_path": str(path)}) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == llm_summary_db.SCHEMA_VERSION
        assert llm_summary_db.recap_schema_capability(conn) == "ready"
