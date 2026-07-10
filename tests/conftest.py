"""Shared fixtures for ccrecall tests."""

import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

from ccrecall.db import _ensure_vec_schema
from ccrecall.health import clear_embedding_failure, record_embedding_failure
from ccrecall.schema import SCHEMA

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def patched_record(sidecar: Path):
    """side_effect redirecting record_embedding_failure to a tmp sidecar path.

    Shared by the embedding-status recording tests in test_backfill_embeddings
    and test_sync_hook so the real ~/.ccrecall sidecar is never touched.
    """
    return lambda reason: record_embedding_failure(reason, path=sidecar)


def patched_clear(sidecar: Path):
    """side_effect redirecting clear_embedding_failure to a tmp sidecar path."""
    return lambda: clear_embedding_failure(path=sidecar)


def vec_available_in_env() -> bool:
    """Return True if the sqlite-vec extension can be loaded in this test run."""
    try:
        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.close()
        return True
    except Exception:
        return False


VEC_AVAILABLE = vec_available_in_env()


def make_vec_conn(db_path: str = ":memory:") -> sqlite3.Connection:
    """Return a connection with schema + sqlite-vec extension loaded.

    Steps: connect, executescript SCHEMA, enable_load_extension,
    sqlite_vec.load, disable_load_extension, _ensure_vec_schema, commit.
    Raises if sqlite-vec is not available in this environment.
    """
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _ensure_vec_schema(conn)
    conn.commit()
    return conn


@pytest.fixture
def memory_db():
    """In-memory SQLite database with full v3 schema applied."""
    conn = sqlite3.connect(":memory:")
    # Match production (db.py enables this on every real connection). Without it,
    # a parent-before-child delete succeeds silently in tests but raises
    # IntegrityError at runtime where FK enforcement is on.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture(params=sorted(FIXTURE_DIR.glob("*.jsonl")), ids=lambda p: p.stem)
def jsonl_fixture(request):
    """Parameterized fixture yielding each JSONL file path."""
    return request.param
