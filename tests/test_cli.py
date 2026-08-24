"""Tests for the `ccrecall backfill schedule` subcommand group and the
`ccrecall backfill embeddings --dismiss` flag.

BACKFILL_SCHEDULE_PATH is one of the sidecar paths tests/conftest.py's autouse
`_isolated_runtime_dir` fixture redirects to a tmp_path location. The local
`_isolated_schedule_path` fixture below re-patches the same attribute to a
second, module-local tmp_path — harmless (the centralized fixture already
prevents any leak into the real ~/.ccrecall), kept for a stable, predictable
path across this file's assertions.
"""

import json
from unittest.mock import patch

import pytest

import ccrecall.health as health
from ccrecall.cli.commands import cmd_backfill_embeddings, cmd_schedule_clear, cmd_schedule_status, cmd_schedule_write
from ccrecall.cli.context import CLIContext
from ccrecall.config import load_settings_for_db
from ccrecall.db import get_connection
from ccrecall.hooks.backfill_query import EXIT_OK, PID_KEY


@pytest.fixture(autouse=True)
def _isolated_schedule_path(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "BACKFILL_SCHEDULE_PATH", tmp_path / "backfill-schedule.json")


class TestScheduleWrite:
    def test_writes_configured_at_marker(self, capsys):
        cmd_schedule_write()

        data = json.loads(health.BACKFILL_SCHEDULE_PATH.read_text())
        assert "configured_at" in data
        assert "wrote" in capsys.readouterr().out.lower()

    def test_overwrites_existing_marker(self):
        health.write_schedule_marker("dismissed_at")
        cmd_schedule_write()

        data = json.loads(health.BACKFILL_SCHEDULE_PATH.read_text())
        assert "configured_at" in data
        assert "dismissed_at" not in data


class TestScheduleClear:
    def test_removes_existing_marker(self, capsys):
        health.write_schedule_marker("configured_at")

        cmd_schedule_clear()

        assert not health.BACKFILL_SCHEDULE_PATH.exists()
        assert "removed" in capsys.readouterr().out.lower()

    def test_no_op_when_absent(self, capsys):
        cmd_schedule_clear()

        assert not health.BACKFILL_SCHEDULE_PATH.exists()
        assert "no backfill schedule marker" in capsys.readouterr().out.lower()


class TestScheduleStatus:
    def test_reports_no_marker(self, tmp_path, capsys):
        cmd_schedule_status(db=tmp_path / "status.db")

        out = capsys.readouterr().out
        assert "not set" in out.lower()
        assert "draft-quality chunks remaining: 0" in out.lower()

    def test_reports_configured_marker(self, tmp_path, capsys):
        health.write_schedule_marker("configured_at")

        cmd_schedule_status(db=tmp_path / "status.db")

        out = capsys.readouterr().out
        assert "configured" in out.lower()

    def test_reports_dismissed_marker(self, tmp_path, capsys):
        health.write_schedule_marker("dismissed_at")

        cmd_schedule_status(db=tmp_path / "status.db")

        out = capsys.readouterr().out
        assert "dismissed" in out.lower()

    def test_reports_draft_quality_chunk_count(self, tmp_path, capsys):
        db_path = tmp_path / "status.db"
        with get_connection(load_settings_for_db(db_path), load_vec=False) as conn:
            conn.execute("INSERT INTO sessions (uuid) VALUES ('s1')")
            session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'l1', 1)", (session_id,))
            branch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO chunks (branch_id, exchange_index, content_hash, cap_tokens) VALUES (?, 0, 'h', 4096)",
                (branch_id,),
            )

        cmd_schedule_status(db=db_path)

        out = capsys.readouterr().out
        assert "draft-quality chunks remaining: 1" in out.lower()


class TestBackfillEmbeddingsDismiss:
    def test_dismiss_writes_marker_and_returns_without_running_backfill(self, capsys):
        with (
            patch("ccrecall.cli.commands.try_acquire_pid_file") as mock_acquire,
            patch("ccrecall.cli.commands.backfill_embeddings_mod.run") as mock_run,
        ):
            cmd_backfill_embeddings(dismiss=True)

        data = json.loads(health.BACKFILL_SCHEDULE_PATH.read_text())
        assert "dismissed_at" in data
        mock_acquire.assert_not_called()
        mock_run.assert_not_called()
        assert "dismissed" in capsys.readouterr().out.lower()

    def test_dismiss_does_not_raise_systemexit(self):
        """Unlike the normal backfill path, --dismiss returns normally (exit 0 implicitly)."""
        with patch("ccrecall.cli.commands.backfill_embeddings_mod.run"):
            cmd_backfill_embeddings(dismiss=True)  # must not raise

    def test_non_dismiss_run_untouched(self):
        """dismiss=False (the default) preserves the existing PID-guard/run wiring."""
        with (
            patch("ccrecall.cli.commands.try_acquire_pid_file", return_value=True) as mock_acquire,
            patch("ccrecall.cli.commands.backfill_embeddings_mod.run", return_value=EXIT_OK) as mock_run,
            patch("ccrecall.cli.commands.backfill_query_mod.cleanup_pid"),
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_backfill_embeddings(dismiss=False)

        assert exc_info.value.code == EXIT_OK
        mock_acquire.assert_called_once_with(PID_KEY)
        mock_run.assert_called_once()
        assert not health.BACKFILL_SCHEDULE_PATH.exists()


JSON_CTX = CLIContext(json_mode=True)


class TestScheduleJsonMode:
    def test_write_json(self, capsys):
        cmd_schedule_write(ctx=JSON_CTX)

        data = json.loads(capsys.readouterr().out)
        assert data["action"] == "write"
        assert "path" in data

    def test_clear_json_existed(self, capsys):
        health.write_schedule_marker("configured_at")
        cmd_schedule_clear(ctx=JSON_CTX)

        data = json.loads(capsys.readouterr().out)
        assert data == {"action": "clear", "existed": True}

    def test_clear_json_not_existed(self, capsys):
        cmd_schedule_clear(ctx=JSON_CTX)

        data = json.loads(capsys.readouterr().out)
        assert data == {"action": "clear", "existed": False}

    def test_status_json(self, tmp_path, capsys):
        health.write_schedule_marker("configured_at")
        cmd_schedule_status(db=tmp_path / "status.db", ctx=JSON_CTX)

        data = json.loads(capsys.readouterr().out)
        assert "configured_at" in data["marker"]
        assert data["draft_quality_remaining"] == 0

    def test_dismiss_json(self, capsys):
        with (
            patch("ccrecall.cli.commands.try_acquire_pid_file"),
            patch("ccrecall.cli.commands.backfill_embeddings_mod.run"),
        ):
            cmd_backfill_embeddings(dismiss=True, ctx=JSON_CTX)

        data = json.loads(capsys.readouterr().out)
        assert data["action"] == "dismiss"
        assert "path" in data
