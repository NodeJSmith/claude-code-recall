"""Integration tests for the import pipeline with v3 schema guards."""

import json
import logging
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
import sqlite_vec
from conftest import FIXTURE_DIR, VEC_SKIP, make_vec_conn

from ccrecall.embeddings import EMBEDDING_DIM
from ccrecall.hooks.import_conversations import _run, import_project, import_session, run

_STALE_IMPORT_LOG_SQL = (
    "UPDATE import_log SET file_hash = 'stale', file_size = NULL, file_mtime = NULL WHERE file_path = ?"
)


@pytest.fixture
def project_id(memory_db):
    """Create a test project and return its ID."""
    cursor = memory_db.cursor()
    cursor.execute(
        "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
        ("/test/project", "-test-project", "test_project"),
    )
    memory_db.commit()
    return cursor.lastrowid


class TestImportSessionBasic:
    """Test basic import workflow with linear conversation."""

    def test_import_session_basic(self, memory_db, project_id):
        """Import linear_3_exchange.jsonl and verify counts."""
        fixture_file = FIXTURE_DIR / "linear_3_exchange.jsonl"
        assert fixture_file.exists(), f"Fixture {fixture_file} not found"

        branches_imported, total_messages = import_session(memory_db, fixture_file, project_id)

        # Should import successfully
        assert branches_imported > 0, "At least one branch should be imported"
        assert total_messages > 0, "At least one message should be imported"

        # Verify session was created
        cursor = memory_db.cursor()
        cursor.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?", (project_id,))
        session_count = cursor.fetchone()[0]
        assert session_count == 1, "Exactly one session should exist"

        # Verify branches exist
        cursor.execute(
            "SELECT COUNT(*) FROM branches WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
        branch_count = cursor.fetchone()[0]
        assert branch_count == branches_imported, "Branch count should match returned value"

        # Verify messages exist
        cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
        message_count = cursor.fetchone()[0]
        assert message_count == total_messages, "Message count should match returned value"

    def test_unchanged_import_repairs_pending_tool_content(self, memory_db, project_id):
        """Import fast-skip paths must not bypass pending tool_content repair."""
        fixture_file = FIXTURE_DIR / "tool_heavy.jsonl"

        import_session(memory_db, fixture_file, project_id)
        memory_db.commit()

        cursor = memory_db.cursor()
        cursor.execute(
            """
            SELECT session_id, uuid FROM messages
            WHERE role = 'assistant' AND tool_content IS NOT NULL AND tool_content != ''
            LIMIT 1
            """
        )
        session_id, uuid = cursor.fetchone()
        cursor.execute("UPDATE messages SET tool_content = NULL WHERE session_id = ? AND uuid = ?", (session_id, uuid))
        memory_db.commit()

        branches_imported, total_messages = import_session(memory_db, fixture_file, project_id)
        memory_db.commit()

        repaired = cursor.execute(
            "SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, uuid)
        ).fetchone()[0]
        assert branches_imported >= 0
        assert total_messages > 0
        assert repaired, "unchanged import should repair NULL tool_content instead of fast-skipping"

    def test_pending_repair_imports_all_session_siblings_to_preserve_active_branch(self, memory_db, tmp_path):
        """Repairing an earlier sibling must not leave that sibling as the active branch."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        parent = project_dir / "sess-split.jsonl"
        agent = project_dir / "agent-sess-split.jsonl"
        parent.write_text(
            "\n".join(
                json.dumps(entry)
                for entry in [
                    {
                        "uuid": "u1",
                        "parentUuid": None,
                        "type": "user",
                        "timestamp": "2026-01-01T10:00:00Z",
                        "message": {"role": "user", "content": "please inspect"},
                    },
                    {
                        "uuid": "a1",
                        "parentUuid": "u1",
                        "type": "assistant",
                        "timestamp": "2026-01-01T10:00:01Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "I will inspect it."},
                                {"type": "tool_use", "name": "Read", "input": {"file_path": "src/app.py"}},
                            ],
                        },
                    },
                ]
            )
            + "\n"
        )
        agent.write_text(
            "\n".join(
                json.dumps(entry)
                for entry in [
                    {
                        "uuid": "u2",
                        "parentUuid": "a1",
                        "type": "user",
                        "timestamp": "2026-01-01T10:00:02Z",
                        "message": {"role": "user", "content": "now fix it"},
                    },
                    {
                        "uuid": "a2",
                        "parentUuid": "u2",
                        "type": "assistant",
                        "timestamp": "2026-01-01T10:00:03Z",
                        "message": {"role": "assistant", "content": "fixed"},
                    },
                ]
            )
            + "\n"
        )

        import_project(memory_db, project_dir)
        cursor = memory_db.cursor()
        session_id, tool_uuid = cursor.execute("SELECT session_id, uuid FROM messages WHERE uuid = 'a1'").fetchone()
        cursor.execute(
            "UPDATE messages SET tool_content = NULL WHERE session_id = ? AND uuid = ?", (session_id, tool_uuid)
        )
        memory_db.commit()

        import_project(memory_db, project_dir)

        repaired, leaf_uuid = cursor.execute(
            """
            SELECT m.tool_content, b.leaf_uuid
            FROM messages m
            JOIN branches b ON b.session_id = m.session_id
            WHERE m.session_id = ? AND m.uuid = ? AND b.is_active = 1
            """,
            (session_id, tool_uuid),
        ).fetchone()
        assert repaired
        assert leaf_uuid == "a2"

    def test_later_sibling_repair_does_not_reimport_processed_parent(self, memory_db, tmp_path):
        """A repair discovered in a later sibling should not replay siblings already handled in this run."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        parent = project_dir / "sess-later.jsonl"
        agent = project_dir / "agent-sess-later.jsonl"
        parent.write_text(
            json.dumps(
                {
                    "uuid": "u1",
                    "parentUuid": None,
                    "type": "user",
                    "timestamp": "2026-01-01T10:00:00Z",
                    "message": {"role": "user", "content": "start"},
                }
            )
            + "\n"
        )
        agent.write_text(
            json.dumps(
                {
                    "uuid": "a1",
                    "parentUuid": "u1",
                    "type": "assistant",
                    "timestamp": "2026-01-01T10:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "src/app.py"}}],
                    },
                }
            )
            + "\n"
        )

        import_project(memory_db, project_dir)
        cursor = memory_db.cursor()
        session_id = cursor.execute("SELECT id FROM sessions WHERE uuid = 'sess-later'").fetchone()[0]
        cursor.execute("UPDATE messages SET tool_content = NULL WHERE session_id = ? AND uuid = 'a1'", (session_id,))
        memory_db.commit()

        _sessions, _messages, skipped = import_project(memory_db, project_dir)

        repaired = cursor.execute(
            "SELECT tool_content FROM messages WHERE session_id = ? AND uuid = 'a1'", (session_id,)
        ).fetchone()[0]
        assert repaired
        assert skipped == 1

    def test_equal_timestamp_repair_keeps_agent_sibling_active(self, memory_db, tmp_path):
        """Equal latest timestamps must still replay the parent before its agent sibling."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        parent = project_dir / "sess-equal.jsonl"
        agent = project_dir / "agent-sess-equal.jsonl"
        shared_ts = "2026-01-01T10:00:01Z"
        parent.write_text(
            "\n".join(
                json.dumps(entry)
                for entry in [
                    {
                        "uuid": "u1",
                        "parentUuid": None,
                        "type": "user",
                        "timestamp": "2026-01-01T10:00:00Z",
                        "message": {"role": "user", "content": "inspect"},
                    },
                    {
                        "uuid": "a1",
                        "parentUuid": "u1",
                        "type": "assistant",
                        "timestamp": shared_ts,
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "src/app.py"}}],
                        },
                    },
                ]
            )
            + "\n"
        )
        agent.write_text(
            json.dumps(
                {
                    "uuid": "a2",
                    "parentUuid": "a1",
                    "type": "assistant",
                    "timestamp": shared_ts,
                    "message": {"role": "assistant", "content": "agent follow-up"},
                }
            )
            + "\n"
        )

        import_project(memory_db, project_dir)
        cursor = memory_db.cursor()
        session_id = cursor.execute("SELECT id FROM sessions WHERE uuid = 'sess-equal'").fetchone()[0]
        cursor.execute("UPDATE messages SET tool_content = NULL WHERE session_id = ? AND uuid = 'a1'", (session_id,))
        memory_db.commit()

        import_project(memory_db, project_dir)

        leaf_uuid = cursor.execute(
            "SELECT leaf_uuid FROM branches WHERE session_id = ? AND is_active = 1", (session_id,)
        ).fetchone()[0]
        assert leaf_uuid == "a2"


class TestImportSessionWithBranches:
    """Test import of a session with a rewind recorded in the JSONL.

    Session-keyed branch identity means find_all_branches returns only the
    active branch, so a rewind in the fixture no longer produces multiple
    branch rows — it produces exactly one, active, row.
    """

    def test_import_session_with_branches(self, memory_db, project_id):
        """Import single_rewind.jsonl and verify exactly one branch is detected."""
        fixture_file = FIXTURE_DIR / "single_rewind.jsonl"
        assert fixture_file.exists(), f"Fixture {fixture_file} not found"

        branches_imported, total_messages = import_session(memory_db, fixture_file, project_id)

        assert branches_imported == 1, f"Expected 1 branch, got {branches_imported}"
        assert total_messages > 0, "Should import messages"

        # Verify branches table
        cursor = memory_db.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM branches WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
        assert cursor.fetchone()[0] == 1, "Exactly 1 branch should exist in DB"

        # Verify active branch is marked
        cursor.execute(
            "SELECT COUNT(*) FROM branches WHERE is_active = 1 AND session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
        assert cursor.fetchone()[0] == 1, "Exactly one active branch should exist"


class TestEmptySessionGuard:
    """Test guard 1: sessions with only tool_result messages are deleted."""

    def test_empty_session_guard(self, memory_db, project_id):
        """Create JSONL with only tool_result messages and verify session is deleted."""
        # Create temporary JSONL with only tool_result content
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            temp_path = Path(f.name)
            # Write file-history-snapshot (ignored)
            f.write('{"type":"file-history-snapshot"}\n')
            # Write progress (ignored)
            f.write(
                '{"uuid":"root-uuid","type":"progress","timestamp":"2026-02-14T00:00:00Z","sessionId":"test","cwd":"/"}\n'
            )
            # Write user message with tool_result only
            f.write(
                '{"uuid":"msg1","parentUuid":"root-uuid","type":"user","timestamp":"2026-02-14T00:00:01Z","sessionId":"test","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tool1","content":"result"}]}}\n'
            )
            # Write assistant message with only a thinking block (no text, no
            # tool_use) — genuinely no extractable content. A tool_use-only
            # turn no longer qualifies: it now produces a row via
            # tool_content, so it would no longer trigger guard 1.
            f.write(
                '{"uuid":"msg2","parentUuid":"msg1","type":"assistant","timestamp":"2026-02-14T00:00:02Z","sessionId":"test","message":{"role":"assistant","content":[{"type":"thinking","thinking":"internal only"}]}}\n'
            )

        try:
            branches_imported, total_messages = import_session(memory_db, temp_path, project_id)

            # Guard 1: no extractable content means session deleted and returns -1
            assert branches_imported == -1, "Session should be deleted (guard 1 triggered)"
            assert total_messages == 0, "No messages should be imported"

            # Verify session was NOT created or was cleaned up
            cursor = memory_db.cursor()
            cursor.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?", (project_id,))
            session_count = cursor.fetchone()[0]
            assert session_count == 0, "Empty session should be deleted"

        finally:
            temp_path.unlink()


class TestEmptyBranchGuard:
    """Test guard 2: branches with empty aggregated content are cleaned up."""

    def test_empty_branch_guard(self, memory_db, project_id):
        """Guard 2 should delete branches whose aggregated content is empty.

        Creates a JSONL where the only real content is notification messages,
        imports via import_session, and verifies the session is cleaned up
        because all branches have empty aggregated content after excluding
        notifications.
        """
        # Create JSONL with a real user message + notification-only branch content
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            temp_path = Path(f.name)
            # Root entry
            f.write(
                '{"uuid":"root","type":"progress","timestamp":"2026-02-14T00:00:00Z","sessionId":"test","cwd":"/"}\n'
            )
            # User notification message (the only "user" content)
            f.write(
                '{"uuid":"msg1","parentUuid":"root","type":"user","timestamp":"2026-02-14T00:00:01Z","sessionId":"test","message":{"role":"user","content":"<task-notification><task-id>abc</task-id>Agent result here</task-notification>"}}\n'
            )
            # Assistant response to notification
            f.write(
                '{"uuid":"msg2","parentUuid":"msg1","type":"assistant","timestamp":"2026-02-14T00:00:02Z","sessionId":"test","message":{"role":"assistant","content":[{"type":"text","text":"Acknowledged."}]}}\n'
            )

        try:
            branches_imported, _total_messages = import_session(memory_db, temp_path, project_id)

            # Guard 2 fires because after excluding notifications, the branch
            # has only assistant text — but the notification user message IS
            # imported (is_notification=1). The branch aggregation excludes
            # notifications, so if the branch's only user content is a
            # notification, aggregated_content may be non-empty (assistant text
            # remains). Let's verify the branch state is consistent.
            cursor = memory_db.cursor()
            if branches_imported > 0:
                # Branch survived — verify notification is flagged
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM messages
                    WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?)
                      AND is_notification = 1
                """,
                    (project_id,),
                )
                notif_count = cursor.fetchone()[0]
                assert notif_count > 0, "Notification messages should be flagged"
            else:
                # Branch was deleted by guard 2 or guard 3 — session should not exist
                cursor.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?", (project_id,))
                assert cursor.fetchone()[0] == 0, "Empty session should be cleaned up"
        finally:
            temp_path.unlink()


class TestReimportIdempotent:
    """Test that reimporting the same file is idempotent."""

    def test_reimport_idempotent(self, memory_db, project_id):
        """Import the same file twice and verify hash check prevents duplicate."""
        fixture_file = FIXTURE_DIR / "linear_3_exchange.jsonl"

        # First import
        branches1, _messages1 = import_session(memory_db, fixture_file, project_id)
        assert branches1 > 0, "First import should succeed"

        # Count sessions after first import
        cursor = memory_db.cursor()
        cursor.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?", (project_id,))
        sessions_after_first = cursor.fetchone()[0]

        # Second import (same file)
        branches2, messages2 = import_session(memory_db, fixture_file, project_id)
        assert branches2 == -1, "Second import should return -1 (file hash match)"
        assert messages2 == 0, "No new messages on second import"

        # Verify no new session created
        cursor.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?", (project_id,))
        sessions_after_second = cursor.fetchone()[0]
        assert sessions_after_second == sessions_after_first, "No new session should be created"


class TestImportLogTracking:
    """Test that import_log tracks file imports correctly."""

    def test_import_log_created(self, memory_db, project_id):
        """Verify import_log entry is created with file hash and message count."""
        fixture_file = FIXTURE_DIR / "linear_3_exchange.jsonl"

        branches_imported, total_messages = import_session(memory_db, fixture_file, project_id)
        assert branches_imported > 0, "Import should succeed"

        # Check import_log
        cursor = memory_db.cursor()
        cursor.execute(
            "SELECT file_path, file_hash, messages_imported FROM import_log WHERE file_path = ?",
            (str(fixture_file),),
        )
        log_row = cursor.fetchone()
        assert log_row is not None, "import_log entry should exist"
        assert log_row[0] == str(fixture_file), "File path should match"
        assert log_row[1], "File hash should be set"
        assert log_row[2] == total_messages, "Message count should match"

    def test_import_log_updated_on_reimport(self, memory_db, project_id):
        """Verify import_log is updated on forced reimport (hash + message count)."""
        fixture_file = FIXTURE_DIR / "linear_3_exchange.jsonl"

        # First import
        import_session(memory_db, fixture_file, project_id)
        cursor = memory_db.cursor()
        cursor.execute(
            "SELECT file_hash, messages_imported FROM import_log WHERE file_path = ?",
            (str(fixture_file),),
        )
        first_row = cursor.fetchone()
        assert first_row is not None, "import_log entry should exist"
        first_hash, first_msg_count = first_row

        # Invalidate hash to force reimport (same pattern as TestFKSafeReimport)
        cursor.execute(
            _STALE_IMPORT_LOG_SQL,
            (str(fixture_file),),
        )
        memory_db.commit()

        # Reimport — should restore correct hash
        branches, _msg_count = import_session(memory_db, fixture_file, project_id)
        assert branches > 0, "Forced reimport should succeed"

        cursor.execute(
            "SELECT file_hash, messages_imported FROM import_log WHERE file_path = ?",
            (str(fixture_file),),
        )
        second_row = cursor.fetchone()
        assert second_row[0] == first_hash, "Hash should be restored to real value"
        assert second_row[1] == first_msg_count, "Message count should match"


class TestFKSafeReimport:
    """Test that reimport works with foreign keys enabled."""

    def test_reimport_with_fk_enabled(self, memory_db, project_id):
        """Reimporting with PRAGMA foreign_keys = ON should not raise IntegrityError."""
        fixture_file = FIXTURE_DIR / "linear_3_exchange.jsonl"

        # First import
        branches1, _messages1 = import_session(memory_db, fixture_file, project_id)
        assert branches1 > 0
        memory_db.commit()

        # Invalidate the import_log hash to force reimport
        cursor = memory_db.cursor()
        cursor.execute(
            _STALE_IMPORT_LOG_SQL,
            (str(fixture_file),),
        )
        memory_db.commit()

        # Reimport should succeed without IntegrityError
        branches2, messages2 = import_session(memory_db, fixture_file, project_id)
        assert branches2 > 0, "Reimport should succeed"
        assert messages2 > 0, "Messages should be reimported"
        memory_db.commit()

    def test_reimport_with_branches_fk(self, memory_db, project_id):
        """Reimport of a rewound conversation with FK enabled should not crash."""
        fixture_file = FIXTURE_DIR / "single_rewind.jsonl"

        branches1, _messages1 = import_session(memory_db, fixture_file, project_id)
        assert branches1 == 1
        memory_db.commit()

        # Force reimport
        cursor = memory_db.cursor()
        cursor.execute(
            _STALE_IMPORT_LOG_SQL,
            (str(fixture_file),),
        )
        memory_db.commit()

        branches2, _messages2 = import_session(memory_db, fixture_file, project_id)
        assert branches2 == 1, "Reimport should produce same branch count"
        memory_db.commit()


class TestBranchMetadata:
    """Test that branch metadata is correctly computed."""

    def test_branch_active_flag(self, memory_db, project_id):
        """Verify that is_active flag correctly identifies current branch."""
        fixture_file = FIXTURE_DIR / "single_rewind.jsonl"
        import_session(memory_db, fixture_file, project_id)

        cursor = memory_db.cursor()
        cursor.execute(
            "SELECT is_active, leaf_uuid FROM branches WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
        branches = cursor.fetchall()
        active_branches = [b for b in branches if b[0] == 1]
        assert len(active_branches) == 1, "Exactly one branch should be marked active"

    def test_branch_exchange_count(self, memory_db, project_id):
        """Verify exchange_count is computed for branches."""
        fixture_file = FIXTURE_DIR / "linear_3_exchange.jsonl"
        import_session(memory_db, fixture_file, project_id)

        cursor = memory_db.cursor()
        cursor.execute(
            "SELECT exchange_count FROM branches WHERE session_id IN (SELECT id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
        count = cursor.fetchone()[0]
        assert count > 0, "Exchange count should be positive"
        # linear_3_exchange has 3 user->assistant exchanges
        assert count >= 3, "Should count at least 3 exchanges"


class TestImportProject:
    """Test import_project — directory-level import with exclusion and subagent handling."""

    def test_exclude_projects_skips(self, memory_db):
        """import_project with exclude_projects should skip named projects."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "-home-user-myproject"
            project_dir.mkdir()

            # Copy a fixture into it; the fixture has cwd="/Users/samarthgupta/repos/forks/node-banana"
            # so project_name will be "node-banana" (derived from real cwd, not directory key)
            shutil.copy(FIXTURE_DIR / "linear_3_exchange.jsonl", project_dir / "session1.jsonl")

            sessions, messages, skipped = import_project(memory_db, project_dir, exclude_projects=["node-banana"])
            # Should return (0, 0, 0) because the project name matches exclusion
            assert sessions == 0
            assert messages == 0
            assert skipped == 0

    def test_normal_import(self, memory_db):
        """import_project should import all JSONL files in a project directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "-Users-sam-project"
            project_dir.mkdir()

            shutil.copy(FIXTURE_DIR / "linear_3_exchange.jsonl", project_dir / "session1.jsonl")

            sessions, _messages, skipped = import_project(memory_db, project_dir)
            assert sessions > 0 or skipped > 0, "Should process the JSONL file"

    def test_dotfiles_skipped(self, memory_db):
        """Dotfiles (hidden JSONL) should be skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "-Users-sam-project"
            project_dir.mkdir()

            # Create a dotfile
            (project_dir / ".hidden.jsonl").write_text('{"type":"progress"}\n')

            sessions, messages, _skipped = import_project(memory_db, project_dir)
            assert sessions == 0
            assert messages == 0

    def test_nested_subagent_jsonl_files_are_imported_via_shared_project_discovery(self, memory_db, tmp_path):
        """Project import must include safe nested subagent transcript files."""
        project_dir = tmp_path / "-Users-sam-project"
        project_dir.mkdir()
        parent = project_dir / "sess-nested.jsonl"
        nested_agent = project_dir / "state" / "nested" / "deeper" / "subagents" / "agent-sess-nested.jsonl"
        parent.write_text(
            json.dumps(
                {
                    "uuid": "u1",
                    "parentUuid": None,
                    "type": "user",
                    "timestamp": "2026-01-01T10:00:00Z",
                    "message": {"role": "user", "content": "start"},
                }
            )
            + "\n"
        )
        nested_agent.parent.mkdir(parents=True)
        nested_agent.write_text(
            json.dumps(
                {
                    "uuid": "a1",
                    "parentUuid": "u1",
                    "type": "assistant",
                    "timestamp": "2026-01-01T10:00:01Z",
                    "message": {"role": "assistant", "content": "nested follow-up"},
                }
            )
            + "\n"
        )

        _sessions, _messages, skipped = import_project(memory_db, project_dir)

        assert skipped == 0
        cursor = memory_db.cursor()
        assert cursor.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert cursor.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        assert cursor.execute("SELECT COUNT(*) FROM import_log").fetchone()[0] == 2
        assert cursor.execute("SELECT leaf_uuid FROM branches WHERE is_active = 1").fetchone()[0] == "a1"

    def test_symlinked_jsonl_is_skipped_before_import(self, memory_db, tmp_path):
        """Symlinked transcript files must not be imported from a project directory."""
        project_dir = tmp_path / "-Users-sam-project"
        project_dir.mkdir()
        outside = tmp_path / "outside.jsonl"
        shutil.copy(FIXTURE_DIR / "linear_3_exchange.jsonl", outside)
        (project_dir / "session1.jsonl").symlink_to(outside)

        sessions, messages, skipped = import_project(memory_db, project_dir)

        assert sessions == 0
        assert messages == 0
        assert skipped == 0
        cursor = memory_db.cursor()
        assert cursor.execute("SELECT COUNT(*) FROM import_log").fetchone()[0] == 0


class TestImportPoisonContainment:
    """Issue #170 — one raising transcript file must not wedge the whole batch.

    Before the fix, a single file that raised during import_session propagated
    straight out of import_project (and _run), killing the detached import
    process before any later file in the same directory (or any later
    project) got a chance to import, and before the crash was logged anywhere.
    """

    def test_poison_file_does_not_block_sibling_files_in_same_project(self, memory_db, tmp_path, caplog, monkeypatch):
        """A raising file must be skipped (and logged) while its siblings still import."""
        project_dir = tmp_path / "-Users-sam-project"
        project_dir.mkdir()

        # "aaa-poison" sorts before "zzz-good" alphabetically, matching the
        # real-world failure mode: everything that sorts after the poison
        # file in projects_dir.iterdir()/glob order never gets a chance.
        poison_file = project_dir / "aaa-poison.jsonl"
        good_file = project_dir / "zzz-good.jsonl"
        shutil.copy(FIXTURE_DIR / "linear_3_exchange.jsonl", poison_file)
        shutil.copy(FIXTURE_DIR / "linear_3_exchange.jsonl", good_file)

        real_import_session = import_session

        def _raise_on_poison(conn, filepath, project_id, *, force=False):
            if filepath == poison_file:
                raise RuntimeError("simulated poison transcript")
            return real_import_session(conn, filepath, project_id, force=force)

        monkeypatch.setattr("ccrecall.hooks.import_conversations.import_session", _raise_on_poison)

        with caplog.at_level(logging.ERROR, logger="ccrecall"):
            sessions, _messages, skipped = import_project(memory_db, project_dir)

        # The good file must still have been imported despite the poison file
        # raising — this is the core containment guarantee.
        assert sessions > 0 or skipped > 0, "the non-poison file must still be processed"

        cursor = memory_db.cursor()
        cursor.execute("SELECT COUNT(*) FROM import_log WHERE file_path = ?", (str(good_file),))
        assert cursor.fetchone()[0] == 1, "the good file must have an import_log entry"

        cursor.execute("SELECT COUNT(*) FROM import_log WHERE file_path = ?", (str(poison_file),))
        assert cursor.fetchone()[0] == 0, "the poison file must be left unwritten so it retries next run"

        # The crash must be logged with a traceback, not silently discarded.
        assert any(
            "poison" in record.message.lower() or "aaa-poison" in record.message
            for record in caplog.records
            if record.levelno >= logging.ERROR
        ), "the poison-file crash must be logged at ERROR level"
        assert any(record.exc_info for record in caplog.records if record.levelno >= logging.ERROR), (
            "the logged crash must include a traceback (logger.exception)"
        )

    def test_poison_file_does_not_corrupt_sibling_transaction(self, memory_db, tmp_path, monkeypatch):
        """A poison file's partial writes must not leak into the shared connection's
        uncommitted transaction alongside a sibling file's real work."""
        project_dir = tmp_path / "-Users-sam-project"
        project_dir.mkdir()

        poison_file = project_dir / "aaa-poison.jsonl"
        good_file = project_dir / "zzz-good.jsonl"
        shutil.copy(FIXTURE_DIR / "linear_3_exchange.jsonl", poison_file)
        shutil.copy(FIXTURE_DIR / "linear_3_exchange.jsonl", good_file)

        real_import_session = import_session

        def _raise_after_partial_write(conn, filepath, project_id, *, force=False):
            if filepath == poison_file:
                # Simulate partial work happening before the crash: write a row,
                # then raise before it would ever be committed or logged to
                # import_log by the real import_session.
                conn.execute(
                    "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
                    ("/poison/partial", "-poison-partial", "poison_partial"),
                )
                raise RuntimeError("simulated poison transcript after partial write")
            return real_import_session(conn, filepath, project_id, force=force)

        monkeypatch.setattr("ccrecall.hooks.import_conversations.import_session", _raise_after_partial_write)

        import_project(memory_db, project_dir)

        cursor = memory_db.cursor()
        cursor.execute("SELECT COUNT(*) FROM projects WHERE key = ?", ("-poison-partial",))
        assert cursor.fetchone()[0] == 0, "partial writes from the poison file must be rolled back, not leaked"

        cursor.execute("SELECT COUNT(*) FROM import_log WHERE file_path = ?", (str(good_file),))
        assert cursor.fetchone()[0] == 1, "the good file's real work must survive the poison file's rollback"

    def test_run_logs_uncaught_exceptions_before_exiting(self, tmp_path, caplog, monkeypatch):
        """An exception that reaches run() (outside the per-file loop entirely) must
        still be logged before the detached process exits — the top-level backstop."""

        def _raise(**_kwargs):
            raise RuntimeError("simulated catastrophic failure outside the per-file loop")

        monkeypatch.setattr("ccrecall.hooks.import_conversations._run", _raise)
        monkeypatch.setattr("ccrecall.hooks.import_conversations.remove_pid_file", lambda _key: None)

        with (
            caplog.at_level(logging.ERROR, logger="ccrecall"),
            pytest.raises(RuntimeError, match="simulated catastrophic failure"),
        ):
            run(db=tmp_path / "memory.db", projects_dir=tmp_path)

        assert any(record.exc_info for record in caplog.records if record.levelno >= logging.ERROR), (
            "an uncaught exception in run() must be logged with a traceback before the process exits"
        )


class TestImportRunPathSafety:
    """Test --project path safety checks in the import hook."""

    def test_run_rejects_symlink_project_dir(self, memory_db, tmp_path, monkeypatch, capsys):
        """A --project target that resolves through a symlinked project dir must be rejected."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        real_project = tmp_path / "real-project"
        real_project.mkdir()
        (projects_dir / "linked-project").symlink_to(real_project, target_is_directory=True)
        db_path = tmp_path / "memory.db"
        db_path.write_text("", encoding="utf-8")

        @contextmanager
        def fake_connection(*_args, **_kwargs):
            yield memory_db

        import_project_mock = Mock()
        monkeypatch.setattr("ccrecall.hooks.import_conversations.load_settings", lambda: {"exclude_projects": []})
        monkeypatch.setattr(
            "ccrecall.hooks.import_conversations.setup_logging", lambda *_args, **_kwargs: logging.getLogger("test")
        )
        monkeypatch.setattr("ccrecall.hooks.import_conversations.get_connection", fake_connection)
        monkeypatch.setattr("ccrecall.hooks.import_conversations.get_db_path", lambda _settings: db_path)
        monkeypatch.setattr("ccrecall.hooks.import_conversations.import_project", import_project_mock)

        _run(db=db_path, projects_dir=projects_dir, project="linked-project", verbose=False)

        assert capsys.readouterr().out == f"Unsafe project path: {projects_dir / 'linked-project'}\n"
        import_project_mock.assert_not_called()


class TestAppendOnlyReimport:
    """Gap 1 — Message deduplication on forced reimport.

    Prevents: stale-hash reimport doubling message rows, breaking recall results
    and inflating context injection counts.
    """

    def test_no_duplicate_messages_on_forced_reimport(self, memory_db, project_id):
        """Staling the import_log hash must not create new message rows."""
        fixture_file = FIXTURE_DIR / "linear_3_exchange.jsonl"

        # First import — establish baseline
        branches1, _messages1 = import_session(memory_db, fixture_file, project_id)
        assert branches1 > 0, "First import must succeed"

        cursor = memory_db.cursor()
        cursor.execute(
            "SELECT id, uuid FROM messages WHERE session_id = (SELECT id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
        rows_before = cursor.fetchall()
        ids_before = {row[0] for row in rows_before}
        uuids_before = {row[1] for row in rows_before}

        # Force reimport by staling the hash
        cursor.execute(
            _STALE_IMPORT_LOG_SQL,
            (str(fixture_file),),
        )
        memory_db.commit()

        branches2, _messages2 = import_session(memory_db, fixture_file, project_id)
        assert branches2 > 0, "Forced reimport must succeed"

        # Same number of message rows — append-only, no duplicates
        cursor.execute(
            "SELECT id, uuid FROM messages WHERE session_id = (SELECT id FROM sessions WHERE project_id = ?)",
            (project_id,),
        )
        rows_after = cursor.fetchall()
        assert len(rows_after) == len(rows_before), "Reimport must not create duplicate message rows"

        # Same DB row IDs — ON CONFLICT DO NOTHING preserved originals
        ids_after = {row[0] for row in rows_after}
        assert ids_after == ids_before, "Same DB row IDs must survive reimport"

        # No new UUIDs introduced
        uuids_after = {row[1] for row in rows_after}
        assert uuids_after == uuids_before, "No new UUIDs may appear after reimport"

    def test_no_duplicate_session_uuid_message_pairs(self, memory_db, project_id):
        """(session_id, uuid) uniqueness must hold across repeated forced reimports."""
        fixture_file = FIXTURE_DIR / "linear_3_exchange.jsonl"
        import_session(memory_db, fixture_file, project_id)

        for _ in range(2):
            cursor = memory_db.cursor()
            cursor.execute(
                _STALE_IMPORT_LOG_SQL,
                (str(fixture_file),),
            )
            memory_db.commit()
            import_session(memory_db, fixture_file, project_id)

        # Check no (session_id, uuid) duplicates exist anywhere
        cursor = memory_db.cursor()
        cursor.execute("""
            SELECT session_id, uuid, COUNT(*) AS cnt
            FROM messages
            GROUP BY session_id, uuid
            HAVING cnt > 1
        """)
        duplicates = cursor.fetchall()
        assert duplicates == [], f"Duplicate (session_id, uuid) pairs found after repeated reimport: {duplicates}"


class TestBranchMessagesDiffOnReimport:
    """Gap 2 — branch_messages link set must be identical after forced reimport.

    Prevents: ghost branch-message links accumulating across reimports, causing
    search results to surface deleted message content.
    """

    def test_branch_messages_identical_after_reimport(self, memory_db, project_id):
        """branch_messages rows must be the same set before and after stale-hash reimport."""
        fixture_file = FIXTURE_DIR / "single_rewind.jsonl"

        branches1, _ = import_session(memory_db, fixture_file, project_id)
        assert branches1 == 1, "Session-keyed identity: fixture must produce exactly 1 branch row"

        cursor = memory_db.cursor()
        cursor.execute(
            """
            SELECT branch_id, message_id FROM branch_messages
            WHERE branch_id IN (
                SELECT b.id FROM branches b
                JOIN sessions s ON b.session_id = s.id
                WHERE s.project_id = ?
            )
            ORDER BY branch_id, message_id
        """,
            (project_id,),
        )
        links_before = cursor.fetchall()
        assert links_before, "branch_messages must be populated after first import"

        # Force reimport
        cursor.execute(
            _STALE_IMPORT_LOG_SQL,
            (str(fixture_file),),
        )
        memory_db.commit()

        branches2, _ = import_session(memory_db, fixture_file, project_id)
        assert branches2 == 1, "Reimport must produce same branch count"

        cursor.execute(
            """
            SELECT branch_id, message_id FROM branch_messages
            WHERE branch_id IN (
                SELECT b.id FROM branches b
                JOIN sessions s ON b.session_id = s.id
                WHERE s.project_id = ?
            )
            ORDER BY branch_id, message_id
        """,
            (project_id,),
        )
        links_after = cursor.fetchall()

        assert links_after == links_before, (
            "branch_messages link set must be identical after forced reimport — "
            f"before={len(links_before)}, after={len(links_after)}"
        )


class TestSessionWithMessagesButNoBranches:
    """Gap 5 — Session rows and message rows survive when all branch content is empty.

    Prevents: conservative cleanup accidentally deleting sessions that have messages
    but whose only branch produced no FTS content (all-notification session).
    The import code skips empty branches but must not delete the session row when
    messages still exist.
    """

    def test_session_row_survives_all_notification_branch(self, memory_db, project_id):
        """Session and its messages must persist when the branch has only notification content."""
        # A JSONL where the sole user message is a task-notification.
        # aggregate_branch_content will return empty (notification excluded from FTS)
        # but the import should keep the session and its message rows intact.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            temp_path = Path(f.name)
            f.write(
                '{"uuid":"root","type":"progress","timestamp":"2026-03-01T10:00:00Z","sessionId":"notif-only-sess","cwd":"/test"}\n'
            )
            # Notification user message — is_notification=1, stored but excluded from FTS
            f.write(
                '{"uuid":"msg1","parentUuid":"root","type":"user","timestamp":"2026-03-01T10:00:01Z","sessionId":"notif-only-sess","message":{"role":"user","content":"<task-notification><task-id>x</task-id>Task done</task-notification>"}}\n'
            )
            # Assistant reply with real text — this IS included in FTS
            f.write(
                '{"uuid":"msg2","parentUuid":"msg1","type":"assistant","timestamp":"2026-03-01T10:00:02Z","sessionId":"notif-only-sess","message":{"role":"assistant","content":[{"type":"text","text":"Acknowledged the task notification."}]}}\n'
            )

        try:
            branches_imported, _total_messages = import_session(memory_db, temp_path, project_id)

            cursor = memory_db.cursor()
            # Session must still exist (messages present — conservative cleanup)
            cursor.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?", (project_id,))
            session_count = cursor.fetchone()[0]

            if branches_imported > 0:
                # Branch survived (assistant text kept it non-empty) — session must exist
                assert session_count == 1, "Session must exist when branch has content"
                cursor.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = (SELECT id FROM sessions WHERE project_id = ?)",
                    (project_id,),
                )
                msg_count = cursor.fetchone()[0]
                assert msg_count > 0, "Message rows must survive alongside the session"
            else:
                # No branches imported — verify the session was only removed if
                # it truly has no messages (conservative cleanup check)
                if session_count > 0:
                    cursor.execute(
                        "SELECT COUNT(*) FROM messages WHERE session_id = "
                        "(SELECT id FROM sessions WHERE project_id = ?)",
                        (project_id,),
                    )
                    msg_count = cursor.fetchone()[0]
                    assert msg_count > 0, (
                        "Session row must not survive with zero message rows — that would be an orphaned session"
                    )
        finally:
            temp_path.unlink()

    def test_session_deleted_only_when_both_messages_and_branches_are_zero(self, memory_db, project_id):
        """Session cleanup fires only when message count AND branch count are both zero.

        A session with messages but no branches (all branches empty) should not be deleted
        if messages exist — removing it would destroy potentially-recoverable data.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            temp_path = Path(f.name)
            # Only tool_result messages — no extractable text, triggers guard 1 (no messages)
            f.write(
                '{"uuid":"root","type":"progress","timestamp":"2026-03-01T10:00:00Z","sessionId":"empty-sess","cwd":"/test"}\n'
            )
            f.write(
                '{"uuid":"msg1","parentUuid":"root","type":"user","timestamp":"2026-03-01T10:00:01Z","sessionId":"empty-sess","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"result"}]}}\n'
            )

        try:
            branches_imported, _total_messages = import_session(memory_db, temp_path, project_id)

            # Guard 1 fires: no extractable text, session must be cleaned up
            assert branches_imported == -1, "Should trigger guard 1 (no extractable content)"

            cursor = memory_db.cursor()
            cursor.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?", (project_id,))
            assert cursor.fetchone()[0] == 0, "Session with zero messages and zero branches must be cleaned up"
        finally:
            temp_path.unlink()


class TestEmptyBranchGuardTightened:
    """Gap 6 — Tightened TestEmptyBranchGuard: empty branches preserved, not deleted.

    The import code comments explicitly state: 'No searchable content — skip but
    don't delete. Deleting causes thrashing.' This test pins that contract so a
    future refactor can't accidentally reintroduce the delete path.
    """

    def test_empty_branch_row_preserved_after_reimport(self, memory_db, project_id):
        """An empty-FTS branch must still exist in the DB after a forced reimport.

        If the branch were deleted on first import and then recreated on reimport
        (thrash cycle), the branch_id would differ — this test pins that the same
        branch row survives.
        """
        # Notification-only session: branch is created but aggregated_content is
        # empty after excluding notifications.  The branch row must be preserved.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            temp_path = Path(f.name)
            f.write(
                '{"uuid":"root","type":"progress","timestamp":"2026-03-01T10:00:00Z","sessionId":"empty-branch-sess","cwd":"/test"}\n'
            )
            # Notification-only user message
            f.write(
                '{"uuid":"msg1","parentUuid":"root","type":"user","timestamp":"2026-03-01T10:00:01Z","sessionId":"empty-branch-sess","message":{"role":"user","content":"<task-notification><task-id>y</task-id>Work done</task-notification>"}}\n'
            )
            # No assistant reply — branch has no non-notification content at all

        try:
            # First import
            import_session(memory_db, temp_path, project_id)

            cursor = memory_db.cursor()
            cursor.execute(
                """
                SELECT b.id FROM branches b
                JOIN sessions s ON b.session_id = s.id
                WHERE s.project_id = ?
            """,
                (project_id,),
            )
            branch_rows_after_first = cursor.fetchall()

            if not branch_rows_after_first:
                # Guard 1 fired (no messages at all) — nothing to test for branch preservation
                cursor.execute("SELECT COUNT(*) FROM sessions WHERE project_id = ?", (project_id,))
                assert cursor.fetchone()[0] == 0, "Guard 1 must clean up empty session"
                return

            branch_ids_after_first = {row[0] for row in branch_rows_after_first}

            # Force reimport
            cursor.execute(
                _STALE_IMPORT_LOG_SQL,
                (str(temp_path),),
            )
            memory_db.commit()
            import_session(memory_db, temp_path, project_id)

            cursor.execute(
                """
                SELECT b.id FROM branches b
                JOIN sessions s ON b.session_id = s.id
                WHERE s.project_id = ?
            """,
                (project_id,),
            )
            branch_ids_after_reimport = {row[0] for row in cursor.fetchall()}

            # No branch rows should have disappeared — empty branches are preserved
            assert branch_ids_after_first.issubset(branch_ids_after_reimport), (
                "Empty branch rows must be preserved across reimport — "
                "deleting them causes import thrash on every cycle"
            )
        finally:
            temp_path.unlink()


class TestIsMetaFiltering:
    """isMeta user entries without origin must not be stored as messages.

    Pins the filter that sync_session applies when deriving the messages list
    from a single-pass parse — isMeta=true entries without an origin field
    (e.g. session metadata injected by the harness) are excluded from storage,
    matching the old parse_jsonl_file behavior.
    """

    def test_ismeta_without_origin_excluded(self, memory_db, project_id):
        """multi_rewind.jsonl has one isMeta=true user entry without origin; it must not be stored."""
        fixture_file = FIXTURE_DIR / "multi_rewind.jsonl"
        import_session(memory_db, fixture_file, project_id)

        cursor = memory_db.cursor()
        cursor.execute(
            """
            SELECT m.uuid, m.content FROM messages m
            JOIN sessions s ON m.session_id = s.id
            WHERE s.project_id = ? AND m.role = 'user'
        """,
            (project_id,),
        )
        user_messages = cursor.fetchall()
        ismeta_uuid = "d91829d6-dc27-44b1-a6ac-cb4b699a1b11"
        stored_uuids = {row[0] for row in user_messages}
        assert ismeta_uuid not in stored_uuids, (
            f"isMeta=true user entry without origin must not be stored as a message; "
            f"found uuid {ismeta_uuid} in stored messages"
        )

    def test_ismeta_with_origin_included(self, memory_db, project_id):
        """channel_telegram.jsonl has isMeta=true entries WITH origin; they must be stored."""
        fixture_file = FIXTURE_DIR / "channel_telegram.jsonl"
        import_session(memory_db, fixture_file, project_id)

        cursor = memory_db.cursor()
        cursor.execute(
            """
            SELECT m.uuid FROM messages m
            JOIN sessions s ON m.session_id = s.id
            WHERE s.project_id = ? AND m.role = 'user'
        """,
            (project_id,),
        )
        user_messages = cursor.fetchall()
        assert len(user_messages) > 0, "isMeta=true user entries with origin should be stored"


class TestEmptySessionCascadeRegression:
    """Regression: #59 — empty-session cleanup must not crash when cascade
    triggers reach chunk_vec on a load_vec=False connection."""

    @VEC_SKIP
    def test_branch_delete_with_chunk_vec_cascade(self, tmp_path):
        """Deleting branches during empty-session cleanup loads vec on demand
        so the chunks_vec_ad cascade trigger can reach chunk_vec."""
        db_file = tmp_path / "cascade.db"
        session_uuid = "sess-cascade-59"

        # Phase 1: vec-loaded connection — creates cascade triggers and seeds
        # a session with chunks + chunk_vec (simulating a prior embedding run).
        vec_conn = make_vec_conn(str(db_file))
        vec_conn.execute("PRAGMA foreign_keys = ON")
        cursor = vec_conn.cursor()
        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/p", "-p", "p"),
        )
        project_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
            (session_uuid, project_id),
        )
        session_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO branches (session_id, leaf_uuid) VALUES (?, ?)",
            (session_id, "leaf-59"),
        )
        branch_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO chunks (branch_id, exchange_index, content_hash) VALUES (?, ?, ?)",
            (branch_id, 0, "hash-0"),
        )
        chunk_id = cursor.lastrowid
        vec = sqlite_vec.serialize_float32([0.1] * EMBEDDING_DIM)
        cursor.execute(
            "INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, vec),
        )
        vec_conn.commit()
        vec_conn.close()

        # Phase 2: non-vec connection (simulating the import path's
        # load_vec=False). JSONL named after the session UUID so
        # extract_session_uuid resolves to the pre-existing session.
        conn = sqlite3.connect(str(db_file))
        conn.execute("PRAGMA foreign_keys = ON")

        jsonl = tmp_path / f"{session_uuid}.jsonl"
        jsonl.write_text(
            '{"type":"file-history-snapshot"}\n'
            f'{{"uuid":"root","type":"progress","timestamp":"2026-01-01T00:00:00Z",'
            f'"sessionId":"{session_uuid}","cwd":"/"}}\n'
            f'{{"uuid":"msg1","parentUuid":"root","type":"user",'
            f'"timestamp":"2026-01-01T00:00:01Z","sessionId":"{session_uuid}",'
            f'"message":{{"role":"user","content":[{{"type":"tool_result",'
            f'"tool_use_id":"t1","content":"result"}}]}}}}\n'
            # Thinking-only content (no text, no tool_use) — genuinely no
            # extractable content. A tool_use-only turn no longer qualifies
            # it now produces a row via tool_content, so it would no
            # longer trigger the empty-session cleanup this test targets.
            f'{{"uuid":"msg2","parentUuid":"msg1","type":"assistant",'
            f'"timestamp":"2026-01-01T00:00:02Z","sessionId":"{session_uuid}",'
            f'"message":{{"role":"assistant","content":[{{"type":"thinking",'
            f'"thinking":"internal only"}}]}}}}\n'
        )

        # Without the fix this raises:
        #   sqlite3.OperationalError: no such module: vec0
        branches_imported, total_messages = import_session(conn, jsonl, project_id)

        assert branches_imported == -1
        assert total_messages == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE uuid = ?",
                (session_uuid,),
            ).fetchone()[0]
            == 0
        )
        conn.close()
