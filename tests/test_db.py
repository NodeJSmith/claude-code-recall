"""Tests for ccrecall.db — schema creation, settings, and vec operations."""

import json
import sqlite3
import subprocess
import sys
import threading
import time
from typing import ClassVar
from unittest.mock import patch

import pytest
import sqlite_vec
from conftest import make_vec_conn

import ccrecall.config as config_module
import ccrecall.db as db_module
from ccrecall.config import (
    DEFAULT_SETTINGS,
    atomic_write_json,
    load_config,
    load_settings,
    log_hook_exception,
)
from ccrecall.db import (
    fetch_branch_messages,
    get_connection,
    vec_available,
)
from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.schema import SCHEMA, SCHEMA_CORE, SCHEMA_FTS5, detect_fts_support


class TestSchemaCreation:
    def test_all_tables_exist(self, memory_db):
        cursor = memory_db.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row[0] for row in cursor.fetchall()}
        expected = {
            "projects",
            "sessions",
            "branches",
            "messages",
            "branch_messages",
            "import_log",
        }
        assert expected.issubset(tables)

    def test_fts_tables_exist(self, memory_db):
        """messages_fts was removed as a dead index; branches_fts is the live keyword index."""
        cursor = memory_db.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_fts%'")
        fts_tables = {row[0] for row in cursor.fetchall()}
        assert "branches_fts" in fts_tables
        assert "messages_fts" not in fts_tables

    def test_schema_idempotent(self, memory_db):
        """Applying schema twice should not raise."""
        memory_db.executescript(SCHEMA)
        memory_db.commit()

    def test_insert_and_query(self, memory_db):
        """Basic insert/query roundtrip."""
        cursor = memory_db.cursor()
        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/home/user/project", "-home-user-project", "project"),
        )
        cursor.execute(
            "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
            ("sess-1", cursor.lastrowid),
        )
        memory_db.commit()
        cursor.execute("SELECT uuid FROM sessions")
        assert cursor.fetchone()[0] == "sess-1"


class TestLoadSettings:
    def test_always_returns_defaults(self, tmp_path, monkeypatch):
        """load_settings returns hardcoded defaults when no config file exists."""
        monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "no_config.json")
        settings = load_settings()
        assert settings == DEFAULT_SETTINGS

    def test_returns_copy(self):
        """Each call should return a fresh copy, not a reference."""
        s1 = load_settings()
        s2 = load_settings()
        s1["max_context_sessions"] = 99
        assert s2["max_context_sessions"] == 2

    def test_default_values(self):
        assert DEFAULT_SETTINGS["auto_inject_context"] is True
        assert DEFAULT_SETTINGS["max_context_sessions"] == 2
        assert DEFAULT_SETTINGS["logging_enabled"] is True
        assert DEFAULT_SETTINGS["log_level"] == "INFO"
        assert isinstance(DEFAULT_SETTINGS["exclude_projects"], list)
        assert DEFAULT_SETTINGS["alert_snooze_hours"] == 24


class TestLoadConfig:
    """load_config() must guard against malformed JSON written to CONFIG_PATH."""

    def test_returns_dict_for_valid_config(self, tmp_path, monkeypatch):
        """A well-formed JSON object is returned as-is."""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"auto_inject_context": False, "onboarding_completed": True}))
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", cfg)

        result = load_config()
        assert result == {"auto_inject_context": False, "onboarding_completed": True}

    def test_returns_empty_dict_for_json_array(self, tmp_path, monkeypatch):
        """A JSON array (not a dict) must return {} — prevents callers from crashing on .get()."""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps([1, 2, 3]))
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", cfg)

        assert load_config() == {}

    def test_returns_empty_dict_for_json_string(self, tmp_path, monkeypatch):
        """A JSON string must return {} — not a dict, should not propagate."""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps("hello"))
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", cfg)

        assert load_config() == {}

    def test_returns_empty_dict_for_json_null(self, tmp_path, monkeypatch):
        """JSON null must return {} — null is not a valid settings container."""
        cfg = tmp_path / "config.json"
        cfg.write_text("null")
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", cfg)

        assert load_config() == {}

    def test_returns_empty_dict_for_missing_file(self, tmp_path, monkeypatch):
        """Missing config file returns {} without raising."""
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", tmp_path / "nonexistent.json")

        assert load_config() == {}

    def test_returns_empty_dict_for_invalid_json(self, tmp_path, monkeypatch):
        """Corrupt JSON returns {} without raising."""
        cfg = tmp_path / "config.json"
        cfg.write_text("{bad json}")
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", cfg)

        assert load_config() == {}

    def test_unexpected_error_propagates(self, tmp_path, monkeypatch):
        """A non-OSError/ValueError (a real bug) must surface, not be masked as {} (issue #10)."""
        cfg = tmp_path / "config.json"
        cfg.write_text("{}")
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", cfg)

        with patch("ccrecall.config.json.loads", side_effect=TypeError("boom")), pytest.raises(TypeError):
            load_config()


class TestLogHookException:
    """log_hook_exception is a best-effort guard helper — it must never raise (issue #10)."""

    def test_does_not_raise_with_active_exception(self):
        """Called from inside an except block, it logs without re-raising."""
        try:
            raise ValueError("boom")
        except ValueError:
            log_hook_exception("test")  # must return normally

    def test_does_not_raise_when_logging_setup_fails(self):
        """Even if logging setup itself raises, the helper suppresses it and returns."""
        with patch("ccrecall.config.setup_logging", side_effect=RuntimeError("broke")):
            try:
                raise ValueError("boom")
            except ValueError:
                log_hook_exception("test")  # suppressed; must return normally


class TestAtomicWriteJson:
    """atomic_write_json is the single runtime-dir atomic-write helper."""

    def test_writes_json_with_trailing_newline(self, tmp_path):
        path = tmp_path / "out.json"
        atomic_write_json(path, {"a": 1})
        assert path.read_text() == json.dumps({"a": 1}, indent=2) + "\n"

    def test_no_tmp_orphan_on_success(self, tmp_path):
        atomic_write_json(tmp_path / "out.json", {})
        assert list(tmp_path.glob("*.tmp")) == []

    def test_creates_parent_dir(self, tmp_path):
        path = tmp_path / "sub" / "out.json"
        atomic_write_json(path, {})
        assert path.exists()

    def test_no_tmp_orphan_on_write_error(self, tmp_path):
        """A serialization failure must clean up the temp file and re-raise."""
        with pytest.raises(TypeError):
            atomic_write_json(tmp_path / "out.json", {"bad": object()})
        assert list(tmp_path.glob("*.tmp")) == []


class TestLoadSettingsWithConfig:
    """load_settings() must stay safe when config.json contains non-dict JSON."""

    def test_non_dict_config_returns_defaults(self, tmp_path, monkeypatch):
        """load_settings() returns DEFAULT_SETTINGS when config.json is a JSON array."""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps([]))
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", cfg)

        result = load_settings()
        assert result == DEFAULT_SETTINGS

    def test_config_overrides_applied(self, tmp_path, monkeypatch):
        """Valid config keys are merged into defaults."""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"auto_inject_context": False, "max_context_sessions": 5}))
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", cfg)

        result = load_settings()
        assert result["auto_inject_context"] is False
        assert result["max_context_sessions"] == 5
        assert result["logging_enabled"] is True  # unchanged default

    def test_alert_snooze_hours_override_and_default(self, tmp_path, monkeypatch):
        """The snooze window changes when alert_snooze_hours is set in config;
        the 24h default applies when the key is absent."""
        cfg = tmp_path / "config.json"
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", cfg)

        # Absent → default of 24 applies.
        cfg.write_text(json.dumps({"auto_inject_context": True}))
        assert load_settings()["alert_snooze_hours"] == 24

        # Present → the configured value flows through load_settings unchanged.
        cfg.write_text(json.dumps({"alert_snooze_hours": 12}))
        assert load_settings()["alert_snooze_hours"] == 12

    def test_logging_enabled_and_exclude_projects_honored(self, tmp_path, monkeypatch):
        """logging_enabled and exclude_projects are user-overridable from config.json."""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"logging_enabled": False, "exclude_projects": ["work-secret"]}))
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", cfg)

        result = load_settings()
        assert result["logging_enabled"] is False
        assert result["exclude_projects"] == ["work-secret"]

    def test_missing_config_returns_defaults(self, tmp_path, monkeypatch):
        """load_settings() returns DEFAULT_SETTINGS when config.json does not exist."""
        monkeypatch.setattr("ccrecall.config.CONFIG_PATH", tmp_path / "nonexistent.json")

        result = load_settings()
        assert result == DEFAULT_SETTINGS


# vec schema, columns, trigger, vec_available, load_vec


def _vec_available_in_env() -> bool:
    """Return True if the sqlite-vec extension can be loaded in this test run."""
    try:
        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.close()
        return True
    except Exception:
        return False


_VEC_AVAILABLE = _vec_available_in_env()


class TestVecAvailable:
    """vec_available(conn) returns bool and never raises."""

    def test_returns_bool(self):
        """vec_available always returns a bool regardless of extension availability."""
        conn = sqlite3.connect(":memory:")
        result = vec_available(conn)
        assert isinstance(result, bool)
        conn.close()

    def test_never_raises_on_attributeerror(self):
        """When enable_load_extension raises AttributeError, vec_available returns False.

        Uses a duck-typed mock because sqlite3.Connection C methods are read-only
        and cannot be patched via patch.object.
        """

        class _NoExtConn:
            def enable_load_extension(self, _flag):
                raise AttributeError("no extension support")

        result = vec_available(_NoExtConn())
        assert result is False

    def test_never_raises_on_operational_error(self):
        """When sqlite_vec.load raises OperationalError, vec_available returns False."""
        with patch("ccrecall.db.sqlite_vec") as mock_vec:
            mock_vec.load.side_effect = sqlite3.OperationalError("cannot load extension")

            class _FakeConn:
                def enable_load_extension(self, _flag):
                    pass

            result = vec_available(_FakeConn())
            assert result is False

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_returns_true_when_available(self):
        """Returns True when the extension loads successfully."""
        conn = sqlite3.connect(":memory:")
        result = vec_available(conn)
        assert result is True
        conn.close()


class TestVecSchema:
    """chunk_vec table and chunk cascade triggers — branch_vec torn down unconditionally."""

    def test_raw_no_vec_connection_unaffected(self):
        """The plain schema path never creates branch_vec or chunk_vec — vec schema is load_vec=True only."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        # Core tables must always be there
        assert "branches" in tables
        # Neither vec table appears via the plain migration path.
        assert "branch_vec" not in tables
        assert "chunk_vec" not in tables
        conn.close()

    def test_conftest_memory_db_fixture_works(self, memory_db):
        """The memory_db fixture (conftest) initializes cleanly — no 'no such module: vec0'."""
        cursor = memory_db.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='branches'")
        assert cursor.fetchone() is not None

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_branch_vec_absent_after_teardown(self):
        """branch_vec teardown: _ensure_vec_schema unconditionally drops branch_vec.

        A fresh make_vec_conn() runs _ensure_vec_schema, which must produce a DB
        where branch_vec does NOT exist — the table is unconditionally dropped
        even on a first-time schema run where it was never present (DROP IF EXISTS).
        """
        conn = make_vec_conn()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "branch_vec" not in tables, "branch_vec must be absent after T06 teardown"
        conn.close()

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_branches_vec_ad_trigger_absent_and_chunk_triggers_present(self):
        """branch_vec teardown: branches_vec_ad is dropped; branches_chunks_ad + chunks_vec_ad are present."""
        conn = make_vec_conn()
        triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
        assert "branches_vec_ad" not in triggers, "branches_vec_ad must be dropped by T06 teardown"
        assert "branches_chunks_ad" in triggers, "branches_chunks_ad must exist after _ensure_vec_schema"
        assert "chunks_vec_ad" in triggers, "chunks_vec_ad must exist after _ensure_vec_schema"
        conn.close()

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_existing_branch_vec_dropped_and_watermarks_reset(self):
        """When branch_vec existed before _ensure_vec_schema, it is dropped and watermarks reset to 0.

        Simulates a real-world upgrade: an existing DB with branch_vec and a branch
        watermarked at the current EMBEDDING_VERSION. After _ensure_vec_schema:
        - branch_vec absent
        - branches.embedding_version reset to 0 (stale branch-level watermark gone)
        - chunk_vec exists and accepts inserts
        """
        # Build a DB with branch_vec manually (pre-teardown state)
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS branch_vec USING vec0(branch_id INTEGER PRIMARY KEY, embedding float[{EMBEDDING_DIM}])"
        )
        conn.execute("INSERT INTO projects (path, key, name) VALUES ('/p', '-p', 'p')")
        conn.execute("INSERT INTO sessions (uuid, project_id) VALUES ('s-pre', 1)")
        conn.execute(
            "INSERT INTO branches (session_id, leaf_uuid, embedding_version) VALUES (1, 'lf-pre', ?)",
            (EMBEDDING_VERSION,),
        )
        conn.commit()

        # Confirm pre-teardown state
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='branch_vec'").fetchone() is not None
        assert (
            conn.execute("SELECT embedding_version FROM branches WHERE leaf_uuid='lf-pre'").fetchone()[0]
            == EMBEDDING_VERSION
        )

        # Run _ensure_vec_schema — must tear down branch_vec and reset watermarks
        db_module._ensure_vec_schema(conn)
        conn.commit()

        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "branch_vec" not in tables, "branch_vec must be dropped by _ensure_vec_schema"
        assert "chunk_vec" in tables, "chunk_vec must exist after _ensure_vec_schema"

        wm = conn.execute("SELECT embedding_version FROM branches WHERE leaf_uuid='lf-pre'").fetchone()[0]
        assert wm == 0, "embedding_version must be reset to 0 when branch_vec is torn down"
        conn.close()

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_ensure_vec_schema_idempotent_no_branch_vec(self):
        """Running _ensure_vec_schema twice when branch_vec was never present: watermarks untouched.

        The first run does DROP TABLE IF EXISTS (no-op, branch_vec absent) so no
        watermark reset fires. Second run is the same. A branch watermarked at the
        current version must not have its watermark zeroed.
        """
        conn = make_vec_conn()
        # Seed a branch with current watermark — branch_vec was never present
        conn.execute("INSERT INTO projects (path, key, name) VALUES ('/p-idem', '-p-idem', 'p-idem')")
        conn.execute("INSERT INTO sessions (uuid, project_id) VALUES ('s-idem', 1)")
        conn.execute(
            "INSERT INTO branches (session_id, leaf_uuid, embedding_version) VALUES (1, 'lf-idem', ?)",
            (EMBEDDING_VERSION,),
        )
        conn.commit()

        # Seed a chunk_vec row so we can verify it survives
        conn.execute("INSERT INTO chunks (branch_id, exchange_index, content_hash) VALUES (1, 0, 'h-idem')")
        chunk_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, sqlite_vec.serialize_float32([0.5] * EMBEDDING_DIM)),
        )
        conn.commit()

        # Second run — must be a no-op for chunk_vec and watermarks
        db_module._ensure_vec_schema(conn)
        conn.commit()

        wm = conn.execute("SELECT embedding_version FROM branches WHERE leaf_uuid='lf-idem'").fetchone()[0]
        assert wm == EMBEDDING_VERSION, "Idempotent run must not reset watermarks when branch_vec was absent"
        count = conn.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0]
        assert count == 1, "Existing chunk_vec row must survive an idempotent _ensure_vec_schema call"
        conn.close()


class TestLoadVecParameter:
    """get_connection(load_vec=...) parameter behavior."""

    def test_default_connection_initializes_cleanly(self, tmp_path, monkeypatch):
        """get_connection() with default load_vec=False returns a working connection."""
        db_file = tmp_path / "conversations.db"
        monkeypatch.setattr(config_module, "DEFAULT_DB_PATH", db_file)

        with get_connection() as conn:
            # Core tables must exist
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert "branches" in tables
            assert "sessions" in tables

            # Three new columns must exist
            cols = {row[1] for row in conn.execute("PRAGMA table_info(branches)").fetchall()}
            assert "embedding_version" in cols
            assert "embedding_model" in cols
            assert "summary_version_at_embed" in cols

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_load_vec_true_allows_chunk_vec_query(self, tmp_path, monkeypatch):
        """get_connection(load_vec=True) returns a connection that can query chunk_vec."""
        db_file = tmp_path / "conversations.db"
        monkeypatch.setattr(config_module, "DEFAULT_DB_PATH", db_file)

        with get_connection(load_vec=True) as conn:
            # chunk_vec must be queryable (extension loaded, branch_vec absent by teardown)
            count = conn.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0]
            assert count == 0
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert "branch_vec" not in tables, "branch_vec must be absent after T06 teardown"

    def test_load_vec_false_default_does_not_require_extension(self, tmp_path, monkeypatch):
        """get_connection() default path works even on machines where vec is unavailable.

        This test always passes — it verifies the non-load_vec path does not
        touch branch_vec in a way that would require the extension.
        """
        db_file = tmp_path / "conversations.db"
        monkeypatch.setattr(config_module, "DEFAULT_DB_PATH", db_file)

        with get_connection() as conn:
            # Must be able to read branches without touching branch_vec
            count = conn.execute("SELECT COUNT(*) FROM branches").fetchone()[0]
            assert count == 0


class TestGetConnectionContextManager:
    """get_connection() as a context manager — commit-on-success, rollback/close-on-exception."""

    def test_connection_closed_on_exception(self, tmp_path):
        """A connection opened via get_connection() is closed even when the with-block raises.

        Characterizes the sync_current.py leak this context manager fixes: the
        old raw-connection pattern left the connection open (and any
        in-progress write uncommitted) whenever the caller's work raised before
        reaching an explicit conn.close(). get_connection() must close on every
        exit path, exception included.
        """
        db_path = tmp_path / "test.db"
        conn_holder: dict = {}

        def _raise_inside_with():
            with get_connection({"db_path": str(db_path)}) as conn:
                conn_holder["conn"] = conn
                conn.execute("SELECT 1")
                raise ValueError("simulate failure")

        with pytest.raises(ValueError, match="simulate failure"):
            _raise_inside_with()

        with pytest.raises(sqlite3.ProgrammingError):
            conn_holder["conn"].execute("SELECT 1")

    def test_connection_closed_on_success(self, tmp_path):
        """A connection opened via get_connection() is committed and closed on normal exit."""
        db_path = tmp_path / "test.db"
        with get_connection({"db_path": str(db_path)}) as conn:
            conn.execute("SELECT 1")
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def _seed_v0_db_with_dead_branches(db_path) -> None:
    """Build a pre-migration (v0) conversation DB matching a real upgrade DB's shape.

    Uses SCHEMA_CORE plus the FTS5 schema as it looked before this migration
    (messages_fts and its messages_ai/ad/au triggers — since removed from
    schema.py but still present on disk for anyone upgrading from before this
    change). Seeds one active and one inactive ("churn") branch for the same
    session, each wired to a branch_messages row and a chunks row — the exact
    shape the v1 migration must delete, and the exact shape the old
    UNIQUE(session_id, leaf_uuid) constraint used to allow that the new
    UNIQUE(session_id) constraint no longer does.
    """
    pre_migration_fts5 = (
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
          content, content=messages, content_rowid=id, tokenize='porter unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
          INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
          INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
          INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
          INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """
        + SCHEMA_FTS5
    )

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_CORE)
    conn.executescript(pre_migration_fts5)
    conn.commit()

    conn.execute("INSERT INTO projects (path, key, name) VALUES ('/p-v0', '-p-v0', 'p-v0')")
    proj_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO sessions (uuid, project_id) VALUES ('sess-v0', ?)", (proj_id,))
    sess_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'leaf-active', 1)", (sess_id,))
    active_branch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'leaf-churn', 0)", (sess_id,))
    inactive_branch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO messages (session_id, uuid, role, content) VALUES (?, 'msg-v0', 'user', 'hi')", (sess_id,)
    )
    msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute("INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (active_branch_id, msg_id))
    conn.execute("INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (inactive_branch_id, msg_id))

    conn.execute(
        "INSERT INTO chunks (branch_id, exchange_index, content_hash) VALUES (?, 0, 'hash-active')",
        (active_branch_id,),
    )
    conn.execute(
        "INSERT INTO chunks (branch_id, exchange_index, content_hash) VALUES (?, 0, 'hash-inactive')",
        (inactive_branch_id,),
    )

    conn.commit()
    conn.close()


def _seed_v0_db_with_dead_branch_chunk_vec(db_path) -> None:
    """Extend _seed_v0_db_with_dead_branches with a real vec-loaded chunk_vec row.

    Mirrors production usage: an embedding write connection (load_vec=True)
    creates chunk_vec and its cascade triggers (branches_chunks_ad,
    chunks_vec_ad) and writes a real vector for the dead branch's chunk. Those
    triggers persist on disk regardless of which connection created them, so a
    later non-vec connection (load_vec=False — the mainline path for most CLI
    commands and non-embedding hooks) reopening this DB must still purge the
    dead branch/chunk/chunk_vec rows correctly instead of crashing with
    "no such module: vec0" when the purge's DELETE fires the cascade.
    """
    _seed_v0_db_with_dead_branches(db_path)

    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec"
        f" USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{EMBEDDING_DIM}])"
    )
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
    inactive_chunk_id = conn.execute("SELECT id FROM chunks WHERE content_hash = 'hash-inactive'").fetchone()[0]
    conn.execute(
        "INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)",
        (inactive_chunk_id, sqlite_vec.serialize_float32([0.1] * EMBEDDING_DIM)),
    )
    conn.commit()
    conn.close()


def _seed_v1_db_with_orphan_messages(db_path) -> None:
    """Build a post-v1, pre-v2 DB: fork_point_uuid still present, an orphan message exists.

    Mirrors the exact post-v1 shape the v2 migration must handle:
    `fork_point_uuid` is still on `branches` (v1 never drops it — only v2's
    rebuild does), and `messages` carries a row with no `branch_messages`
    reference (linked only to a branch v1 already deleted) — the exact
    population v2's orphan purge exists to clean up.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_CORE)
    conn.executescript(SCHEMA_FTS5)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    conn.execute("INSERT INTO projects (path, key, name) VALUES ('/p-v1', '-p-v1', 'p-v1')")
    proj_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO sessions (uuid, project_id) VALUES ('sess-v1', ?)", (proj_id,))
    sess_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO branches (session_id, leaf_uuid, fork_point_uuid, is_active) VALUES (?, 'leaf-v1', NULL, 1)",
        (sess_id,),
    )
    branch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO messages (session_id, uuid, role, content) VALUES (?, 'msg-linked', 'user', 'hi')", (sess_id,)
    )
    linked_msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO messages (session_id, uuid, role, content) VALUES (?, 'msg-orphan', 'user', 'orphaned')",
        (sess_id,),
    )

    conn.execute("INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (branch_id, linked_msg_id))

    conn.commit()
    conn.close()


class TestSchemaVersioning:
    """PRAGMA user_version schema versioning and the v1 dead-branch migration."""

    def test_fresh_db_user_version_matches_schema_version(self, tmp_path):
        """A freshly created DB is stamped with SCHEMA_VERSION on first connection."""
        db_path = tmp_path / "fresh.db"
        with get_connection(settings={"db_path": str(db_path)}) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION

    def test_fresh_db_has_no_fork_point_uuid_column(self, tmp_path):
        """A fresh install runs v1 (creates fork_point_uuid) then v2 (drops it) — the column is absent at rest."""
        db_path = tmp_path / "fresh_v2.db"
        with get_connection(settings={"db_path": str(db_path)}) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
            columns = {row[1] for row in conn.execute("PRAGMA table_info(branches)").fetchall()}
            assert "fork_point_uuid" not in columns

    def test_migration_from_v1_drops_fork_point_uuid_and_purges_orphans(self, tmp_path):
        """A v1 DB with fork_point_uuid and an orphan message is migrated to v2 on first connection."""
        db_path = tmp_path / "v1_to_v2.db"
        _seed_v1_db_with_orphan_messages(db_path)

        with get_connection(settings={"db_path": str(db_path)}) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 2

            columns = {row[1] for row in conn.execute("PRAGMA table_info(branches)").fetchall()}
            assert "fork_point_uuid" not in columns

            orphan_count = conn.execute(
                "SELECT COUNT(*) FROM messages m"
                " LEFT JOIN branch_messages bm ON bm.message_id = m.id"
                " WHERE bm.message_id IS NULL"
            ).fetchone()[0]
            assert orphan_count == 0

            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    def test_migration_from_v0_purges_dead_branches_and_rebuilds(self, tmp_path):
        """A v0 DB seeded with a churn (inactive) branch row is cleaned on first connection.

        Covers dead branch rows purged in FK-safe order, messages_fts dropped
        while branches_fts is preserved and still trigger-synced, and the
        UNIQUE(session_id, leaf_uuid) -> UNIQUE(session_id) constraint change
        from the branches table rebuild.
        """
        db_path = tmp_path / "legacy.db"
        _seed_v0_db_with_dead_branches(db_path)

        with get_connection(settings={"db_path": str(db_path)}) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION

            assert conn.execute("SELECT COUNT(*) FROM branches WHERE is_active = 0").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM branches WHERE is_active = 1").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM branch_messages").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1

            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert "messages_fts" not in tables
            assert "branches_fts" in tables

            # branches_fts sync triggers were re-created after the rebuild — an
            # UPDATE must still land in the FTS index.
            conn.execute("UPDATE branches SET aggregated_content = 'hello world' WHERE is_active = 1")
            match = conn.execute("SELECT rowid FROM branches_fts WHERE branches_fts MATCH 'hello'").fetchall()
            assert len(match) == 1

            # UNIQUE(session_id) now rejects a second row for the same session —
            # the old UNIQUE(session_id, leaf_uuid) would have allowed this.
            sess_id = conn.execute("SELECT session_id FROM branches WHERE is_active = 1").fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
                conn.execute(
                    "INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'dup-leaf', 1)",
                    (sess_id,),
                )

            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_migration_purges_orphaned_chunk_vec_without_crashing(self, tmp_path):
        """The mainline load_vec=False migration path must not crash on a real upgrade DB.

        Regression test for a reproduced CRITICAL bug: a DB that already has a
        vec-loaded chunk_vec row + chunks_vec_ad cascade trigger for a dead
        branch's chunk (the exact population the v1 migration exists to purge)
        used to raise `sqlite3.OperationalError: no such module: vec0` the
        first time a non-vec connection (get_connection's default,
        load_vec=False — most CLI commands and non-embedding hooks) reopened
        it, because the purge's DELETE fired the on-disk cascade trigger
        without vec0 registered on that connection.
        """
        db_path = tmp_path / "legacy_with_vec.db"
        _seed_v0_db_with_dead_branch_chunk_vec(db_path)

        with get_connection(settings={"db_path": str(db_path)}, load_vec=False) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
            assert conn.execute("SELECT COUNT(*) FROM branches WHERE is_active = 0").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
            # The dead chunk's chunk_vec row is orphaned by the purge — it must
            # be cleaned up too, not just left dangling or erroring.
            assert conn.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0] == 0

    def test_migration_is_reentrant(self, tmp_path):
        """Re-running the migration (a second get_connection call) is a no-op."""
        db_path = tmp_path / "legacy_reentrant.db"
        _seed_v0_db_with_dead_branches(db_path)

        with get_connection(settings={"db_path": str(db_path)}) as conn:
            first_version = conn.execute("PRAGMA user_version").fetchone()[0]
            first_active = conn.execute("SELECT COUNT(*) FROM branches WHERE is_active = 1").fetchone()[0]

        with get_connection(settings={"db_path": str(db_path)}) as conn:
            second_version = conn.execute("PRAGMA user_version").fetchone()[0]
            second_active = conn.execute("SELECT COUNT(*) FROM branches WHERE is_active = 1").fetchone()[0]

        assert first_version == second_version == db_module.SCHEMA_VERSION
        assert first_active == second_active == 1

    def test_migration_toctou_race_runs_migration_once(self, tmp_path):
        """Two connections racing to open the same v0 DB must not both migrate.

        Regression test for a reproduced TOCTOU: the pre-fix code read
        `PRAGMA user_version` once *before* acquiring BEGIN IMMEDIATE, so a
        second connection blocked waiting for the write lock would still act
        on that stale, unmigrated read once the lock was granted — re-running
        _migrate_to_v1 after the first connection had already committed the
        migration. The fix (db.py:389-390) re-reads user_version under the
        lock before deciding whether to migrate. This test forces the race
        with an artificial delay inside a patched _migrate_to_v1 and asserts
        it runs exactly once even when two threads open the same v0 DB
        concurrently.
        """
        db_path = tmp_path / "legacy_race.db"
        _seed_v0_db_with_dead_branches(db_path)

        # Stamp the file as WAL up front so both threads' own journal_mode=WAL
        # pragma (in apply_base_pragmas) is a same-mode no-op rather than a
        # second, unrelated lock race over the mode switch itself — this test
        # targets the user_version re-read race in _apply_migrations, not
        # first-time WAL conversion.
        warmup = sqlite3.connect(db_path)
        db_module.apply_base_pragmas(warmup)
        warmup.close()

        call_count = 0
        count_lock = threading.Lock()
        original_migrate = db_module._migrate_to_v1

        def slow_migrate(conn):
            nonlocal call_count
            with count_lock:
                call_count += 1
            time.sleep(0.2)
            original_migrate(conn)

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def open_connection():
            try:
                barrier.wait()
                with get_connection(settings={"db_path": str(db_path)}) as conn:
                    conn.execute("SELECT 1")
            except Exception as exc:
                errors.append(exc)

        with patch.object(db_module, "_migrate_to_v1", side_effect=slow_migrate):
            threads = [threading.Thread(target=open_connection) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        assert not errors, f"unexpected errors in racing threads: {errors}"
        assert call_count == 1

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_vec_self_heal_runs_outside_version_gate(self, tmp_path):
        """_ensure_vec_schema's self-heal runs on every vec-loaded connection, not just when migrating.

        Once a DB is already at SCHEMA_VERSION, _apply_migrations is a no-op on
        every later connection. Dropping chunk_vec directly (bypassing
        get_connection) after the first vec-loaded connection simulates the
        kind of drift _ensure_vec_schema heals (e.g. a stale embedding
        dimension). A second vec-loaded connection must still recreate it even
        though no migration ran — proving the self-heal isn't gated by the
        version check.
        """
        db_path = tmp_path / "selfheal.db"
        with get_connection(settings={"db_path": str(db_path)}, load_vec=True) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
            assert db_module.chunk_vec_queryable(conn)

        raw = sqlite3.connect(db_path)
        raw.enable_load_extension(True)
        sqlite_vec.load(raw)
        raw.enable_load_extension(False)
        raw.execute("DROP TABLE chunk_vec")
        raw.commit()
        raw.close()

        with get_connection(settings={"db_path": str(db_path)}, load_vec=True) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
            assert db_module.chunk_vec_queryable(conn), "chunk_vec must be re-created by the self-heal, not migration"


class TestSchemaEquivalencePin:
    """Characterization pin — guards the migrations squash to v6 baseline.

    This pin captures the schema a fresh conversation DB produces via the
    production get_connection path and asserts it matches an inline expected
    literal.  SCHEMA_CORE now carries the embedding DDL and migrations.py is gone,
    so a fresh DB matches this snapshot exactly — the schema is the v6 baseline
    minus the intentionally-removed token_snapshots table.

    Exclusion rule: we exclude from the snapshot any table whose name contains
    '_fts_' (those are FTS5 shadow tables auto-created alongside the virtual FTS
    tables — e.g. branches_fts_idx) and sqlite_* internals.  The FTS virtual
    table itself (branches_fts) does NOT contain '_fts_' so it IS included.
    messages_fts is gone entirely (dropped by the
    version-1 migration and removed from schema.py) so it is absent from both
    the table set and the exclusion rule.
    """

    # Expected schema captured from the production SCHEMA_CORE + SCHEMA_FTS5 output.
    # token_snapshots is intentionally absent — SCHEMA_CORE and get_connection no
    # longer create it; the exclusion clause keeps this literal stable on legacy DBs
    # that still carry the table.
    EXPECTED_TABLES: ClassVar[list[str]] = [
        "branch_messages",
        "branches",
        "branches_fts",
        "chunks",
        "import_log",
        "messages",
        "projects",
        "sessions",
    ]

    # Per-table column info: (cid, name, type, notnull, dflt_value, pk)
    EXPECTED_COLUMNS: ClassVar[dict[str, list[tuple]]] = {
        "branch_messages": [
            (0, "branch_id", "INTEGER", 1, None, 1),
            (1, "message_id", "INTEGER", 1, None, 2),
        ],
        "branches": [
            (0, "id", "INTEGER", 0, None, 1),
            (1, "session_id", "INTEGER", 1, None, 0),
            (2, "leaf_uuid", "TEXT", 1, None, 0),
            (3, "is_active", "INTEGER", 0, "1", 0),
            (4, "started_at", "DATETIME", 0, None, 0),
            (5, "ended_at", "DATETIME", 0, None, 0),
            (6, "exchange_count", "INTEGER", 0, "0", 0),
            (7, "files_modified", "TEXT", 0, None, 0),
            (8, "commits", "TEXT", 0, None, 0),
            (9, "tool_counts", "TEXT", 0, None, 0),
            (10, "aggregated_content", "TEXT", 0, None, 0),
            (11, "context_summary", "TEXT", 0, None, 0),
            (12, "context_summary_json", "TEXT", 0, None, 0),
            (13, "summary_version", "INTEGER", 0, "0", 0),
            (14, "embedding_version", "INTEGER", 0, "0", 0),
            (15, "embedding_model", "TEXT", 0, None, 0),
            (16, "summary_version_at_embed", "INTEGER", 0, None, 0),
        ],
        "branches_fts": [
            (0, "aggregated_content", "", 0, None, 0),
        ],
        "chunks": [
            (0, "id", "INTEGER", 0, None, 1),
            (1, "branch_id", "INTEGER", 1, None, 0),
            (2, "exchange_index", "INTEGER", 1, None, 0),
            (3, "content_hash", "TEXT", 1, None, 0),
            (4, "first_message_uuid", "TEXT", 0, None, 0),
            (5, "timestamp", "TEXT", 0, None, 0),
            (6, "user_text", "TEXT", 0, None, 0),
            (7, "assistant_text", "TEXT", 0, None, 0),
            (8, "was_capped", "INTEGER", 1, "0", 0),
            (9, "embedding_version", "INTEGER", 1, "0", 0),
            (10, "embedding_model", "TEXT", 0, None, 0),
        ],
        "import_log": [
            (0, "id", "INTEGER", 0, None, 1),
            (1, "file_path", "TEXT", 1, None, 0),
            (2, "file_hash", "TEXT", 0, None, 0),
            (3, "imported_at", "DATETIME", 0, "CURRENT_TIMESTAMP", 0),
            (4, "messages_imported", "INTEGER", 0, "0", 0),
        ],
        "messages": [
            (0, "id", "INTEGER", 0, None, 1),
            (1, "session_id", "INTEGER", 1, None, 0),
            (2, "uuid", "TEXT", 0, None, 0),
            (3, "parent_uuid", "TEXT", 0, None, 0),
            (4, "timestamp", "DATETIME", 0, None, 0),
            (5, "role", "TEXT", 0, None, 0),
            (6, "content", "TEXT", 1, None, 0),
            (7, "tool_summary", "TEXT", 0, None, 0),
            (8, "has_tool_use", "INTEGER", 0, "0", 0),
            (9, "has_thinking", "INTEGER", 0, "0", 0),
            (10, "is_notification", "INTEGER", 0, "0", 0),
            (11, "origin", "TEXT", 0, None, 0),
        ],
        "projects": [
            (0, "id", "INTEGER", 0, None, 1),
            (1, "path", "TEXT", 1, None, 0),
            (2, "key", "TEXT", 1, None, 0),
            (3, "name", "TEXT", 0, None, 0),
            (4, "created_at", "DATETIME", 0, "CURRENT_TIMESTAMP", 0),
        ],
        "sessions": [
            (0, "id", "INTEGER", 0, None, 1),
            (1, "uuid", "TEXT", 1, None, 0),
            (2, "project_id", "INTEGER", 0, None, 0),
            (3, "parent_session_id", "INTEGER", 0, None, 0),
            (4, "git_branch", "TEXT", 0, None, 0),
            (5, "cwd", "TEXT", 0, None, 0),
            (6, "imported_at", "DATETIME", 0, "CURRENT_TIMESTAMP", 0),
        ],
    }

    EXPECTED_IDX_INDEXES: ClassVar[list[str]] = [
        "idx_branch_messages_message",
        "idx_branches_active",
        "idx_branches_embedding_version",
        "idx_branches_session",
        "idx_branches_summary_version",
        "idx_chunks_branch",
        "idx_chunks_version",
        "idx_messages_session",
        "idx_messages_session_uuid",
        "idx_messages_timestamp",
        "idx_projects_key",
        "idx_sessions_project",
    ]

    def test_schema_snapshot_fts5(self, tmp_path):
        """Pin: fresh conv DB schema matches the expected literal (FTS5 path).

        Only runs when FTS5 is available — mirrors how other test_db.py tests
        guard FTS-specific assertions via detect_fts_support.
        """
        with get_connection(settings={"db_path": str(tmp_path / "conv.db")}) as conn:
            fts = detect_fts_support(conn)
            if fts != "fts5":
                pytest.skip("FTS5 not available in this SQLite build")

            cursor = conn.cursor()

            # Tables: exclude token_snapshots, sqlite_* internals, and FTS shadow tables
            # (shadow tables contain '_fts_' in their name, e.g. branches_fts_idx).
            # The branches_fts virtual table does NOT match '_fts_%' so it is
            # correctly included. messages_fts no longer exists.
            cursor.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table'
                AND name NOT LIKE 'sqlite_%'
                AND name != 'token_snapshots'
                AND name NOT LIKE '%_fts_%'
                ORDER BY name
            """)
            actual_tables = [row[0] for row in cursor.fetchall()]
            assert actual_tables == self.EXPECTED_TABLES, (
                f"Table set mismatch.\nExpected: {self.EXPECTED_TABLES}\nActual:   {actual_tables}"
            )

            # Per-table column info (preserves column order)
            for tbl in actual_tables:
                cursor.execute(f"PRAGMA table_info({tbl})")
                actual_cols = [tuple(row) for row in cursor.fetchall()]
                assert actual_cols == self.EXPECTED_COLUMNS[tbl], (
                    f"Column mismatch for table '{tbl}'.\nExpected: {self.EXPECTED_COLUMNS[tbl]}\n"
                    f"Actual:   {actual_cols}"
                )

            # idx_* indexes only (skip sqlite auto-indexes and token_snapshots indexes)
            cursor.execute("""
                SELECT name FROM sqlite_master
                WHERE type='index'
                AND name LIKE 'idx_%'
                AND name NOT LIKE 'idx_token_%'
                ORDER BY name
            """)
            actual_indexes = [row[0] for row in cursor.fetchall()]
            assert actual_indexes == self.EXPECTED_IDX_INDEXES, (
                f"Index set mismatch.\nExpected: {self.EXPECTED_IDX_INDEXES}\nActual:   {actual_indexes}"
            )


class TestEmbeddingDDLInSchema:
    """A fresh DB built from SCHEMA alone has the three embedding columns and index.

    This verifies that SCHEMA_CORE (and therefore SCHEMA) is the complete schema
    source — SCHEMA alone provides the embedding columns.
    """

    def test_embedding_columns_last_three_in_schema_only_db(self):
        """SCHEMA-only fresh DB has embedding columns as last three PRAGMA table_info rows."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(branches)")
        rows = cursor.fetchall()
        # Each row: (cid, name, type, notnull, dflt_value, pk)
        # (name, type, dflt_value, pk) — dflt_value guards the DEFAULT 0 on embedding_version
        last_three = [(row[1], row[2], row[4], row[5]) for row in rows[-3:]]
        assert last_three == [
            ("embedding_version", "INTEGER", "0", 0),
            ("embedding_model", "TEXT", None, 0),
            ("summary_version_at_embed", "INTEGER", None, 0),
        ], f"Last three columns were: {last_three}"

        conn.close()

    def test_embedding_version_index_in_schema_only_db(self):
        """SCHEMA-only fresh DB has idx_branches_embedding_version index."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_branches_embedding_version'")
        assert cursor.fetchone() is not None, "idx_branches_embedding_version index not found"

        conn.close()


class TestExistingV6DbOpen:
    """Opening a pre-populated v6-style DB succeeds and leaves all rows intact."""

    def test_existing_v6_db_rows_intact_after_get_connection(self, tmp_path):
        """Reopen an existing v6 DB: get_connection must not drop or overwrite any table or row."""
        db_file = tmp_path / "existing_v6.db"

        # Build a DB that looks like an existing v6 conversation DB
        setup_conn = sqlite3.connect(str(db_file))
        setup_conn.executescript(SCHEMA)
        setup_conn.commit()

        # Insert one row per FK-chain link: projects → sessions → branches, messages
        setup_conn.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/home/user/proj", "-home-user-proj", "proj"),
        )
        proj_id = setup_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        setup_conn.execute(
            "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
            ("sess-v6-ac4", proj_id),
        )
        sess_id = setup_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        setup_conn.execute(
            "INSERT INTO branches (session_id, leaf_uuid) VALUES (?, ?)",
            (sess_id, "leaf-v6-ac4"),
        )
        setup_conn.execute(
            "INSERT INTO messages (session_id, uuid, role, content) VALUES (?, ?, ?, ?)",
            (sess_id, "msg-uuid-1", "user", "hello"),
        )
        setup_conn.commit()

        setup_conn.execute("PRAGMA user_version = 6")
        setup_conn.commit()
        setup_conn.close()

        # Reopen via get_connection
        with get_connection(settings={"db_path": str(db_file)}) as conn:
            assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM branches").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


class TestChunkSchema:
    """chunks table + chunk_vec virtual table — additive schema additions."""

    def test_chunks_table_exists_in_schema_core(self, memory_db):
        """chunks table is created by SCHEMA_CORE (plain path, no vec extension needed)."""
        tables = {row[0] for row in memory_db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "chunks" in tables

    def test_chunks_table_indexes_exist(self, memory_db):
        """idx_chunks_branch and idx_chunks_version exist in SCHEMA_CORE."""
        indexes = {row[0] for row in memory_db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "idx_chunks_branch" in indexes
        assert "idx_chunks_version" in indexes

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_chunk_vec_exists_after_ensure_vec_schema(self):
        """chunk_vec virtual table is created by _ensure_vec_schema."""
        conn = make_vec_conn()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "chunk_vec" in tables
        conn.close()

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_branch_vec_absent_chunk_vec_present_after_teardown(self):
        """branch_vec teardown: branch_vec absent, chunk_vec present after _ensure_vec_schema."""
        conn = make_vec_conn()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "branch_vec" not in tables, "branch_vec must be torn down by T06"
        assert "chunk_vec" in tables, "chunk_vec must be present after _ensure_vec_schema"
        conn.close()

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_both_cascade_triggers_exist(self):
        """branches_chunks_ad and chunks_vec_ad triggers exist after _ensure_vec_schema."""
        conn = make_vec_conn()
        triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
        assert "branches_chunks_ad" in triggers
        assert "chunks_vec_ad" in triggers
        conn.close()

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_two_level_cascade_delete(self):
        """deleting a branch row removes all its chunks rows and their chunk_vec rows."""
        conn = make_vec_conn()
        cursor = conn.cursor()

        cursor.execute("INSERT INTO projects (path, key, name) VALUES (?, ?, ?)", ("/p-casc", "-p-casc", "p-casc"))
        proj_id = cursor.lastrowid
        cursor.execute("INSERT INTO sessions (uuid, project_id) VALUES (?, ?)", ("sess-casc", proj_id))
        sess_id = cursor.lastrowid
        cursor.execute("INSERT INTO branches (session_id, leaf_uuid) VALUES (?, ?)", (sess_id, "leaf-casc"))
        branch_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO chunks (branch_id, exchange_index, content_hash) VALUES (?, ?, ?)",
            (branch_id, 0, "hash-0"),
        )
        chunk_id_0 = cursor.lastrowid
        cursor.execute(
            "INSERT INTO chunks (branch_id, exchange_index, content_hash) VALUES (?, ?, ?)",
            (branch_id, 1, "hash-1"),
        )
        chunk_id_1 = cursor.lastrowid

        vec = sqlite_vec.serialize_float32([0.1] * EMBEDDING_DIM)
        cursor.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (chunk_id_0, vec))
        cursor.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (chunk_id_1, vec))
        conn.commit()

        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE branch_id = ?", (branch_id,)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0] == 2

        conn.execute("DELETE FROM branches WHERE id = ?", (branch_id,))
        conn.commit()

        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE branch_id = ?", (branch_id,)).fetchone()[0] == 0, (
            "branches_chunks_ad trigger must remove chunks rows when a branch is deleted"
        )
        assert conn.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0] == 0, (
            "chunks_vec_ad trigger must remove chunk_vec rows when chunks are deleted"
        )
        conn.close()

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_chunk_vec_stale_dim_rebuilds_and_resets_watermarks(self):
        """A stale-dim chunk_vec is rebuilt at EMBEDDING_DIM and branch watermarks reset to 0.

        Per design.md "chunk_vec drop resets watermarks": dropping chunk_vec (e.g. an
        embedding-model swap) leaves branches reporting EMBEDDING_VERSION while their
        vectors are gone, so _ensure_vec_schema must zero branches.embedding_version
        (the repurposed per-branch chunk watermark) to force backfill repopulation.
        """
        conn = make_vec_conn()
        cursor = conn.cursor()

        # Seed a branch already at the current watermark.
        cursor.execute("INSERT INTO projects (path, key, name) VALUES (?, ?, ?)", ("/p-heal", "-p-heal", "p-heal"))
        proj_id = cursor.lastrowid
        cursor.execute("INSERT INTO sessions (uuid, project_id) VALUES (?, ?)", ("sess-heal", proj_id))
        sess_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO branches (session_id, leaf_uuid, embedding_version, embedding_model) VALUES (?, ?, ?, ?)",
            (sess_id, "leaf-heal", EMBEDDING_VERSION, EMBEDDING_MODEL),
        )

        # Replace chunk_vec with a stale-dim one carrying a row.
        stale_dim = EMBEDDING_DIM * 2
        conn.execute("DROP TRIGGER IF EXISTS chunks_vec_ad")
        conn.execute("DROP TABLE chunk_vec")
        conn.execute(
            f"CREATE VIRTUAL TABLE chunk_vec USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{stale_dim}])"
        )
        conn.execute(
            "INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)",
            (1, sqlite_vec.serialize_float32([0.1] * stale_dim)),
        )
        conn.commit()

        db_module._ensure_vec_schema(conn)
        conn.commit()

        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='chunk_vec'").fetchone()[0]
        assert f"float[{EMBEDDING_DIM}]" in sql
        assert f"float[{stale_dim}]" not in sql
        # Watermark reset to 0 so backfill repopulates the dropped vectors.
        wm = conn.execute("SELECT embedding_version FROM branches WHERE leaf_uuid = ?", ("leaf-heal",)).fetchone()[0]
        assert wm == 0, "chunk_vec drop must reset branches.embedding_version watermark to 0"
        conn.close()

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_write_chunk_embedding_round_trip(self):
        """write_chunk_embedding writes the vector FIRST, then the chunk's version/model bookkeeping."""
        conn = make_vec_conn()
        cursor = conn.cursor()

        cursor.execute("INSERT INTO projects (path, key, name) VALUES (?, ?, ?)", ("/p-wce", "-p-wce", "p-wce"))
        proj_id = cursor.lastrowid
        cursor.execute("INSERT INTO sessions (uuid, project_id) VALUES (?, ?)", ("sess-wce", proj_id))
        sess_id = cursor.lastrowid
        cursor.execute("INSERT INTO branches (session_id, leaf_uuid) VALUES (?, ?)", (sess_id, "leaf-wce"))
        branch_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO chunks (branch_id, exchange_index, content_hash) VALUES (?, ?, ?)",
            (branch_id, 0, "hash-wce"),
        )
        chunk_id = cursor.lastrowid
        conn.commit()

        db_module.write_chunk_embedding(cursor, chunk_id, [0.4] * EMBEDDING_DIM, EMBEDDING_VERSION, EMBEDDING_MODEL)
        conn.commit()

        # Vector written.
        assert conn.execute("SELECT COUNT(*) FROM chunk_vec WHERE chunk_id = ?", (chunk_id,)).fetchone()[0] == 1
        # Bookkeeping written on the chunk row.
        ver, model = conn.execute(
            "SELECT embedding_version, embedding_model FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        assert ver == EMBEDDING_VERSION
        assert model == EMBEDDING_MODEL
        conn.close()

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_upsert_chunk_vec_replaces_without_error(self):
        """upsert_chunk_vec (DELETE+INSERT) replaces an existing row without error."""
        conn = make_vec_conn()
        cursor = conn.cursor()

        cursor.execute("INSERT INTO projects (path, key, name) VALUES (?, ?, ?)", ("/p-up", "-p-up", "p-up"))
        proj_id = cursor.lastrowid
        cursor.execute("INSERT INTO sessions (uuid, project_id) VALUES (?, ?)", ("sess-up-chunk", proj_id))
        sess_id = cursor.lastrowid
        cursor.execute("INSERT INTO branches (session_id, leaf_uuid) VALUES (?, ?)", (sess_id, "leaf-up-chunk"))
        branch_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO chunks (branch_id, exchange_index, content_hash) VALUES (?, ?, ?)",
            (branch_id, 0, "hash-up"),
        )
        chunk_id = cursor.lastrowid
        conn.commit()

        embedding = [0.5] * EMBEDDING_DIM
        db_module.upsert_chunk_vec(cursor, chunk_id, embedding)
        conn.commit()

        # Second call — must not raise on repeat
        db_module.upsert_chunk_vec(cursor, chunk_id, embedding)
        conn.commit()

        count = conn.execute("SELECT COUNT(*) FROM chunk_vec WHERE chunk_id = ?", (chunk_id,)).fetchone()[0]
        assert count == 1, "upsert_chunk_vec must produce exactly one row after repeated calls"
        conn.close()


class TestChunkVecQueryable:
    """chunk_vec_queryable(conn) — probes chunk_vec existence (successor to the removed branch_vec probe)."""

    def test_returns_false_without_vec(self):
        """chunk_vec_queryable returns False when chunk_vec does not exist."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()
        assert db_module.chunk_vec_queryable(conn) is False
        conn.close()

    @pytest.mark.skipif(not _VEC_AVAILABLE, reason="sqlite-vec not available in this environment")
    def test_returns_true_with_vec_loaded(self):
        """chunk_vec_queryable returns True when chunk_vec exists and is queryable."""
        conn = make_vec_conn()
        assert db_module.chunk_vec_queryable(conn) is True
        conn.close()


class TestFetchBranchMessagesUuid:
    """fetch_branch_messages must return the uuid field — additive extension."""

    def test_returns_uuid_field(self, memory_db):
        """fetch_branch_messages returns a 'uuid' key in each message dict."""
        cursor = memory_db.cursor()

        cursor.execute("INSERT INTO projects (path, key, name) VALUES (?, ?, ?)", ("/p-fbm", "-p-fbm", "p-fbm"))
        proj_id = cursor.lastrowid
        cursor.execute("INSERT INTO sessions (uuid, project_id) VALUES (?, ?)", ("sess-fbm", proj_id))
        sess_id = cursor.lastrowid
        cursor.execute("INSERT INTO branches (session_id, leaf_uuid) VALUES (?, ?)", (sess_id, "leaf-fbm"))
        branch_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO messages (session_id, uuid, role, content) VALUES (?, ?, ?, ?)",
            (sess_id, "msg-uuid-test", "user", "hello"),
        )
        msg_id = cursor.lastrowid
        cursor.execute("INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (branch_id, msg_id))
        memory_db.commit()

        messages = fetch_branch_messages(cursor, branch_id, include_notifications=False)

        assert len(messages) == 1
        assert "uuid" in messages[0], "fetch_branch_messages must include 'uuid' key in each message dict"
        assert messages[0]["uuid"] == "msg-uuid-test"


class TestTransitiveImportIsolation:
    """config.py, health.py, and hooks/memory_sync.py must stay free of the heavy
    fastembed/onnxruntime/sqlite_vec stack.

    Each module is imported in a fresh subprocess (not the test process, which
    has already loaded sqlite_vec via ccrecall.db) so sys.modules reflects only
    what that one import pulled in.
    """

    HEAVY_MODULES: ClassVar[str] = "{'fastembed', 'onnxruntime', 'sqlite_vec'}"

    def _assert_no_heavy_imports(self, module_name: str) -> None:
        code = (
            f"import {module_name}\n"
            "import sys\n"
            f"heavy = {self.HEAVY_MODULES}\n"
            "found = heavy & set(sys.modules)\n"
            "assert not found, f'Heavy modules loaded: {found}'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    def test_config_does_not_import_heavy_deps(self):
        self._assert_no_heavy_imports("ccrecall.config")

    def test_health_does_not_import_heavy_deps(self):
        """health.py imports only from config.py, not db.py."""
        self._assert_no_heavy_imports("ccrecall.health")

    def test_memory_sync_does_not_import_heavy_deps(self):
        """memory_sync.py imports only from config.py."""
        self._assert_no_heavy_imports("ccrecall.hooks.memory_sync")

    def test_clear_handoff_does_not_import_heavy_deps(self):
        """clear_handoff.py imports only from config.py."""
        self._assert_no_heavy_imports("ccrecall.hooks.clear_handoff")
