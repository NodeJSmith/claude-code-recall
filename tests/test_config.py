"""Tests for ccrecall.config — logging formatter and default settings."""

import logging
import os

import pytest

from ccrecall.config import DEFAULT_SETTINGS, _ExtraFieldsFormatter, live_pid_holder, pid_file_path


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


class TestLivePidHolder:
    """A read-only liveness check: it reports a running job and never touches the marker."""

    KEY = "some-job"

    def _write_marker(self, content: str) -> None:
        marker = pid_file_path(self.KEY)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(content)

    def test_no_marker(self):
        assert live_pid_holder(self.KEY) is None

    def test_live_holder_returns_its_pid(self):
        self._write_marker(str(os.getppid()))
        assert live_pid_holder(self.KEY) == os.getppid()

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
