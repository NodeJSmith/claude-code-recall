"""Tests for ccrecall.legacy and the SessionStart hook wiring around it.

The migration carries a pre-rename install (~/.claude-memory) forward into
~/.ccrecall. A bug here either strands a user's synced history (the hook
re-imports from scratch) or clobbers a live database — both are the kind of
silent data loss the conversations DB contract exists to prevent.
"""

import io
import json
import sqlite3
import sys
from pathlib import Path

import sqlite_vec

import ccrecall.db as _db_mod
import ccrecall.hooks.memory_setup as memory_setup
import ccrecall.hooks.onboarding as onboarding
import ccrecall.legacy as legacy
from ccrecall.schema import SCHEMA


def _patch_paths(monkeypatch, home: Path) -> tuple[Path, Path]:
    """Point legacy + db constants at a temp home. Returns (new_db, legacy_dir)."""
    new_db = home / ".ccrecall" / "conversations.db"
    new_cfg = home / ".ccrecall" / "config.json"
    legacy_dir = home / ".claude-memory"
    monkeypatch.setattr(legacy, "DEFAULT_DB_PATH", new_db)
    monkeypatch.setattr(legacy, "CONFIG_PATH", new_cfg)
    monkeypatch.setattr(legacy, "LEGACY_DATA_DIRS", [legacy_dir])
    # run_migration -> get_db_connection() reads db's own module constant.
    monkeypatch.setattr(_db_mod, "DEFAULT_DB_PATH", new_db)
    return new_db, legacy_dir


def _make_legacy_db(legacy_dir: Path, *, vec_dim: int = 1024) -> Path:
    """Create a minimal legacy conversations.db with a branch_vec at vec_dim."""
    legacy_dir.mkdir(parents=True, exist_ok=True)
    db = legacy_dir / "conversations.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS branch_vec USING vec0(branch_id INTEGER PRIMARY KEY, embedding float[{vec_dim}])"
    )
    conn.execute("INSERT INTO projects(id,path,key,name) VALUES (1,'/p','p','p')")
    conn.execute("INSERT INTO sessions(id,uuid,project_id) VALUES (1,'u1',1)")
    conn.execute(
        "INSERT INTO branches(id,session_id,leaf_uuid,is_active,context_summary,summary_version,embedding_version,embedding_model)"
        " VALUES (1,1,'lf',1,'hi',1,1,'gpahal/bge-m3-onnx-int8')"
    )
    conn.commit()
    conn.close()
    return db


class TestFindLegacyDb:
    def test_finds_nonempty_legacy_when_current_absent(self, tmp_path, monkeypatch):
        _, legacy_dir = _patch_paths(monkeypatch, tmp_path)
        db = _make_legacy_db(legacy_dir)
        assert legacy.find_legacy_db() == db

    def test_none_when_current_db_exists(self, tmp_path, monkeypatch):
        new_db, legacy_dir = _patch_paths(monkeypatch, tmp_path)
        _make_legacy_db(legacy_dir)
        new_db.parent.mkdir(parents=True, exist_ok=True)
        new_db.touch()
        # Never clobber a live current DB, even if a legacy one is present.
        assert legacy.find_legacy_db() is None

    def test_none_when_legacy_empty_or_missing(self, tmp_path, monkeypatch):
        _, legacy_dir = _patch_paths(monkeypatch, tmp_path)
        assert legacy.find_legacy_db() is None  # no legacy dir at all
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "conversations.db").touch()  # 0-byte sentinel, not a real DB
        assert legacy.find_legacy_db() is None


class TestPortableConfig:
    def test_drops_unknown_keys_keeps_known(self):
        kept = legacy.portable_config(
            {
                "onboarding_completed": True,
                "onboarding_version": 1,
                "auto_inject_context": False,
                "consolidation_min_hours": 24,
                "consolidation_reminder_enabled": True,
            }
        )
        assert kept == {"onboarding_completed": True, "onboarding_version": 1, "auto_inject_context": False}


class TestCopyHelpers:
    def test_copy_db_preserves_original_and_refuses_overwrite(self, tmp_path, monkeypatch):
        new_db, legacy_dir = _patch_paths(monkeypatch, tmp_path)
        src = _make_legacy_db(legacy_dir)

        assert legacy.copy_legacy_db(src) is True
        assert new_db.exists()
        assert src.exists()  # non-destructive: original left in place

        # Second call must not clobber the now-live current DB.
        assert legacy.copy_legacy_db(src) is False

    def test_copy_config_no_op_when_current_exists_or_src_missing(self, tmp_path, monkeypatch):
        new_db, legacy_dir = _patch_paths(monkeypatch, tmp_path)
        legacy_dir.mkdir(parents=True)
        assert legacy.copy_legacy_config(legacy_dir / "config.json") is False  # src missing

        new_cfg = new_db.parent / "config.json"
        new_cfg.parent.mkdir(parents=True, exist_ok=True)
        new_cfg.write_text("{}")
        (legacy_dir / "config.json").write_text(json.dumps({"auto_inject_context": True}))
        assert legacy.copy_legacy_config(legacy_dir / "config.json") is False  # current exists


class TestRunMigration:
    def test_full_migration_tears_down_branch_vec_and_filters_config(self, tmp_path, monkeypatch):
        new_db, legacy_dir = _patch_paths(monkeypatch, tmp_path)
        # _make_legacy_db seeds a branch_vec at 1024 (stale dim) and a branch with
        # embedding_version=1 (stale watermark). After migration:
        # - branch_vec must be ABSENT (T06 teardown via _ensure_vec_schema)
        # - chunk_vec must EXIST (created by _ensure_vec_schema)
        # - branches.embedding_version must be 0 (reset because branch_vec existed)
        src = _make_legacy_db(legacy_dir, vec_dim=1024)
        (legacy_dir / "config.json").write_text(
            json.dumps({"onboarding_completed": True, "onboarding_version": 1, "consolidation_min_hours": 9})
        )

        assert legacy.run_migration() == 0
        assert new_db.exists()
        assert src.exists()

        cfg = json.loads((new_db.parent / "config.json").read_text())
        assert "consolidation_min_hours" not in cfg
        assert cfg["onboarding_completed"] is True

        conn = sqlite3.connect(new_db)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "branch_vec" not in tables, "branch_vec must be torn down during migration"
        assert "chunk_vec" in tables, "chunk_vec must exist after migration"
        # Stale branch-level watermark must be reset so backfill re-embeds at chunk grain.
        wm = conn.execute("SELECT embedding_version FROM branches WHERE leaf_uuid = 'lf'").fetchone()[0]
        conn.close()
        assert wm == 0, "embedding_version must be 0 after branch_vec teardown clears old watermarks"

    def test_idempotent_second_run_is_noop(self, tmp_path, monkeypatch):
        _, legacy_dir = _patch_paths(monkeypatch, tmp_path)
        _make_legacy_db(legacy_dir)
        assert legacy.run_migration() == 0
        assert legacy.run_migration() == 0  # current DB now exists -> nothing to do

    def test_no_legacy_is_noop(self, tmp_path, monkeypatch):
        _patch_paths(monkeypatch, tmp_path)
        assert legacy.run_migration() == 0


def _fail_if_called(*_a, **_k):
    raise AssertionError("_needs_backfill must not run during migration (it would create the DB)")


def _run_memory_setup(monkeypatch) -> tuple[dict, list]:
    """Run memory_setup.main(), returning (parsed output, list of spawned argvs)."""
    spawned: list = []
    monkeypatch.setattr(memory_setup, "_spawn_background", lambda argv, pid_key: spawned.append(argv))
    monkeypatch.setattr(memory_setup, "_reap_stale_temp_files", lambda: None)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    memory_setup.main()
    monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    return json.loads(buf.getvalue()), spawned


class TestMemorySetupHook:
    def test_legacy_present_spawns_migrate_and_injects_notice(self, tmp_path, monkeypatch):
        new_db = tmp_path / ".ccrecall" / "conversations.db"  # absent
        monkeypatch.setattr(memory_setup, "DEFAULT_DB_PATH", new_db)
        monkeypatch.setattr(memory_setup, "find_legacy_db", lambda: tmp_path / ".claude-memory" / "conversations.db")
        # If the backfill probe ran it would open/create the DB; assert it doesn't.
        monkeypatch.setattr(memory_setup, "_needs_backfill", _fail_if_called)

        out, spawned = _run_memory_setup(monkeypatch)

        # migrate must be spawned; fresh import must NOT be (migrate-only path).
        # warm-model is also spawned on every SessionStart — both are expected.
        assert ["ccrecall", "migrate"] in spawned, "migrate must be spawned on legacy path"
        assert ["ccrecall", "import"] not in spawned, "fresh import must not run alongside migrate"
        assert ["ccrecall-warm-model"] in spawned, "model warm must be spawned"
        assert out["continue"] is True
        assert "claude-memory" in out["hookSpecificOutput"]["additionalContext"]
        assert not new_db.exists()  # never created an empty DB that migrate would refuse

    def test_no_legacy_fresh_install_spawns_import(self, tmp_path, monkeypatch):
        new_db = tmp_path / ".ccrecall" / "conversations.db"  # absent
        monkeypatch.setattr(memory_setup, "DEFAULT_DB_PATH", new_db)
        monkeypatch.setattr(memory_setup, "find_legacy_db", lambda: None)
        monkeypatch.setattr(memory_setup, "_needs_backfill", lambda s: False)

        out, spawned = _run_memory_setup(monkeypatch)

        # import must be spawned; warm-model is also spawned on every SessionStart.
        assert ["ccrecall", "import"] in spawned, "fresh import must be spawned"
        assert ["ccrecall-warm-model"] in spawned, "model warm must be spawned"
        assert "hookSpecificOutput" not in out  # no migration notice on a clean install


class TestOnboardingDefersToMigration:
    def test_silent_when_legacy_present_and_not_onboarded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_db_mod, "CONFIG_PATH", tmp_path / "nonexistent.json")  # not onboarded
        monkeypatch.setattr(onboarding, "find_legacy_db", lambda: tmp_path / ".claude-memory" / "conversations.db")
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        onboarding.main()
        monkeypatch.setattr(sys, "stdout", sys.__stdout__)
        assert json.loads(buf.getvalue()) == {}  # migration shows its own notice instead

    def test_injects_normally_when_no_legacy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_db_mod, "CONFIG_PATH", tmp_path / "nonexistent.json")
        monkeypatch.setattr(onboarding, "find_legacy_db", lambda: None)
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        onboarding.main()
        monkeypatch.setattr(sys, "stdout", sys.__stdout__)
        assert "hookSpecificOutput" in json.loads(buf.getvalue())
