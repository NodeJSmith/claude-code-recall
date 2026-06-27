"""Shared fixtures for ccrecall tests."""

import sqlite3
from pathlib import Path

import pytest
import sqlite_vec
from token_helpers import TOKEN_JNL, token_session, token_turn

from ccrecall.db import _ensure_vec_schema
from ccrecall.health import clear_embedding_failure, record_embedding_failure
from ccrecall.schema import SCHEMA
from ccrecall.token_analytics import import_session
from ccrecall.token_parser import ToolCall
from ccrecall.token_schema import ensure_schema

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def patched_record(sidecar: Path):
    """side_effect redirecting record_embedding_failure to a tmp sidecar path.

    Shared by the T02 embedding-status recording tests in test_backfill_embeddings
    and test_sync_hook so the real ~/.ccrecall sidecar is never touched.
    """
    return lambda reason: record_embedding_failure(reason, path=sidecar)


def patched_clear(sidecar: Path):
    """side_effect redirecting clear_embedding_failure to a tmp sidecar path."""
    return lambda: clear_embedding_failure(path=sidecar)


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


@pytest.fixture
def token_db():
    """In-memory DB with the token-ingest schema and no data."""
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def populated_token_db():
    """Token DB with a known dataset, so read-side aggregates are assertable.

    Two top-level sessions, three turns total:
      s1: turn1 (in=100, out=50, one Read tool call), turn2 (in=200, out=80)
      s2: turn1 (in=300, out=120)
    Totals: sessions=2, turns=3, output=250, input=600, tool calls=1.
    """
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    import_session(
        conn,
        token_session(
            "s1",
            [
                token_turn(1, input_tokens=100, output_tokens=50, tool_calls=[ToolCall("Read", "t1")]),
                token_turn(2, input_tokens=200, output_tokens=80),
            ],
        ),
        TOKEN_JNL,
    )
    import_session(conn, token_session("s2", [token_turn(1, input_tokens=300, output_tokens=120)]), TOKEN_JNL)
    conn.commit()
    yield conn
    conn.close()
