"""Tests for structured error output (cli.errors)."""

import json

import pytest

from ccrecall.errors import emit_error, emit_error_return


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
