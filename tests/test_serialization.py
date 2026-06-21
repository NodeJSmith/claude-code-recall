"""Tests for the shared JSON-column decode helpers."""

import json

import pytest

from ccrecall.serialization import decode_json_column, decode_json_field


class TestDecodeJsonColumn:
    """Strict decode: raw column string or NULL only."""

    def test_none_returns_default(self):
        assert decode_json_column(None, []) == []
        assert decode_json_column(None, {}) == {}

    def test_empty_string_returns_default(self):
        assert decode_json_column("", []) == []

    def test_valid_list(self):
        assert decode_json_column('["a", "b"]', []) == ["a", "b"]

    def test_valid_dict(self):
        assert decode_json_column('{"Read": 2}', {}) == {"Read": 2}

    def test_default_identity_preserved(self):
        sentinel = ["unchanged"]
        assert decode_json_column(None, sentinel) is sentinel

    def test_malformed_raises(self):
        # Contract: columns only ever hold json.dumps output, so a corrupt value
        # raises rather than masking DB corruption — same as the inline code.
        with pytest.raises(json.JSONDecodeError):
            decode_json_column("{not json", [])


class TestDecodeJsonField:
    """Lenient decode: raw string, already-decoded value, or empty."""

    def test_none_returns_default(self):
        assert decode_json_field(None, []) == []

    def test_empty_string_returns_default(self):
        assert decode_json_field("", []) == []

    def test_raw_string_parsed(self):
        assert decode_json_field('["x"]', []) == ["x"]

    def test_already_decoded_passthrough(self):
        already = ["a", "b"]
        assert decode_json_field(already, []) is already

    def test_already_decoded_dict_passthrough(self):
        already = {"Read": 1}
        assert decode_json_field(already, {}) is already

    def test_malformed_string_returns_default(self):
        assert decode_json_field("{not json", []) == []
