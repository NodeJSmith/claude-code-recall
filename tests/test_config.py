"""Tests for ccrecall.config — logging formatter and default settings."""

import logging

import pytest

from ccrecall.config import DEFAULT_SETTINGS, _ExtraFieldsFormatter, resolve_backfill_stale_hours


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
