"""Tests for context_alerts — the SessionStart proactive alert builder.

Covers the tool-content backfill coverage predicate (has_backfillable_tool_content)
and its wiring into the proactive_alert_block.
"""

import sqlite3
from pathlib import Path

import pytest

from ccrecall.health import write_schedule_marker
from ccrecall.hooks.context_alerts import (
    _TOOL_CONTENT_SAMPLE_SIZE,
    has_backfillable_tool_content,
    has_draft_quality_chunks,
    proactive_alert_block,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


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


def _seed_branch_with_chunk(conn: sqlite3.Connection, *, session_uuid: str, cap_tokens: int | None) -> int:
    """Seed a minimal active branch with one chunk row. Returns branch_id."""
    conn.execute("INSERT INTO sessions (uuid) VALUES (?)", (session_uuid,))
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, ?, 1)",
        (session_id, f"leaf-{session_uuid}"),
    )
    branch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO chunks (branch_id, exchange_index, content_hash, cap_tokens) VALUES (?, 0, 'hash', ?)",
        (branch_id, cap_tokens),
    )
    conn.commit()
    return branch_id


class TestHasDraftQualityChunks:
    """cap_tokens-based draft-quality chunk detection (FR#7)."""

    def test_no_chunks_returns_false(self, memory_db):
        """Empty chunks table → no draft-quality chunks."""
        assert has_draft_quality_chunks(memory_db) is False

    def test_full_quality_chunk_returns_false(self, memory_db):
        """NULL cap_tokens (untruncated, full quality) → no draft-quality chunks."""
        _seed_branch_with_chunk(memory_db, session_uuid="sess-full", cap_tokens=None)
        assert has_draft_quality_chunks(memory_db) is False

    def test_draft_quality_chunk_returns_true(self, memory_db):
        """cap_tokens below FULL_QUALITY_TOKEN_LIMIT → draft-quality chunk found."""
        _seed_branch_with_chunk(memory_db, session_uuid="sess-draft", cap_tokens=4096)
        assert has_draft_quality_chunks(memory_db) is True


class TestDraftQualityAlertWiring:
    """ALERT_DRAFT_QUALITY_VECTORS wiring into proactive_alert_block (FR#7, FR#8, AC#5)."""

    def test_alert_fires_when_draft_chunks_present_and_no_marker(self, tmp_path, memory_db):
        """A draft-quality chunk with no schedule marker → alert fires (AC#5 step 1)."""
        conn = memory_db
        _seed_branch_with_chunk(conn, session_uuid="sess-i", cap_tokens=4096)

        block = proactive_alert_block(
            {"alert_snooze_hours": 0},
            conn,
            db_available=True,
            marker_path=tmp_path / ".write-probe",
            snooze_path=tmp_path / "snooze.json",
            status_path=tmp_path / "embedding-status.json",
            schedule_path=tmp_path / "backfill-schedule.json",
        )
        assert "draft-quality" in block.lower()

    def test_alert_suppressed_when_schedule_marker_configured(self, tmp_path, memory_db):
        """A configured_at marker suppresses the alert (AC#5 step 2)."""
        conn = memory_db
        _seed_branch_with_chunk(conn, session_uuid="sess-j", cap_tokens=4096)
        schedule = tmp_path / "backfill-schedule.json"
        write_schedule_marker("configured_at", path=schedule)

        block = proactive_alert_block(
            {"alert_snooze_hours": 0},
            conn,
            db_available=True,
            marker_path=tmp_path / ".write-probe",
            snooze_path=tmp_path / "snooze.json",
            status_path=tmp_path / "embedding-status.json",
            schedule_path=schedule,
        )
        assert "draft-quality" not in block.lower()

    def test_alert_suppressed_when_schedule_marker_dismissed(self, tmp_path, memory_db):
        """A dismissed_at marker also suppresses the alert (FR#17)."""
        conn = memory_db
        _seed_branch_with_chunk(conn, session_uuid="sess-k", cap_tokens=4096)
        schedule = tmp_path / "backfill-schedule.json"
        write_schedule_marker("dismissed_at", path=schedule)

        block = proactive_alert_block(
            {"alert_snooze_hours": 0},
            conn,
            db_available=True,
            marker_path=tmp_path / ".write-probe",
            snooze_path=tmp_path / "snooze.json",
            status_path=tmp_path / "embedding-status.json",
            schedule_path=schedule,
        )
        assert "draft-quality" not in block.lower()

    def test_alert_fires_when_marker_has_no_recognized_fields(self, tmp_path, memory_db):
        """An empty-dict marker (`{}`) doesn't count as configured/dismissed — alert fires."""
        conn = memory_db
        _seed_branch_with_chunk(conn, session_uuid="sess-l", cap_tokens=4096)
        schedule = tmp_path / "backfill-schedule.json"
        schedule.write_text("{}")

        block = proactive_alert_block(
            {"alert_snooze_hours": 0},
            conn,
            db_available=True,
            marker_path=tmp_path / ".write-probe",
            snooze_path=tmp_path / "snooze.json",
            status_path=tmp_path / "embedding-status.json",
            schedule_path=schedule,
        )
        assert "draft-quality" in block.lower()

    def test_ac5_marker_removed_fires_again(self, tmp_path, memory_db):
        """Remove the marker after a suppressed session → alert fires again (AC#5 step 3)."""
        conn = memory_db
        _seed_branch_with_chunk(conn, session_uuid="sess-m", cap_tokens=4096)
        schedule = tmp_path / "backfill-schedule.json"
        write_schedule_marker("configured_at", path=schedule)
        schedule.unlink()

        block = proactive_alert_block(
            {"alert_snooze_hours": 0},
            conn,
            db_available=True,
            marker_path=tmp_path / ".write-probe",
            snooze_path=tmp_path / "snooze.json",
            status_path=tmp_path / "embedding-status.json",
            schedule_path=schedule,
        )
        assert "draft-quality" in block.lower()

    def test_no_alert_when_no_draft_quality_chunks(self, tmp_path, memory_db):
        """No draft-quality chunks at all → block is empty."""
        conn = memory_db
        block = proactive_alert_block(
            {"alert_snooze_hours": 0},
            conn,
            db_available=True,
            marker_path=tmp_path / ".write-probe",
            snooze_path=tmp_path / "snooze.json",
            status_path=tmp_path / "embedding-status.json",
            schedule_path=tmp_path / "backfill-schedule.json",
        )
        assert block == ""


class TestHasBackfillableToolContent:
    def test_no_pending_sessions_returns_false(self, tmp_path, memory_db):
        """All sessions already have tool_content → no alert."""
        conn = memory_db
        filepath = tmp_path / "sess-a.jsonl"
        filepath.touch()
        _seed_session(conn, session_uuid="sess-a", filepath=filepath, tool_content="[Bash: ls]")

        assert has_backfillable_tool_content(conn) is False

    def test_pending_with_existing_jsonl_returns_true(self, tmp_path, memory_db):
        """A session with NULL tool_content whose JSONL exists → alert fires."""
        conn = memory_db
        filepath = tmp_path / "sess-b.jsonl"
        filepath.touch()
        _seed_session(conn, session_uuid="sess-b", filepath=filepath, tool_content=None)

        assert has_backfillable_tool_content(conn) is True

    def test_pending_with_missing_jsonl_returns_false(self, tmp_path, memory_db):
        """A session with NULL tool_content whose JSONL is gone → no alert."""
        conn = memory_db
        filepath = tmp_path / "sess-c.jsonl"
        # Don't create the file — it's missing on disk
        _seed_session(conn, session_uuid="sess-c", filepath=filepath, tool_content=None)

        assert has_backfillable_tool_content(conn) is False

    def test_mixed_pending_some_exist_returns_true(self, tmp_path, memory_db):
        """Multiple pending: some with missing JSONL, one with existing → alert fires."""
        conn = memory_db
        missing = tmp_path / "sess-gone.jsonl"
        _seed_session(conn, session_uuid="sess-gone", filepath=missing, tool_content=None)

        existing = tmp_path / "sess-here.jsonl"
        existing.touch()
        _seed_session(conn, session_uuid="sess-here", filepath=existing, tool_content=None)

        assert has_backfillable_tool_content(conn) is True

    def test_agent_prefixed_file_detected(self, tmp_path, memory_db):
        """A session whose import_log entry is agent-{uuid}.jsonl is still found."""
        conn = memory_db
        filepath = tmp_path / "agent-sess-d.jsonl"
        filepath.touch()
        _seed_session(conn, session_uuid="sess-d", filepath=filepath, tool_content=None)

        assert has_backfillable_tool_content(conn) is True

    def test_empty_database_returns_false(self, memory_db):
        """Fresh install with no sessions → no alert."""
        conn = memory_db
        assert has_backfillable_tool_content(conn) is False

    def test_sample_cap_misses_later_existing_jsonl(self, tmp_path, memory_db):
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
        conn = memory_db
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
    def test_alert_fires_in_proactive_block(self, tmp_path, memory_db):
        """The tool-content alert appears in the proactive block when backfillable."""
        conn = memory_db
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
            marker_path=marker,
            snooze_path=snooze,
            status_path=status,
        )
        assert "ccrecall backfill tool-content" in block

    def test_alert_suppressed_when_no_backfillable(self, tmp_path, memory_db):
        """No pending sessions → no tool-content mention in the block."""
        conn = memory_db
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
            marker_path=marker,
            snooze_path=snooze,
            status_path=status,
        )
        assert block == ""

    def test_snooze_suppresses_repeat_firing(self, tmp_path, memory_db):
        """After firing once, the alert is snoozed and doesn't fire again."""
        conn = memory_db
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
            marker_path=marker,
            snooze_path=snooze,
            status_path=status,
        )
        assert "ccrecall backfill tool-content" in block1

        block2 = proactive_alert_block(
            settings,
            conn,
            db_available=True,
            marker_path=marker,
            snooze_path=snooze,
            status_path=status,
        )
        assert block2 == ""
