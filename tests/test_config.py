"""Tests for ccrecall.config — logging formatter."""

import logging

from ccrecall.config import _ExtraFieldsFormatter


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
