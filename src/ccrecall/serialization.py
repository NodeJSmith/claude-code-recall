"""Helpers for decoding JSON stored in SQLite TEXT columns.

The branch/session rows store ``files_modified``, ``commits``, and ``tool_counts``
as JSON text. Read paths previously decoded these inline, in two slightly
different shapes — these helpers give each shape one home:

- ``decode_json_column`` when the value comes straight from a cursor row (raw str).
- ``decode_json_field`` when an intermediate layer may already have decoded it.
"""

import json
from typing import Any


def decode_json_column(raw: str | None, default: Any) -> Any:
    """Decode a raw JSON column value, returning ``default`` when NULL/empty.

    For read paths that hold the raw column string. Assumes well-formed JSON
    (the columns only ever store json.dumps output), so a corrupt value raises —
    same as the inline ``json.loads(raw) if raw else default`` it replaces.
    """
    return json.loads(raw) if raw else default


def decode_json_field(value: object, default: Any) -> Any:
    """Decode a JSON field that may be a raw string OR already decoded.

    Some callers hand back the raw column string; others (e.g. memory_context)
    pre-decode it before passing the row on. Returns ``default`` for None/empty,
    the value unchanged if already decoded, or the parsed JSON if it's a string;
    a malformed string falls back to ``default``.
    """
    # Falsy (None, "", empty list/dict) -> default, mirroring the old `value or default`.
    if not value:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
