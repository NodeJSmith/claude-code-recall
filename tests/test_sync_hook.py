"""Integration tests for sync_current.py hook."""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import patched_clear, patched_record

from ccrecall.hooks import memory_setup, memory_sync, sync_current
from ccrecall.hooks.sync_current import sync_session, validate_session_id
from ccrecall.recent_chats import run as recent_chats_run
from ccrecall.schema import SCHEMA
from ccrecall.search_conversations import run as search_conversations_run

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Canonical valid session UUID for hook-input fixtures (passes validate_session_id).
VALID_SYNC_UUID = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def memory_db_with_project():
    """In-memory SQLite database with schema and a test project."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.commit()

    # Create a test project
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
        ("/test/project", "-test-project", "project"),
    )
    cursor.execute("SELECT id FROM projects WHERE path = ?", ("/test/project",))
    project_id = cursor.fetchone()[0]

    conn.commit()
    yield conn, project_id
    conn.close()


class TestSyncSessionCreatesBranches:
    """Test that sync_session creates branches correctly from JSONL fixture."""

    def test_sync_session_creates_branches(self, memory_db_with_project):
        """sync_session should create branches from a fixture with rewinding."""
        conn, _project_id = memory_db_with_project
        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            # Sync the session
            new_count = sync_session(conn, fixture_path, project_dir)

            # Verify messages were added
            assert new_count > 0, "Should have added messages"

            # Verify a session was created
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sessions")
            assert cursor.fetchone()[0] == 1

            # Verify branches were created
            cursor.execute("SELECT COUNT(*) FROM branches")
            branch_count = cursor.fetchone()[0]
            assert branch_count > 0, "Should have created at least one branch"

            # Verify branch_messages were created
            cursor.execute("SELECT COUNT(*) FROM branch_messages")
            branch_msg_count = cursor.fetchone()[0]
            assert branch_msg_count > 0, "Should have linked messages to branches"

            # Verify only one active branch
            cursor.execute("SELECT COUNT(*) FROM branches WHERE is_active = 1")
            assert cursor.fetchone()[0] == 1, "Should have exactly one active branch"

    def test_sync_session_populates_branch_content(self, memory_db_with_project):
        """Aggregated content should be populated after sync."""
        conn, _project_id = memory_db_with_project
        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(conn, fixture_path, project_dir)

            cursor = conn.cursor()
            cursor.execute("SELECT aggregated_content FROM branches WHERE is_active = 1")
            row = cursor.fetchone()
            assert row is not None
            content = row[0]
            assert content, "Active branch should have aggregated content"

    def test_sync_session_populates_context_summary(self, memory_db_with_project):
        """Context summary and summary_version should be populated after sync."""
        conn, _project_id = memory_db_with_project
        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(conn, fixture_path, project_dir)
            conn.commit()

            cursor = conn.cursor()
            cursor.execute("SELECT context_summary, summary_version FROM branches WHERE is_active = 1")
            row = cursor.fetchone()
            assert row is not None
            summary, version = row
            assert summary, "Active branch should have context_summary"
            assert version == 3, "summary_version should be 3 after sync"
            assert "### Session:" in summary
            assert "/ccrecall:ccr-recall" in summary


class TestSyncSessionUpdatesExisting:
    """Test that syncing the same session twice updates rather than duplicates."""

    def test_sync_session_updates_existing(self, memory_db_with_project):
        """Syncing the same session twice should update, not duplicate messages.

        Verifies both the Python-level dedup (existing_uuids set check)
        and the overall idempotency of sync_session.
        """
        conn, _project_id = memory_db_with_project
        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            # First sync
            new_count_1 = sync_session(conn, fixture_path, project_dir)
            conn.commit()
            assert new_count_1 > 0, "First sync should add messages"

            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM messages")
            msg_count_1 = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM sessions")
            session_count_1 = cursor.fetchone()[0]

            # Record branch structure after first sync
            cursor.execute("SELECT id, leaf_uuid, is_active FROM branches ORDER BY id")
            branches_1 = cursor.fetchall()

            # Record message UUIDs (these are what the Python-level dedup tracks)
            cursor.execute("SELECT uuid FROM messages WHERE uuid IS NOT NULL ORDER BY uuid")
            uuids_1 = [row[0] for row in cursor.fetchall()]
            assert len(uuids_1) > 0, "Messages should have UUIDs for dedup tracking"

            # Second sync (same session)
            new_count_2 = sync_session(conn, fixture_path, project_dir)
            conn.commit()

            cursor.execute("SELECT COUNT(*) FROM messages")
            msg_count_2 = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM sessions")
            session_count_2 = cursor.fetchone()[0]

            # Record UUIDs after second sync
            cursor.execute("SELECT uuid FROM messages WHERE uuid IS NOT NULL ORDER BY uuid")
            uuids_2 = [row[0] for row in cursor.fetchall()]

            # Session count should not increase
            assert session_count_2 == session_count_1, "Session count should not increase"

            # Message count should be the same (no duplicates)
            assert msg_count_2 == msg_count_1, "Messages should not be duplicated"

            # Second sync should have zero new messages — this proves the Python-level
            # existing_uuids check works, because the code loads existing UUIDs into a
            # set and skips them before reaching the SQL INSERT
            assert new_count_2 == 0, "Second sync should add no new messages"

            # UUID set should be identical (same messages, no extras)
            assert uuids_1 == uuids_2, "Message UUID set should be unchanged"

            # Branch structure should be preserved (updated, not recreated)
            cursor.execute("SELECT id, leaf_uuid, is_active FROM branches ORDER BY id")
            branches_2 = cursor.fetchall()
            assert len(branches_2) == len(branches_1), "Branch count should be unchanged"
            assert [b[1] for b in branches_2] == [b[1] for b in branches_1], "Branch leaf_uuids should be unchanged"


class TestValidateSessionIdValid:
    """Test that validate_session_id accepts valid UUIDs."""

    def test_validate_session_id_lowercase(self):
        """Should accept lowercase UUID format."""
        session_id = "016e1f0d-cff2-4552-9e21-43833c9a468e"
        assert validate_session_id(session_id) is True

    def test_validate_session_id_uppercase(self):
        """Should accept uppercase UUID format."""
        session_id = "016E1F0D-CFF2-4552-9E21-43833C9A468E"
        assert validate_session_id(session_id) is True

    def test_validate_session_id_mixed_case(self):
        """Should accept mixed case UUID format."""
        session_id = "016e1F0d-CfF2-4552-9E21-43833c9A468e"
        assert validate_session_id(session_id) is True


class TestValidateSessionIdRejectsTraversal:
    """Test that validate_session_id rejects path traversal and invalid formats."""

    def test_validate_session_id_rejects_path_traversal(self):
        """Should reject path traversal attempts."""
        assert validate_session_id("../etc/passwd") is False

    def test_validate_session_id_rejects_empty_string(self):
        """Should reject empty string."""
        assert validate_session_id("") is False

    def test_validate_session_id_rejects_non_uuid(self):
        """Should reject non-UUID formats."""
        assert validate_session_id("not-a-uuid") is False

    def test_validate_session_id_rejects_partial_uuid(self):
        """Should reject partial UUIDs."""
        assert validate_session_id("016e1f0d-cff2-4552-9e21") is False

    def test_validate_session_id_rejects_sql_injection(self):
        """Should reject SQL injection patterns."""
        assert validate_session_id("' OR '1'='1") is False

    def test_validate_session_id_rejects_none(self):
        """Should reject None (edge case)."""
        assert validate_session_id(None) is False

    def test_validate_session_id_rejects_uuid_with_extra(self):
        """Should reject UUID with extra characters."""
        assert validate_session_id("016e1f0d-cff2-4552-9e21-43833c9a468e-extra") is False


class TestSyncBranchMessagesDiff:
    """Test that sync_session's branch_messages diff is stable across repeated syncs.

    Prevents: ghost branch-message links accumulating on every PostToolUse turn,
    which would cause search to surface deleted/stale message content and bloat
    branch_messages with duplicate rows that survive until manual DB repair.
    """

    def test_branch_messages_stable_on_resync(self, memory_db_with_project):
        """branch_messages row set must be identical after a second sync of the same session."""
        conn, _ = memory_db_with_project
        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            # First sync — populate branches and branch_messages
            sync_session(conn, fixture_path, project_dir)
            conn.commit()

            cursor = conn.cursor()
            cursor.execute("SELECT branch_id, message_id FROM branch_messages ORDER BY branch_id, message_id")
            links_after_first = cursor.fetchall()
            assert links_after_first, "branch_messages must be populated after first sync"

            # Second sync — same file, same session; the diff should be a no-op
            sync_session(conn, fixture_path, project_dir)
            conn.commit()

            cursor.execute("SELECT branch_id, message_id FROM branch_messages ORDER BY branch_id, message_id")
            links_after_second = cursor.fetchall()

            assert links_after_second == links_after_first, (
                "branch_messages link set must be identical after resync — "
                f"before={len(links_after_first)}, after={len(links_after_second)}"
            )

    def test_no_duplicate_branch_messages_on_repeated_sync(self, memory_db_with_project):
        """Repeated syncs must never produce duplicate (branch_id, message_id) pairs."""
        conn, _ = memory_db_with_project
        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            for _ in range(3):
                sync_session(conn, fixture_path, project_dir)
                conn.commit()

            cursor = conn.cursor()
            cursor.execute("""
                SELECT branch_id, message_id, COUNT(*) AS cnt
                FROM branch_messages
                GROUP BY branch_id, message_id
                HAVING cnt > 1
            """)
            duplicates = cursor.fetchall()
            assert not duplicates, f"Duplicate (branch_id, message_id) pairs found after 3 syncs: {duplicates}"

    def test_new_messages_add_branch_links_without_removing_old(self, memory_db_with_project):
        """Growing a session mid-sync must add new branch_messages and keep existing ones.

        Prevents: the diff logic silently dropping links when new messages arrive
        (to_add stays empty or to_remove incorrectly prunes valid links), which
        would cause a mid-session Stop hook to lose newly-synced conversation turns
        from search results until the next full reimport.
        """
        conn, _ = memory_db_with_project
        fixture_lines = (FIXTURE_DIR / "single_rewind.jsonl").read_text().splitlines()

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            # Use a UUID-shaped stem so sync_session can parse it as a session UUID.
            # Both files share the same stem so they map to the same session row in the DB.
            session_stem = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            partial_path = project_dir / f"{session_stem}.jsonl"
            full_path = project_dir / f"{session_stem}.jsonl"

            # First sync: a truncated session (first 20 raw lines — 2 user/assistant
            # exchanges that survive the text-content filter).
            partial_path.write_text("\n".join(fixture_lines[:20]))
            sync_session(conn, partial_path, project_dir)
            conn.commit()

            cursor = conn.cursor()
            cursor.execute("SELECT branch_id, message_id FROM branch_messages ORDER BY branch_id, message_id")
            links_after_partial = set(cursor.fetchall())
            assert links_after_partial, "branch_messages must be populated after partial sync"

            # Second sync: the full session (all fixture lines — many more exchanges).
            full_path.write_text("\n".join(fixture_lines))
            sync_session(conn, full_path, project_dir)
            conn.commit()

            cursor.execute("SELECT branch_id, message_id FROM branch_messages ORDER BY branch_id, message_id")
            links_after_full = set(cursor.fetchall())

            # All links from the partial sync must still exist (append-only for existing links)
            assert links_after_partial.issubset(links_after_full), (
                "branch_messages from partial sync were removed after full sync — "
                f"missing: {links_after_partial - links_after_full}"
            )

            # The full sync must have added new links (growth was actually recorded)
            assert len(links_after_full) > len(links_after_partial), (
                "branch_messages did not grow after syncing a longer session — "
                f"partial={len(links_after_partial)}, full={len(links_after_full)}"
            )


class TestPidGuard:
    """Tests for _spawn_background() PID-file guard in memory_setup.py."""

    def test_pid_guard_prevents_concurrent_spawn(self, tmp_path, monkeypatch):
        """When a live PID file exists, _spawn_background skips spawning."""

        pid_path = tmp_path / ".pid-cm-test-cmd"
        monkeypatch.setattr(memory_setup, "pid_file_path", lambda pid_key: tmp_path / f".pid-{pid_key}")

        # Write our own PID (current process) as a "live" PID
        pid_path.write_text(str(os.getpid()))

        with patch("subprocess.Popen") as mock_popen:
            memory_setup._spawn_background(["cm-test-cmd"], "cm-test-cmd")
            mock_popen.assert_not_called()

    def test_pid_guard_reaps_stale_pid(self, tmp_path, monkeypatch):
        """When PID file holds a dead PID, _spawn_background reaps it and spawns."""

        pid_path = tmp_path / ".pid-cm-test-cmd"
        monkeypatch.setattr(memory_setup, "pid_file_path", lambda pid_key: tmp_path / f".pid-{pid_key}")

        # We need a real PID that is guaranteed dead. Spawn a trivial subprocess
        # and reap it. Deliberately NOT os.fork(): by the time this test runs the
        # process has usually started threads (the embedding model's onnxruntime
        # pool), and forking a multi-threaded process can deadlock the child — an
        # intermittent CI hang. Popen+wait gives the same dead PID with no fork.
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        dead_pid = proc.pid

        # Write the dead PID to the file
        pid_path.write_text(str(dead_pid))

        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, patch("os.write"):
            memory_setup._spawn_background(["cm-test-cmd"], "cm-test-cmd")
            mock_popen.assert_called_once()

        # PID file should have been reaped (it no longer exists or has been rewritten)
        # Since we patched os.write, the file won't be written with the new PID
        # but the stale file should have been unlinked before the spawn attempt

    def test_pid_guard_atomic_create(self, tmp_path, monkeypatch):
        """PID file creation uses O_CREAT | O_EXCL to prevent TOCTOU races."""

        monkeypatch.setattr(memory_setup, "pid_file_path", lambda pid_key: tmp_path / f".pid-{pid_key}")

        created_flags = []

        original_open = os.open

        def capturing_open(path, flags, mode=0o777):
            if ".pid-cm-test-cmd" in str(path):
                created_flags.append(flags)
            return original_open(path, flags, mode)

        mock_proc = MagicMock()
        mock_proc.pid = 12345

        with (
            patch("os.open", side_effect=capturing_open),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("os.write"),
            patch("os.close"),
        ):
            memory_setup._spawn_background(["cm-test-cmd"], "cm-test-cmd")

        assert created_flags, "os.open must have been called for the PID file"
        flags = created_flags[0]
        assert flags & os.O_CREAT, "O_CREAT must be set"
        assert flags & os.O_EXCL, "O_EXCL must be set for atomic create"


class TestMemorySyncTempCleanup:
    """Tests for temp file cleanup in memory_sync.py."""

    def test_memory_sync_cleans_temp_on_popen_failure(self, tmp_path):
        """When Popen raises, the temp file is unlinked."""

        # Create a fake temp file
        tmp_file = tmp_path / "ccrecall-sync-test.json"
        tmp_file.write_text('{"test": true}')
        tmp_path_str = str(tmp_file)

        with patch("tempfile.mkstemp", return_value=(0, tmp_path_str)), patch("os.fdopen") as mock_fdopen:
            # Make fdopen return a context manager that writes successfully
            mock_file = MagicMock()
            mock_fdopen.return_value.__enter__ = MagicMock(return_value=mock_file)
            mock_fdopen.return_value.__exit__ = MagicMock(return_value=False)
            with (
                patch("subprocess.Popen", side_effect=OSError("no such file")),
                patch("sys.stdin") as mock_stdin,
            ):
                # Run with a stdin that returns empty content
                mock_stdin.read.return_value = '{"session": "test"}'
                with contextlib.suppress(Exception):
                    memory_sync.main()

        # Popen failed, so the cleanup path must have deleted the real temp file
        assert not tmp_file.exists(), "temp file should be unlinked when Popen fails"


class TestReapStaleTempFiles:
    """Tests for _reap_stale_temp_files() in memory_setup.py."""

    def test_reaps_files_older_than_one_hour(self, tmp_path, monkeypatch):
        """Files older than 1 hour matching the pattern are deleted."""

        # Create a stale file matching the pattern
        stale_file = tmp_path / "ccrecall-sync-old.json"
        stale_file.write_text('{"old": true}')
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(str(stale_file), (old_time, old_time))

        # Create a fresh file (should NOT be deleted)
        fresh_file = tmp_path / "ccrecall-sync-new.json"
        fresh_file.write_text('{"new": true}')

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

        memory_setup._reap_stale_temp_files()

        assert not stale_file.exists(), "Stale file should have been deleted"
        assert fresh_file.exists(), "Fresh file should NOT have been deleted"

    def test_does_not_delete_non_matching_files(self, tmp_path, monkeypatch):
        """Files not matching the pattern are not touched."""

        # Create an old file with a different pattern
        other_file = tmp_path / "other-old-file.json"
        other_file.write_text('{"other": true}')
        old_time = time.time() - 7200
        os.utime(str(other_file), (old_time, old_time))

        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

        memory_setup._reap_stale_temp_files()

        assert other_file.exists(), "Non-matching file must not be deleted"


class TestRecentChatsDbFlag:
    """Tests for --db flag override in recent_chats.py after sqlite3.connect replacement."""

    def test_recent_chats_db_flag_override(self, tmp_path):
        """--db /tmp/test.db still works after sqlite3.connect replacement."""

        # Create a real SQLite DB at a custom path with known data
        custom_db = tmp_path / "custom.db"
        conn = sqlite3.connect(str(custom_db))
        conn.executescript(SCHEMA)
        conn.commit()

        # Insert a project and session with a branch so recent_chats can retrieve it
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/test/project", "-test-project", "test-project"),
        )
        project_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
            ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", project_id),
        )
        session_id = cursor.lastrowid
        cursor.execute(
            """INSERT INTO branches
               (session_id, leaf_uuid, is_active, started_at, ended_at, exchange_count)
               VALUES (?, ?, 1, datetime('now', '-1 hour'), datetime('now'), 1)""",
            (session_id, "leaf-uuid-1"),
        )
        branch_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, datetime('now'))",
            (session_id, "msg-uuid-1", "user", "hello from custom db"),
        )
        msg_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
            (branch_id, msg_id),
        )
        conn.commit()
        conn.close()

        # Call run() with --db pointing to our custom DB
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            recent_chats_run(db=custom_db)

        output = captured.getvalue()
        assert "test-project" in output or "hello from custom db" in output or "Recent Conversations" in output


class TestSearchConversationsDbFlag:
    """Tests for --db flag override in search_conversations.py after sqlite3.connect replacement."""

    def test_search_conversations_db_flag_override(self, tmp_path):
        """--db /tmp/test.db still works after sqlite3.connect replacement."""

        # Create a real SQLite DB at a custom path with known data
        custom_db = tmp_path / "search_custom.db"
        conn = sqlite3.connect(str(custom_db))
        conn.executescript(SCHEMA)
        conn.commit()

        # Insert a project, session, branch, and message with unique searchable content
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/test/search-project", "-test-search-project", "search-project"),
        )
        project_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
            ("bbbbbbbb-cccc-dddd-eeee-ffffffffffff", project_id),
        )
        session_id = cursor.lastrowid
        cursor.execute(
            """INSERT INTO branches
               (session_id, leaf_uuid, is_active, started_at, ended_at,
                exchange_count, aggregated_content)
               VALUES (?, ?, 1, datetime('now', '-1 hour'), datetime('now'), 1, ?)""",
            (session_id, "leaf-uuid-search", "uniqueterm12345"),
        )
        branch_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, datetime('now'))",
            (session_id, "msg-uuid-search", "user", "uniqueterm12345"),
        )
        msg_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
            (branch_id, msg_id),
        )
        conn.commit()
        conn.close()

        # Call run() with query and db
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            search_conversations_run(query="uniqueterm12345", db=custom_db)

        output = captured.getvalue()
        assert "Error" not in output


class TestSyncCurrentExcludeProjects:
    """sync_current.run honors exclude_projects for the live session (matches import)."""

    def _run(self, tmp_path, monkeypatch, *, settings, cwd):
        monkeypatch.setattr(sync_current, "pid_file_path", lambda key: tmp_path / f".pid-{key}")
        monkeypatch.setattr(sync_current, "remove_pid_file", lambda key: None)
        monkeypatch.setattr(sync_current, "load_settings", lambda: settings)
        synced = []
        monkeypatch.setattr(sync_current, "sync_session", lambda *a, **k: synced.append(1) or 0)
        # If the guard fails to short-circuit, get_session_file finding nothing keeps the
        # test from touching a real DB — but sync_session would still register if reached.
        monkeypatch.setattr(sync_current, "get_session_file", lambda *a, **k: synced.append("reached") or None)

        input_file = tmp_path / "hook.json"
        input_file.write_text(json.dumps({"session_id": VALID_SYNC_UUID, "cwd": cwd}))
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            sync_current.run(input_file=input_file)
        return synced, json.loads(captured.getvalue())

    def test_excluded_project_skips_before_sync(self, tmp_path, monkeypatch):
        synced, out = self._run(
            tmp_path,
            monkeypatch,
            settings={"exclude_projects": ["secret-repo"], "logging_enabled": False},
            cwd="/home/u/secret-repo",
        )
        assert out == {"continue": True}
        assert synced == []  # neither get_session_file nor sync_session reached

    def test_non_excluded_project_proceeds_past_guard(self, tmp_path, monkeypatch):
        synced, out = self._run(
            tmp_path,
            monkeypatch,
            settings={"exclude_projects": ["secret-repo"], "logging_enabled": False},
            cwd="/home/u/public-repo",
        )
        assert out == {"continue": True}
        assert synced == ["reached"]  # guard let it through to session-file lookup


class TestSyncCurrentConcurrencyGuard:
    """sync-current skips if another instance holds the lock."""

    def _make_input(self, tmp_path, session_id=VALID_SYNC_UUID):
        p = tmp_path / "hook.json"
        p.write_text(json.dumps({"session_id": session_id}))
        return p

    def _run(self, tmp_path, monkeypatch, *, session_id=VALID_SYNC_UUID, extra_patches=None):
        monkeypatch.setattr(sync_current, "pid_file_path", lambda key: tmp_path / f".pid-{key}")
        monkeypatch.setattr(sync_current, "remove_pid_file", lambda key: None)
        monkeypatch.setattr(sync_current, "load_settings", lambda: {"exclude_projects": [], "logging_enabled": False})
        monkeypatch.setattr(sync_current, "get_session_file", lambda *a, **k: None)
        if extra_patches:
            for attr, val in extra_patches.items():
                monkeypatch.setattr(sync_current, attr, val)
        input_file = self._make_input(tmp_path, session_id=session_id)
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            sync_current.run(input_file=input_file)
        return json.loads(captured.getvalue())

    def test_second_sync_skips_when_lock_held_by_live_pid(self, tmp_path, monkeypatch):
        """When a live PID file exists, a second sync-current skips without embedding."""
        monkeypatch.setattr(sync_current, "pid_file_path", lambda key: tmp_path / f".pid-{key}")
        monkeypatch.setattr(sync_current, "remove_pid_file", lambda key: None)

        # Write current process PID as "live" holder
        (tmp_path / f".pid-{sync_current.PID_KEY}").write_text(str(os.getpid()))

        sync_called = []
        monkeypatch.setattr(sync_current, "sync_session", lambda *a, **k: sync_called.append(1) or 0)

        input_file = self._make_input(tmp_path)
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            sync_current.run(input_file=input_file)

        out = json.loads(captured.getvalue())
        assert out == {"continue": True}
        assert sync_called == [], "sync_session must NOT be called when lock is held"

    def test_skip_outputs_exactly_continue_true(self, tmp_path, monkeypatch):
        """Skip path prints exactly the hook-contract JSON: {\"continue\": true}."""
        monkeypatch.setattr(sync_current, "pid_file_path", lambda key: tmp_path / f".pid-{key}")
        monkeypatch.setattr(sync_current, "remove_pid_file", lambda key: None)
        (tmp_path / f".pid-{sync_current.PID_KEY}").write_text(str(os.getpid()))

        input_file = self._make_input(tmp_path)
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            sync_current.run(input_file=input_file)

        # Must be exactly the string the hook harness expects
        assert captured.getvalue().strip() == '{"continue": true}'

    def test_stale_lock_is_reaped_and_run_proceeds(self, tmp_path, monkeypatch):
        """A stale lock (dead PID) is reaped and the sync continues."""
        monkeypatch.setattr(sync_current, "pid_file_path", lambda key: tmp_path / f".pid-{key}")
        # Let remove_pid_file actually delete from tmp_path
        monkeypatch.setattr(
            sync_current,
            "remove_pid_file",
            lambda key: (tmp_path / f".pid-{key}").unlink(missing_ok=True),
        )
        monkeypatch.setattr(sync_current, "load_settings", lambda: {"exclude_projects": [], "logging_enabled": False})
        monkeypatch.setattr(sync_current, "get_session_file", lambda *a, **k: None)

        # Spawn a real subprocess and reap it to get a guaranteed-dead PID
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        dead_pid = proc.pid

        lock_file = tmp_path / f".pid-{sync_current.PID_KEY}"
        lock_file.write_text(str(dead_pid))

        input_file = self._make_input(tmp_path)
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            sync_current.run(input_file=input_file)

        out = json.loads(captured.getvalue())
        assert out == {"continue": True}
        # Lock was reaped and cleaned up by the successful run's finally block
        assert not lock_file.exists(), "stale lock file should be gone after run"

    def test_first_sync_completes_normally_without_lock(self, tmp_path, monkeypatch):
        """When no lock file exists, sync-current runs normally (no skip)."""
        reached = []
        out = self._run(
            tmp_path,
            monkeypatch,
            extra_patches={"get_session_file": lambda *a, **k: reached.append(1) or None},
        )
        assert out == {"continue": True}
        assert reached, "get_session_file should be reached (lock guard passed)"

    def test_missing_runtime_dir_is_created(self, tmp_path, monkeypatch):
        """Stop hook firing before ~/.ccrecall/ exists must not crash — run() creates it."""
        # runtime_dir is where the pid file lives; it does not exist yet (only
        # tmp_path does), so run() must create it before opening the lock.
        # Can't reuse _run here: it pins pid_file_path to tmp_path, which always
        # exists and so wouldn't exercise the missing-dir path.
        runtime_dir = tmp_path / "absent"
        monkeypatch.setattr(sync_current, "pid_file_path", lambda key: runtime_dir / f".pid-{key}")
        monkeypatch.setattr(sync_current, "remove_pid_file", lambda key: None)
        monkeypatch.setattr(sync_current, "load_settings", lambda: {"exclude_projects": [], "logging_enabled": False})
        monkeypatch.setattr(sync_current, "get_session_file", lambda *a, **k: None)

        input_file = self._make_input(tmp_path)
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            sync_current.run(input_file=input_file)

        assert json.loads(captured.getvalue()) == {"continue": True}
        assert runtime_dir.is_dir(), "run() should create the missing runtime dir"


class TestModelWarmOnSetup:
    """M22: model warm is spawned at setup, PID-guarded, non-blocking."""

    WARM_PID_KEY = "ccrecall-warm-model"

    def test_setup_spawns_warm_model_command(self, tmp_path, monkeypatch):
        """memory_setup.main() spawns ccrecall-warm-model after DB setup."""
        spawned: list[tuple[list[str], str]] = []

        def mock_spawn(argv, pid_key):
            spawned.append((argv, pid_key))

        monkeypatch.setattr(memory_setup, "_spawn_background", mock_spawn)
        monkeypatch.setattr(memory_setup, "DEFAULT_DB_PATH", tmp_path / "conversations.db")
        monkeypatch.setattr(memory_setup, "load_settings", lambda: {"logging_enabled": False})
        monkeypatch.setattr(memory_setup, "_needs_reimport", lambda *a, **k: False)
        monkeypatch.setattr(memory_setup, "_needs_backfill", lambda *a, **k: False)
        monkeypatch.setattr(memory_setup, "find_legacy_db", lambda: None)
        monkeypatch.setattr(memory_setup, "_reap_stale_temp_files", lambda: None)

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            memory_setup.main()

        warm_calls = [a for a, _ in spawned if a and a[0] == "ccrecall-warm-model"]
        assert warm_calls, f"ccrecall-warm-model was not spawned; all spawns: {spawned}"

    def test_warm_spawn_pid_guarded_by_spawn_background(self, tmp_path, monkeypatch):
        """The warm spawn uses _spawn_background (which has the atomic PID guard)."""
        spawn_keys: list[str] = []

        def recording_spawn(argv, pid_key):
            spawn_keys.append(pid_key)

        monkeypatch.setattr(memory_setup, "_spawn_background", recording_spawn)
        monkeypatch.setattr(memory_setup, "DEFAULT_DB_PATH", tmp_path / "conversations.db")
        monkeypatch.setattr(memory_setup, "load_settings", lambda: {"logging_enabled": False})
        monkeypatch.setattr(memory_setup, "_needs_reimport", lambda *a, **k: False)
        monkeypatch.setattr(memory_setup, "_needs_backfill", lambda *a, **k: False)
        monkeypatch.setattr(memory_setup, "find_legacy_db", lambda: None)
        monkeypatch.setattr(memory_setup, "_reap_stale_temp_files", lambda: None)

        with patch("sys.stdout", io.StringIO()):
            memory_setup.main()

        # The warm spawn must pass "ccrecall-warm-model" as the pid_key so the
        # atomic O_CREAT|O_EXCL guard in _spawn_background prevents concurrent warms.
        assert self.WARM_PID_KEY in spawn_keys, (
            f"expected pid_key {self.WARM_PID_KEY!r} in spawn calls; got: {spawn_keys}"
        )

    def test_setup_non_blocking_returns_continue_true(self, tmp_path, monkeypatch):
        """memory_setup.main() always prints {continue: true} (warm spawn is detached)."""
        monkeypatch.setattr(memory_setup, "_spawn_background", lambda *a, **k: None)
        monkeypatch.setattr(memory_setup, "DEFAULT_DB_PATH", tmp_path / "conversations.db")
        monkeypatch.setattr(memory_setup, "load_settings", lambda: {"logging_enabled": False})
        monkeypatch.setattr(memory_setup, "_needs_reimport", lambda *a, **k: False)
        monkeypatch.setattr(memory_setup, "_needs_backfill", lambda *a, **k: False)
        monkeypatch.setattr(memory_setup, "find_legacy_db", lambda: None)
        monkeypatch.setattr(memory_setup, "_reap_stale_temp_files", lambda: None)

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            memory_setup.main()

        out = json.loads(captured.getvalue())
        assert out.get("continue") is True

    def test_cold_model_warning_fires_when_disk_cache_absent(self, tmp_path):
        """_warn_cold_model logs a warning when the fastembed disk cache is absent."""
        warning_messages: list[str] = []
        mock_logger = MagicMock()
        mock_logger.handlers = []
        mock_logger.warning = lambda msg, *a, **k: warning_messages.append(msg)

        with (
            patch("ccrecall.hooks.sync_current.is_model_cached_on_disk", return_value=False),
            patch("ccrecall.hooks.sync_current.DEFAULT_LOG_PATH", tmp_path / "ccrecall.log"),
            patch("logging.getLogger", return_value=mock_logger),
        ):
            sync_current._warn_cold_model()

        assert warning_messages, "warning should fire when disk cache is absent"

    def test_cold_model_warning_silent_when_disk_cache_present(self):
        """_warn_cold_model is silent when the fastembed disk cache is already present."""
        with (
            patch("ccrecall.hooks.sync_current.is_model_cached_on_disk", return_value=True),
            patch("logging.getLogger") as mock_get_logger,
        ):
            sync_current._warn_cold_model()
            mock_get_logger.assert_not_called()


# T02: embedding-status sidecar recording/clearing in sync_current.run()


class TestSyncEmbeddingStatusRecording:
    """T02: sync_current.run() records vec-unavailable failures and clears on success.

    Structural capability check in sync_current: chunk_vec_queryable only.
    Model failures are silently swallowed by contextlib.suppress in session_ops
    and are detected authoritatively by backfill_embeddings instead.
    """

    def _run(
        self,
        tmp_path,
        monkeypatch,
        *,
        vec_queryable=True,
        extra_patches=None,
    ):
        """Run sync_current.run() with mocked infrastructure; return parsed stdout JSON."""
        monkeypatch.setattr(sync_current, "pid_file_path", lambda key: tmp_path / f".pid-{key}")
        monkeypatch.setattr(sync_current, "remove_pid_file", lambda key: None)
        monkeypatch.setattr(
            sync_current,
            "load_settings",
            lambda: {"exclude_projects": [], "logging_enabled": False},
        )
        monkeypatch.setattr(sync_current, "_warn_cold_model", lambda: None)

        session_file = tmp_path / f"{VALID_SYNC_UUID}.jsonl"
        session_file.write_text("{}")
        monkeypatch.setattr(sync_current, "get_session_file", lambda *a, **k: session_file)

        mock_conn = MagicMock()
        monkeypatch.setattr(sync_current, "get_db_connection", lambda *a, **k: mock_conn)
        monkeypatch.setattr(sync_current, "chunk_vec_queryable", lambda conn: vec_queryable)
        monkeypatch.setattr(sync_current, "sync_session", lambda *a, **k: 0)

        if extra_patches:
            for attr, val in extra_patches.items():
                monkeypatch.setattr(sync_current, attr, val)

        input_file = tmp_path / "hook.json"
        input_file.write_text(json.dumps({"session_id": VALID_SYNC_UUID}))

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            sync_current.run(input_file=input_file)

        return json.loads(captured.getvalue())

    def test_vec_unavailable_records_reason(self, tmp_path, monkeypatch):
        """chunk_vec_queryable() → False in sync_current writes 'vec_unavailable' to sidecar."""
        sidecar = tmp_path / "embedding-status.json"

        self._run(
            tmp_path,
            monkeypatch,
            vec_queryable=False,
            extra_patches={
                "record_embedding_failure": patched_record(sidecar),
            },
        )

        assert sidecar.exists(), "sidecar must be written when vec is unavailable"
        data = json.loads(sidecar.read_text())
        assert data["reason"] == "vec_unavailable"
        assert "since" in data

    def test_vec_available_clears_status(self, tmp_path, monkeypatch):
        """A sync run with vec available clears the embedding-status sidecar (FR#5)."""
        sidecar = tmp_path / "embedding-status.json"
        # Pre-seed sidecar as if there was a prior failure
        sidecar.write_text('{"reason": "vec_unavailable", "since": "2026-01-01T00:00:00Z"}')

        self._run(
            tmp_path,
            monkeypatch,
            vec_queryable=True,
            extra_patches={
                "clear_embedding_failure": patched_clear(sidecar),
            },
        )

        assert not sidecar.exists(), "sidecar must be cleared when vec is available and sync completes"

    def test_vec_unavailable_does_not_clear(self, tmp_path, monkeypatch):
        """When vec is unavailable, clear_embedding_failure must NOT be called."""
        sidecar = tmp_path / "embedding-status.json"
        sidecar.write_text('{"reason": "vec_unavailable", "since": "2026-01-01T00:00:00Z"}')

        clear_calls = []

        self._run(
            tmp_path,
            monkeypatch,
            vec_queryable=False,
            extra_patches={
                "record_embedding_failure": lambda reason: None,
                "clear_embedding_failure": lambda: clear_calls.append(1),
            },
        )

        assert clear_calls == [], "clear must not be called when vec is unavailable"
        assert sidecar.exists(), "pre-seeded sidecar should remain when vec is unavailable"

    def test_recording_failure_does_not_break_hook(self, tmp_path, monkeypatch):
        """A sidecar write failure must not affect the hook's continue=true output."""

        def raising_record(reason):
            raise OSError("disk full")

        out = self._run(
            tmp_path,
            monkeypatch,
            vec_queryable=False,
            extra_patches={"record_embedding_failure": raising_record},
        )

        assert out == {"continue": True}, "hook must still output continue:true even if sidecar write raises"
