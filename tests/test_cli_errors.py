"""Tests for structured error output (cli.errors)."""

import errno
import json
import logging
import logging.handlers
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from ccrecall.cli import active_log_path, launcher
from ccrecall.errors import (
    CODE_CORRUPT,
    CODE_DATABASE_ERROR,
    CODE_DISK_FULL,
    CODE_FILESYSTEM_ERROR,
    CODE_LOCKED,
    CODE_SCHEMA_AHEAD,
    classify_failure,
    emit_error,
    emit_error_return,
)

LOG = Path("/tmp/ccrecall-cli.log")


def _sqlite_error(cls: type[sqlite3.Error], message: str, code: int) -> sqlite3.Error:
    exc = cls(message)
    exc.sqlite_errorcode = code  # set by SQLite itself on a real failure
    return exc


def _classify(exc: sqlite3.Error | OSError, *, schema_ahead_cause: str = "", log_path: Path | None = LOG):
    return classify_failure(exc, operation="ccrecall search", schema_ahead_cause=schema_ahead_cause, log_path=log_path)


class TestEmitError:
    def test_writes_json_to_stderr(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            emit_error("bad input", code="invalid_arg", exit_code=2)
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        envelope = json.loads(err)
        assert envelope["error"] == "bad input"
        assert envelope["code"] == "invalid_arg"
        assert envelope["exit_code"] == 2

    def test_includes_remediation_when_provided(self, capsys):
        with pytest.raises(SystemExit):
            emit_error("db missing", code="db_not_found", exit_code=1, remediation="Run ccrecall import")
        envelope = json.loads(capsys.readouterr().err)
        assert envelope["remediation"] == "Run ccrecall import"

    def test_omits_remediation_when_none(self, capsys):
        with pytest.raises(SystemExit):
            emit_error("fail", code="generic", exit_code=1)
        envelope = json.loads(capsys.readouterr().err)
        assert "remediation" not in envelope

    def test_nothing_on_stdout(self, capsys):
        with pytest.raises(SystemExit):
            emit_error("fail", code="generic", exit_code=1)
        assert capsys.readouterr().out == ""


class TestEmitErrorReturn:
    def test_returns_exit_code(self, capsys):
        code = emit_error_return("oops", code="test", exit_code=2)
        assert code == 2

    def test_writes_json_to_stderr(self, capsys):
        emit_error_return("oops", code="test", exit_code=2, remediation="try again")
        envelope = json.loads(capsys.readouterr().err)
        assert envelope["error"] == "oops"
        assert envelope["remediation"] == "try again"

    def test_does_not_raise(self):
        code = emit_error_return("oops", code="test", exit_code=1)
        assert code == 1


class TestClassifyFailure:
    def test_locked_database(self):
        report = _classify(_sqlite_error(sqlite3.OperationalError, "database is locked", sqlite3.SQLITE_BUSY))
        assert report.code == CODE_LOCKED
        assert report.message == "ccrecall search failed: the database is locked by another ccrecall process"

    def test_sqlite_disk_full(self):
        report = _classify(_sqlite_error(sqlite3.OperationalError, "database or disk is full", sqlite3.SQLITE_FULL))
        assert report.code == CODE_DISK_FULL

    def test_os_disk_full(self):
        assert _classify(OSError(errno.ENOSPC, "No space left on device")).code == CODE_DISK_FULL

    @pytest.mark.parametrize("code", [sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB])
    def test_corrupt_database(self, code):
        report = _classify(_sqlite_error(sqlite3.DatabaseError, "file is not a database", code))
        assert report.code == CODE_CORRUPT
        assert "file is not a database" in report.message
        assert "ccrecall import" in report.remediation

    def test_schema_ahead_wins_over_the_exception_class(self):
        exc = _sqlite_error(sqlite3.OperationalError, "database is locked", sqlite3.SQLITE_BUSY)
        report = _classify(exc, schema_ahead_cause="the database is at schema v99, but this install expects v9")
        assert report.code == CODE_SCHEMA_AHEAD
        assert "schema v99" in report.message
        assert "Upgrade ccrecall" in report.remediation

    def test_other_sqlite_error_falls_back(self):
        report = _classify(sqlite3.IntegrityError("UNIQUE constraint failed: projects.path"))
        assert report.code == CODE_DATABASE_ERROR
        assert "UNIQUE constraint failed: projects.path" in report.message

    def test_other_os_error_falls_back(self):
        report = _classify(PermissionError(errno.EACCES, "Permission denied", "/x/conversations.db"))
        assert report.code == CODE_FILESYSTEM_ERROR
        assert "Permission denied" in report.message

    def test_remediation_names_the_log(self):
        assert str(LOG) in _classify(sqlite3.IntegrityError("x")).remediation

    def test_remediation_without_a_log(self):
        assert "logging_enabled" in _classify(sqlite3.IntegrityError("x"), log_path=None).remediation


class TestCommandErrorBoundary:
    """launcher turns a database error escaping a command into one line, exit 1, traceback logged."""

    def _run_failing_command(self, tmp_path, *, json_mode: bool = False) -> None:
        def _raise(**_kwargs):
            raise sqlite3.DatabaseError("file is not a database")

        with (
            patch("ccrecall.config.load_settings", return_value={"db_path": str(tmp_path / "conversations.db")}),
            patch("ccrecall.cli.commands.import_mod.run", side_effect=_raise),
            pytest.raises(SystemExit) as exc_info,
        ):
            launcher("import", json_mode=json_mode)
        assert exc_info.value.code == 1

    def test_prints_one_envelope_line_and_no_traceback(self, tmp_path, capsys):
        self._run_failing_command(tmp_path)

        captured = capsys.readouterr()
        lines = captured.err.strip().splitlines()
        assert len(lines) == 1
        envelope = json.loads(lines[0])
        assert envelope["error"].startswith("ccrecall import failed:")
        assert envelope["exit_code"] == 1
        assert "Traceback" not in captured.err
        assert captured.out == ""

    def test_json_mode_uses_the_same_envelope(self, tmp_path, capsys):
        self._run_failing_command(tmp_path, json_mode=True)

        envelope = json.loads(capsys.readouterr().err)
        assert envelope["error"].startswith("ccrecall import failed:")

    def test_logs_the_traceback(self, tmp_path, caplog):
        with caplog.at_level(logging.ERROR, logger="ccrecall"):
            self._run_failing_command(tmp_path)

        assert any(record.exc_info for record in caplog.records if record.levelno >= logging.ERROR)

    def test_broken_pipe_is_not_reported_as_a_failure(self, tmp_path, capsys):
        with (
            patch("ccrecall.config.load_settings", return_value={"db_path": str(tmp_path / "conversations.db")}),
            patch("ccrecall.cli.commands.import_mod.run", side_effect=BrokenPipeError),
            pytest.raises(BrokenPipeError),
        ):
            launcher("import")
        assert capsys.readouterr().err == ""

    def test_other_exceptions_still_propagate(self, tmp_path):
        with (
            patch("ccrecall.config.load_settings", return_value={"db_path": str(tmp_path / "conversations.db")}),
            patch("ccrecall.cli.commands.import_mod.run", side_effect=RuntimeError("bug")),
            pytest.raises(RuntimeError, match="bug"),
        ):
            launcher("import")


class TestActiveLogPath:
    def test_returns_the_rotating_file(self, tmp_path):
        logger = logging.getLogger("ccrecall-test-active-log")
        handler = logging.handlers.RotatingFileHandler(tmp_path / "ccrecall-cli.log")
        logger.addHandler(handler)
        try:
            assert active_log_path(logger) == tmp_path / "ccrecall-cli.log"
        finally:
            logger.removeHandler(handler)
            handler.close()

    def test_none_when_logging_is_disabled(self):
        logger = logging.getLogger("ccrecall-test-no-log")
        logger.addHandler(logging.NullHandler())
        assert active_log_path(logger) is None
