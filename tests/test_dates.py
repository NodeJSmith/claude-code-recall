"""Tests for --before/--after boundary validation (ccrecall.dates).

Correctness here matters more than usual: stored timestamps are compared
lexicographically as raw strings (see search_query.scope_filter_clause and
recent_chats.get_recent_sessions), so a value that parses "successfully" but
in a format that doesn't sort the same way as the stored strings produces a
filter that silently returns wrong results with no error at all -- exactly
the failure mode this module exists to close off.
"""

import pytest

from ccrecall.dates import parse_date_boundary, validate_date_boundaries


class TestParseDateBoundaryAccepts:
    def test_bare_date_returned_unchanged(self):
        assert parse_date_boundary("2026-08-01", "--before") == "2026-08-01"

    def test_full_instant_with_z_returned_unchanged(self):
        assert parse_date_boundary("2026-08-01T10:00:00Z", "--before") == "2026-08-01T10:00:00Z"

    def test_full_instant_with_milliseconds_returned_unchanged(self):
        assert parse_date_boundary("2026-08-01T10:00:00.123Z", "--before") == "2026-08-01T10:00:00.123Z"

    def test_offset_instant_normalized_to_utc(self):
        # +02:00 must be converted to its UTC equivalent, not passed through
        # verbatim -- see the docstring in dates.py for why passthrough would
        # silently corrupt the lexicographic comparison against stored UTC values.
        assert parse_date_boundary("2026-08-01T10:00:00+02:00", "--before") == "2026-08-01T08:00:00Z"

    def test_negative_offset_instant_normalized_to_utc(self):
        assert parse_date_boundary("2026-08-01T10:00:00-05:00", "--after") == "2026-08-01T15:00:00Z"

    def test_normalized_instant_sorts_correctly_against_stored_format(self):
        # The whole point of normalization: after conversion, string comparison
        # must agree with actual chronological order against a Z-format value
        # that represents the same real instant.
        normalized = parse_date_boundary("2026-08-01T10:00:00+02:00", "--before")
        same_instant_as_z = "2026-08-01T08:00:00Z"
        assert normalized == same_instant_as_z


class TestParseDateBoundaryRejects:
    @pytest.mark.parametrize(
        "value",
        [
            "2026-8-1",  # unpadded month/day
            "2026-08-01T10:00:00",  # missing timezone (naive)
            "08/01/2026",  # US slash format
            "not-a-date",
            "",
            "2026-13-01",  # invalid month
            "2026-08-32",  # invalid day
            "  2026-08-01",  # leading whitespace
        ],
    )
    def test_rejects_unparseable_value(self, value):
        with pytest.raises(ValueError, match="--before"):
            parse_date_boundary(value, "--before")

    def test_error_message_includes_flag_name(self):
        with pytest.raises(ValueError, match="--after"):
            parse_date_boundary("garbage", "--after")

    def test_error_message_includes_offending_value(self):
        with pytest.raises(ValueError, match="2026-8-1"):
            parse_date_boundary("2026-8-1", "--before")


class TestValidateDateBoundaries:
    def test_both_none_returns_none_none(self):
        assert validate_date_boundaries(None, None) == (None, None)

    def test_valid_pair_returned_normalized(self):
        before, after = validate_date_boundaries("2026-08-10", "2026-08-01")
        assert (before, after) == ("2026-08-10", "2026-08-01")

    def test_invalid_before_raises(self):
        with pytest.raises(ValueError, match="--before"):
            validate_date_boundaries("2026-8-1", "2026-08-01")

    def test_invalid_after_raises(self):
        with pytest.raises(ValueError, match="--after"):
            validate_date_boundaries("2026-08-10", "not-a-date")

    def test_only_before_provided(self):
        assert validate_date_boundaries("2026-08-10", None) == ("2026-08-10", None)

    def test_only_after_provided(self):
        assert validate_date_boundaries(None, "2026-08-01") == (None, "2026-08-01")
