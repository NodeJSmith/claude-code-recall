"""Tests for context_alerts — the SessionStart proactive alert builder.

Covers the tool-content backfill coverage predicate (has_backfillable_tool_content)
and its wiring into the proactive_alert_block.
"""

import sqlite3
from pathlib import Path

import pytest

from ccrecall.hooks.context_alerts import (
    _TOOL_CONTENT_SAMPLE_SIZE,
    has_backfillable_tool_content,
    proactive_alert_block,
)
from ccrecall.schema import SCHEMA

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _seed_session(
    conn: sqlite3.Connection,
    *,
    session_uuid: str,
    filepath: Path,
    tool_content: str | None = None,
) -> int:
    """Seed a minimal session with one message. Returns session_id."""
    conn.execute("INSERT INTO sessions (uuid) VALUES (?)", (session_uuid,))
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp, tool_content)"
        " VALUES (?, ?, 'user', 'hello', '2026-01-01T10:00:00Z', ?)",
        (session_id, f"msg-{session_uuid}", tool_content),
    )
    msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO branches (session_id, leaf_uuid, is_active, ended_at) VALUES (?, ?, 1, '2026-01-01T10:00:00Z')",
        (session_id, f"leaf-{session_uuid}"),
    )
    branch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
        (branch_id, msg_id),
    )

    conn.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, ?, 1)",
        (str(filepath), "deadbeef"),
    )
    conn.commit()
    return session_id


class TestHasBackfillableToolContent:
    def test_no_pending_sessions_returns_false(self, tmp_path):
        """All sessions already have tool_content → no alert."""
        conn = _make_conn()
        filepath = tmp_path / "sess-a.jsonl"
        filepath.touch()
        _seed_session(conn, session_uuid="sess-a", filepath=filepath, tool_content="[Bash: ls]")

        assert has_backfillable_tool_content(conn) is False

    def test_pending_with_existing_jsonl_returns_true(self, tmp_path):
        """A session with NULL tool_content whose JSONL exists → alert fires."""
        conn = _make_conn()
        filepath = tmp_path / "sess-b.jsonl"
        filepath.touch()
        _seed_session(conn, session_uuid="sess-b", filepath=filepath, tool_content=None)

        assert has_backfillable_tool_content(conn) is True

    def test_pending_with_missing_jsonl_returns_false(self, tmp_path):
        """A session with NULL tool_content whose JSONL is gone → no alert."""
        conn = _make_conn()
        filepath = tmp_path / "sess-c.jsonl"
        # Don't create the file — it's missing on disk
        _seed_session(conn, session_uuid="sess-c", filepath=filepath, tool_content=None)

        assert has_backfillable_tool_content(conn) is False

    def test_mixed_pending_some_exist_returns_true(self, tmp_path):
        """Multiple pending: some with missing JSONL, one with existing → alert fires."""
        conn = _make_conn()
        missing = tmp_path / "sess-gone.jsonl"
        _seed_session(conn, session_uuid="sess-gone", filepath=missing, tool_content=None)

        existing = tmp_path / "sess-here.jsonl"
        existing.touch()
        _seed_session(conn, session_uuid="sess-here", filepath=existing, tool_content=None)

        assert has_backfillable_tool_content(conn) is True

    def test_agent_prefixed_file_detected(self, tmp_path):
        """A session whose import_log entry is agent-{uuid}.jsonl is still found."""
        conn = _make_conn()
        filepath = tmp_path / "agent-sess-d.jsonl"
        filepath.touch()
        _seed_session(conn, session_uuid="sess-d", filepath=filepath, tool_content=None)

        assert has_backfillable_tool_content(conn) is True

    def test_empty_database_returns_false(self):
        """Fresh install with no sessions → no alert."""
        conn = _make_conn()
        assert has_backfillable_tool_content(conn) is False

    def test_sample_cap_misses_later_existing_jsonl(self, tmp_path):
        """Documents the intentional sampling-cap tradeoff, not a bug.

        has_backfillable_tool_content only samples the first
        _TOOL_CONTENT_SAMPLE_SIZE pending session uuids (see its docstring:
        "caps at _TOOL_CONTENT_SAMPLE_SIZE queries + stat calls"). When more
        than that many sessions are pending and only a session beyond the
        sample window has a surviving on-disk JSONL, the function returns
        False even though real backfillable work exists — the sample never
        reaches that session. This test pins that behavior so it isn't
        "fixed" by accident, and so anyone surprised by it in production can
        find the test that explains it.
        """
        conn = _make_conn()
        for i in range(_TOOL_CONTENT_SAMPLE_SIZE + 1):
            session_uuid = f"sess-{i}"
            filepath = tmp_path / f"{session_uuid}.jsonl"
            # Only the last-seeded session's JSONL exists on disk; the rest
            # (the first _TOOL_CONTENT_SAMPLE_SIZE, which is what gets
            # sampled) are missing.
            if i == _TOOL_CONTENT_SAMPLE_SIZE:
                filepath.touch()
            _seed_session(conn, session_uuid=session_uuid, filepath=filepath, tool_content=None)

        assert has_backfillable_tool_content(conn) is False


class TestToolContentAlertWiring:
    def test_alert_fires_in_proactive_block(self, tmp_path):
        """The tool-content alert appears in the proactive block when backfillable."""
        conn = _make_conn()
        filepath = tmp_path / "sess-e.jsonl"
        filepath.touch()
        _seed_session(conn, session_uuid="sess-e", filepath=filepath, tool_content=None)

        snooze = tmp_path / "snooze.json"
        marker = tmp_path / ".write-probe"
        status = tmp_path / "embedding-status.json"
        block = proactive_alert_block(
            {"alert_snooze_hours": 0},
            conn,
            db_available=True,
            _marker_path=marker,
            _snooze_path=snooze,
            _status_path=status,
        )
        assert "ccrecall backfill tool-content" in block

    def test_alert_suppressed_when_no_backfillable(self, tmp_path):
        """No pending sessions → no tool-content mention in the block."""
        conn = _make_conn()
        filepath = tmp_path / "sess-f.jsonl"
        filepath.touch()
        _seed_session(conn, session_uuid="sess-f", filepath=filepath, tool_content="done")

        snooze = tmp_path / "snooze.json"
        marker = tmp_path / ".write-probe"
        status = tmp_path / "embedding-status.json"
        block = proactive_alert_block(
            {"alert_snooze_hours": 0},
            conn,
            db_available=True,
            _marker_path=marker,
            _snooze_path=snooze,
            _status_path=status,
        )
        assert block == ""

    def test_snooze_suppresses_repeat_firing(self, tmp_path):
        """After firing once, the alert is snoozed and doesn't fire again."""
        conn = _make_conn()
        filepath = tmp_path / "sess-g.jsonl"
        filepath.touch()
        _seed_session(conn, session_uuid="sess-g", filepath=filepath, tool_content=None)

        snooze = tmp_path / "snooze.json"
        marker = tmp_path / ".write-probe"
        status = tmp_path / "embedding-status.json"
        settings = {"alert_snooze_hours": 24}

        block1 = proactive_alert_block(
            settings,
            conn,
            db_available=True,
            _marker_path=marker,
            _snooze_path=snooze,
            _status_path=status,
        )
        assert "ccrecall backfill tool-content" in block1

        block2 = proactive_alert_block(
            settings,
            conn,
            db_available=True,
            _marker_path=marker,
            _snooze_path=snooze,
            _status_path=status,
        )
        assert block2 == ""
