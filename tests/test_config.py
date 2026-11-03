"""Tests for ccrecall.config — logging formatter and default settings."""

import logging
import subprocess
import sys

import pytest

from ccrecall.config import (
    DEFAULT_SETTINGS,
    _ExtraFieldsFormatter,
    is_ccrecall_process,
    live_pid_holder,
    pid_file_path,
    resolve_backfill_stale_hours,
    try_acquire_pid_file,
)


def _format(record_kwargs: dict, extra: dict | None = None) -> str:
    formatter = _ExtraFieldsFormatter("%(levelname)s - %(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=record_kwargs.get("msg", "a message"),
        args=(),
        exc_info=None,
    )
    if extra:
        for key, value in extra.items():
            setattr(record, key, value)
    return formatter.format(record)


class TestExtraFieldsFormatter:
    def test_renders_extra_fields_as_key_value_pairs(self):
        rendered = _format(
            {"msg": "skipped malformed JSONL lines"}, extra={"skipped_count": 3, "source": "/tmp/foo.jsonl"}
        )

        assert rendered == "WARNING - skipped malformed JSONL lines | skipped_count=3 source='/tmp/foo.jsonl'"

    def test_no_extra_fields_renders_base_message_unchanged(self):
        rendered = _format({"msg": "no extras here"})

        assert rendered == "WARNING - no extras here"

    def test_extra_fields_sorted_for_deterministic_output(self):
        rendered = _format({"msg": "m"}, extra={"zeta": 1, "alpha": 2})

        assert rendered == "WARNING - m | alpha=2 zeta=1"


class TestDefaultSettings:
    def test_sync_path_token_limit_present_with_default_value(self):
        assert DEFAULT_SETTINGS["sync_path_token_limit"] == 4096

    def test_backfill_stale_hours_defaults_to_72(self):
        assert DEFAULT_SETTINGS["backfill_stale_hours"] == 72


class TestResolveBackfillStaleHours:
    @pytest.mark.parametrize("value", [36, 0.5, 72.0])
    def test_positive_numbers_are_used(self, value):
        assert resolve_backfill_stale_hours(value) == float(value)

    @pytest.mark.parametrize("value", [0, -5, "36", None, True])
    def test_anything_else_falls_back_to_the_default_with_a_warning(self, value, caplog):
        assert resolve_backfill_stale_hours(value) == 72.0
        assert "invalid backfill_stale_hours" in caplog.text


class TestLivePidHolder:
    """A read-only liveness check: it reports a running job and never touches the marker."""

    KEY = "some-job"

    def _write_marker(self, content: str) -> None:
        marker = pid_file_path(self.KEY)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(content)

    def test_no_marker(self):
        assert live_pid_holder(self.KEY) is None

    def test_live_holder_returns_its_pid(self, live_ccrecall_pid):
        self._write_marker(str(live_ccrecall_pid))
        assert live_pid_holder(self.KEY) == live_ccrecall_pid

    @pytest.mark.parametrize("content", ["99999999", "not-a-pid", "0", "-1", "9" * 40])
    def test_dead_or_corrupt_marker_is_not_live_and_is_left_in_place(self, content):
        self._write_marker(content)
        assert live_pid_holder(self.KEY) is None
        assert pid_file_path(self.KEY).read_text() == content

    def test_holder_we_cannot_signal_counts_as_live(self, monkeypatch):
        self._write_marker("12345")

        def deny(_pid, _signal):
            raise PermissionError

        monkeypatch.setattr("ccrecall.config.os.kill", deny)
        assert live_pid_holder(self.KEY) == 12345


class TestIsCcrecallProcess:
    def _ps_prints(self, monkeypatch, stdout: str, returncode: int = 0) -> None:
        def fake_run(*_args, **_kwargs):
            return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")

        monkeypatch.setattr("ccrecall.config.subprocess.run", fake_run)

    @pytest.mark.parametrize(
        "command",
        [
            "/home/u/.local/bin/ccrecall import --repair-gaps",
            "/usr/bin/python3 /home/u/.venv/bin/ccrecall backfill embeddings",
            "ccrecall-warm-model",
            "/opt/bin/ccrecall-sync",
        ],
    )
    def test_ccrecall_console_scripts_match(self, monkeypatch, command):
        self._ps_prints(monkeypatch, command + "\n")
        assert is_ccrecall_process(4242) is True

    @pytest.mark.parametrize(
        "command",
        ["tail -f /home/u/.ccrecall/ccrecall-import.log", "sleep 60", "vim ccrecall.py", "ccrecallx"],
    )
    def test_other_commands_do_not(self, monkeypatch, command):
        self._ps_prints(monkeypatch, command)
        assert is_ccrecall_process(4242) is False

    def test_unreadable_command_line_is_unknown(self, monkeypatch):
        self._ps_prints(monkeypatch, "", returncode=1)
        assert is_ccrecall_process(4242) is None

    def test_missing_ps_is_unknown(self, monkeypatch):
        def no_ps(*_args, **_kwargs):
            raise FileNotFoundError("ps")

        monkeypatch.setattr("ccrecall.config.subprocess.run", no_ps)
        assert is_ccrecall_process(4242) is None


class TestReusedPid:
    """A marker whose PID now belongs to something other than ccrecall reads as dead."""

    KEY = "some-job"

    @pytest.fixture
    def other_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            yield proc.pid
        finally:
            proc.kill()
            proc.wait()

    def _write_marker(self, content: str) -> None:
        marker = pid_file_path(self.KEY)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(content)

    def test_is_not_live(self, other_process):
        self._write_marker(str(other_process))
        assert live_pid_holder(self.KEY) is None

    def test_guard_reaps_it_and_acquires(self, other_process):
        self._write_marker(str(other_process))
        assert try_acquire_pid_file(self.KEY) is True

    def test_guard_still_refuses_a_live_ccrecall_holder(self, live_ccrecall_pid):
        self._write_marker(str(live_ccrecall_pid))
        assert try_acquire_pid_file(self.KEY) is False

    def test_holder_stays_live_when_ps_cannot_identify_it(self, other_process, monkeypatch):
        """A guard that can't identify its holder must not let a second instance start."""
        monkeypatch.setattr("ccrecall.config.is_ccrecall_process", lambda _pid: None)
        self._write_marker(str(other_process))
        assert live_pid_holder(self.KEY) == other_process
