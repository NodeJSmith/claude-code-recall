"""Shared fixtures for ccrecall tests."""

import json
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

import ccrecall.config as config
import ccrecall.health as health
import ccrecall.hooks.sync_current as sync_current
from ccrecall.db_vec import _ensure_vec_schema
from ccrecall.health import clear_embedding_failure, record_embedding_failure
from ccrecall.schema import SCHEMA

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_runtime_dir(tmp_path, monkeypatch) -> None:
    """Redirect the known ccrecall runtime-dir write sites to this test's tmp_path.

    Closes #151: RUNTIME_DIR-derived paths are computed once, at import time
    (module-level constants in config.py/health.py, some also bound as
    def-time default-argument values). Patching config.RUNTIME_DIR alone
    doesn't reach any of those already-computed copies, so a test that
    forgets to pass an explicit path/db could silently write into the real,
    live ~/.ccrecall — which is exactly how a prior pytest run polluted real
    log files and, via #152, a real alert-snooze ledger entry.

    Patches the constants that production code actually re-reads at call
    time (health.py's sidecar functions now resolve their `path`/`marker_path`
    /`snooze_path` defaults dynamically — see health.py — so this monkeypatch
    reaches them even when a caller omits the argument) plus the two other
    concrete known real-file-write sites: config.py's own RUNTIME_DIR-reading
    functions (setup_logging, pid_file_path) and sync_current.py's
    by-value-imported DEFAULT_LOG_PATH (_warn_cold_model's cold-start log).

    Deliberately NOT covered: DEFAULT_DB_PATH is imported by value into ~9
    other modules (db.py, status.py, search_cli.py, the backfill/import
    hooks, cli/commands.py, ...) as a CLI default-argument value — patching
    config.DEFAULT_DB_PATH here wouldn't reach any of those copies either,
    and every existing DB test already passes an explicit db/memory_db, so
    there's no observed pollution vector to close. A future test that both
    omits --db AND exercises a real code path would still hit the real DB;
    if that ever happens, it's a #151-shaped follow-up, not silently covered
    by this fixture.
    """
    isolated = tmp_path / "ccrecall-runtime"
    monkeypatch.setattr(config, "RUNTIME_DIR", isolated)
    monkeypatch.setattr(config, "CONFIG_PATH", isolated / "config.json")
    monkeypatch.setattr(health, "EMBEDDING_STATUS_PATH", isolated / "embedding-status.json")
    monkeypatch.setattr(health, "ALERT_SNOOZE_PATH", isolated / "alert-snooze.json")
    monkeypatch.setattr(health, "BACKFILL_SCHEDULE_PATH", isolated / "backfill-schedule.json")
    monkeypatch.setattr(health, "_PROBE_MARKER_PATH", isolated / ".write-probe")
    monkeypatch.setattr(sync_current, "DEFAULT_LOG_PATH", isolated / "ccrecall.log")


def patched_record(sidecar: Path):
    """side_effect redirecting record_embedding_failure to a tmp sidecar path.

    Shared by the embedding-status recording tests in test_backfill_embeddings
    and test_sync_hook so the real ~/.ccrecall sidecar is never touched.
    """
    return lambda reason: record_embedding_failure(reason, path=sidecar)


def patched_clear(sidecar: Path):
    """side_effect redirecting clear_embedding_failure to a tmp sidecar path.

    Forwards any kwargs (e.g. `reasons=`) so callers that scope the clear —
    sync_current's vec-ok clear (#164) — still redirect to the tmp sidecar.
    """
    return lambda **kwargs: clear_embedding_failure(path=sidecar, **kwargs)


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
VEC_SKIP = pytest.mark.skipif(not VEC_AVAILABLE, reason="sqlite-vec not available in this environment")


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


def make_jsonl_entry(uuid: str, parent_uuid: str | None, ts: str, role: str, content) -> dict:
    return {
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "type": role,
        "timestamp": ts,
        "message": {"role": role, "content": content},
    }


def write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


class NoCloseConn:
    """Wrapper delegating to a sqlite3.Connection but making close() a no-op.

    Stands in for get_connection() (a @contextlib.contextmanager) via
    `patch(..., return_value=NoCloseConn(conn))` so the test keeps access to
    the same connection (and its rows) after run() returns.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        pass

    def __getattr__(self, name: str):
        return getattr(self._conn, name)
