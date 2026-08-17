"""Tests for message_ops.py: content parsing, session upsert, and message row ops."""

import sqlite3

from conftest import make_jsonl_entry

from ccrecall.message_ops import (
    build_message_row,
    insert_new_messages,
    message_content_parts,
    update_missing_tool_content,
    upsert_session,
)

TS = "2026-01-01T00:00:00Z"


def _setup_session(conn: sqlite3.Connection, uuid: str = "sess-1") -> int:
    cursor = conn.cursor()
    cursor.execute("INSERT INTO projects (path, key) VALUES (?, ?)", (f"/tmp/{uuid}", uuid))
    project_id = cursor.lastrowid
    session_id = upsert_session(cursor, uuid, project_id, {"git_branch": "main", "cwd": "/tmp"})
    conn.commit()
    return session_id


class TestMessageContentParts:
    def test_valid_user_entry(self):
        entry = make_jsonl_entry("u1", None, TS, "user", "hello world")
        result = message_content_parts(entry)
        assert result == ("hello world", False, "")

    def test_valid_assistant_entry(self):
        entry = make_jsonl_entry("a1", "u1", TS, "assistant", [{"type": "text", "text": "reply text"}])
        result = message_content_parts(entry)
        assert result == ("reply text", False, "")

    def test_non_insertable_entry_returns_none(self):
        entry = {"type": "summary", "uuid": "s1"}
        assert message_content_parts(entry) is None

    def test_tool_result_user_entry_returns_none(self):
        entry = make_jsonl_entry("u2", None, TS, "user", [{"type": "tool_result", "content": "output"}])
        assert message_content_parts(entry) is None

    def test_empty_text_and_tool_content_returns_none(self):
        entry = make_jsonl_entry("a2", None, TS, "assistant", [])
        assert message_content_parts(entry) is None

    def test_assistant_tool_use_block_populates_tool_content(self):
        content = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]
        entry = make_jsonl_entry("a3", None, TS, "assistant", content)
        text, has_thinking, tool_content = message_content_parts(entry)
        assert text == ""
        assert has_thinking is False
        assert tool_content == "[Bash: ls -la]"


class TestUpsertSession:
    def test_inserts_new_session_and_returns_id(self, memory_db):
        cursor = memory_db.cursor()
        cursor.execute("INSERT INTO projects (path, key) VALUES (?, ?)", ("/tmp/proj", "proj"))
        project_id = cursor.lastrowid

        session_id = upsert_session(cursor, "sess-uuid-1", project_id, {"git_branch": "main", "cwd": "/tmp/proj"})
        memory_db.commit()

        assert session_id is not None
        cursor.execute("SELECT uuid, project_id, git_branch, cwd FROM sessions WHERE id = ?", (session_id,))
        assert cursor.fetchone() == ("sess-uuid-1", project_id, "main", "/tmp/proj")

    def test_updates_git_branch_and_cwd_on_conflict(self, memory_db):
        cursor = memory_db.cursor()
        cursor.execute("INSERT INTO projects (path, key) VALUES (?, ?)", ("/tmp/proj", "proj"))
        project_id = cursor.lastrowid

        first_id = upsert_session(cursor, "sess-uuid-2", project_id, {"git_branch": "main", "cwd": "/tmp/proj"})
        second_id = upsert_session(cursor, "sess-uuid-2", project_id, {"git_branch": "feature-x", "cwd": "/tmp/proj2"})
        memory_db.commit()

        assert first_id == second_id
        cursor.execute("SELECT git_branch, cwd FROM sessions WHERE id = ?", (first_id,))
        assert cursor.fetchone() == ("feature-x", "/tmp/proj2")

    def test_null_excluded_values_do_not_overwrite_existing(self, memory_db):
        cursor = memory_db.cursor()
        cursor.execute("INSERT INTO projects (path, key) VALUES (?, ?)", ("/tmp/proj", "proj"))
        project_id = cursor.lastrowid

        session_id = upsert_session(cursor, "sess-uuid-3", project_id, {"git_branch": "main", "cwd": "/tmp/proj"})
        upsert_session(cursor, "sess-uuid-3", project_id, {"git_branch": None, "cwd": None})
        memory_db.commit()

        cursor.execute("SELECT git_branch, cwd FROM sessions WHERE id = ?", (session_id,))
        assert cursor.fetchone() == ("main", "/tmp/proj")


class TestBuildMessageRow:
    # build_message_row returns:
    # (session_id, uuid, parent_uuid, timestamp, role, text, has_thinking, is_notification, origin, tool_content)

    def test_valid_entry_returns_tuple(self, memory_db):
        session_id = _setup_session(memory_db, "sess-a")
        entry = make_jsonl_entry("uuid-1", None, TS, "user", "hello world")

        row = build_message_row(entry, session_id, {"uuid-1"}, set())

        assert row is not None
        sid, uuid, _parent, _ts, role, text, *_ = row
        assert sid == session_id
        assert uuid == "uuid-1"
        assert role == "user"
        assert text == "hello world"

    def test_missing_uuid_returns_none(self, memory_db):
        session_id = _setup_session(memory_db, "sess-b")
        entry = make_jsonl_entry(None, None, TS, "user", "hello")

        assert build_message_row(entry, session_id, set(), set()) is None

    def test_uuid_not_in_valid_branch_uuids_returns_none(self, memory_db):
        session_id = _setup_session(memory_db, "sess-c")
        entry = make_jsonl_entry("uuid-2", None, TS, "user", "hello")

        assert build_message_row(entry, session_id, {"other-uuid"}, set()) is None

    def test_uuid_already_in_existing_uuids_returns_none(self, memory_db):
        session_id = _setup_session(memory_db, "sess-d")
        entry = make_jsonl_entry("uuid-3", None, TS, "user", "hello")

        assert build_message_row(entry, session_id, {"uuid-3"}, {"uuid-3"}) is None

    def test_message_content_parts_none_returns_none(self, memory_db):
        session_id = _setup_session(memory_db, "sess-e")
        entry = {"type": "summary", "uuid": "uuid-4"}

        assert build_message_row(entry, session_id, {"uuid-4"}, set()) is None

    def test_task_notification_sets_is_notification(self, memory_db):
        session_id = _setup_session(memory_db, "sess-f")
        entry = make_jsonl_entry("uuid-5", None, TS, "user", "<task-notification>done</task-notification>")

        row = build_message_row(entry, session_id, {"uuid-5"}, set())

        assert row is not None
        *_, is_notification, _origin, _tool_content = row
        assert is_notification == 1

    def test_teammate_message_sets_is_notification(self, memory_db):
        session_id = _setup_session(memory_db, "sess-g")
        entry = make_jsonl_entry("uuid-6", None, TS, "user", "<teammate-message>hi</teammate-message>")

        row = build_message_row(entry, session_id, {"uuid-6"}, set())

        assert row is not None
        *_, is_notification, _origin, _tool_content = row
        assert is_notification == 1

    def test_regular_message_is_notification_zero(self, memory_db):
        session_id = _setup_session(memory_db, "sess-h")
        entry = make_jsonl_entry("uuid-7", None, TS, "user", "just a regular message")

        row = build_message_row(entry, session_id, {"uuid-7"}, set())

        assert row is not None
        *_, is_notification, _origin, _tool_content = row
        assert is_notification == 0

    def test_includes_tool_content_in_returned_tuple(self, memory_db):
        session_id = _setup_session(memory_db, "sess-i")
        content = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]
        entry = make_jsonl_entry("uuid-8", None, TS, "assistant", content)

        row = build_message_row(entry, session_id, {"uuid-8"}, set())

        assert row is not None
        _, _, _, _, _, text, _, _, _, tool_content = row
        assert tool_content == "[Bash: ls -la]"
        assert text == ""


class TestInsertNewMessages:
    def test_inserts_new_messages_and_returns_count(self, memory_db):
        session_id = _setup_session(memory_db, "sess-j")
        cursor = memory_db.cursor()
        messages = [
            make_jsonl_entry("uuid-1", None, TS, "user", "hello"),
            make_jsonl_entry("uuid-2", "uuid-1", TS, "assistant", [{"type": "text", "text": "hi"}]),
        ]

        count = insert_new_messages(cursor, session_id, messages, {"uuid-1", "uuid-2"}, set())
        memory_db.commit()

        assert count == 2
        cursor.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,))
        assert cursor.fetchone()[0] == 2

    def test_skips_messages_already_in_existing_uuids(self, memory_db):
        session_id = _setup_session(memory_db, "sess-k")
        cursor = memory_db.cursor()
        messages = [
            make_jsonl_entry("uuid-1", None, TS, "user", "hello"),
            make_jsonl_entry("uuid-2", "uuid-1", TS, "assistant", [{"type": "text", "text": "hi"}]),
        ]

        count = insert_new_messages(cursor, session_id, messages, {"uuid-1", "uuid-2"}, {"uuid-1"})

        assert count == 1

    def test_adds_newly_inserted_uuids_to_existing_uuids(self, memory_db):
        session_id = _setup_session(memory_db, "sess-l")
        cursor = memory_db.cursor()
        messages = [make_jsonl_entry("uuid-1", None, TS, "user", "hello")]
        existing_uuids: set[str] = set()

        insert_new_messages(cursor, session_id, messages, {"uuid-1"}, existing_uuids)

        assert "uuid-1" in existing_uuids

    def test_on_conflict_do_nothing_does_not_raise(self, memory_db):
        session_id = _setup_session(memory_db, "sess-m")
        cursor = memory_db.cursor()
        # Row already present (simulates a race where existing_uuids hasn't
        # caught up with what's actually in the DB).
        cursor.execute(
            "INSERT INTO messages (session_id, uuid, parent_uuid, timestamp, role, content) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, "uuid-1", None, TS, "user", "hello"),
        )
        messages = [make_jsonl_entry("uuid-1", None, TS, "user", "hello")]
        existing_uuids: set[str] = set()

        count = insert_new_messages(cursor, session_id, messages, {"uuid-1"}, existing_uuids)

        assert count == 0
        assert "uuid-1" not in existing_uuids


class TestUpdateMissingToolContent:
    def test_updates_null_tool_content(self, memory_db):
        session_id = _setup_session(memory_db, "sess-n")
        cursor = memory_db.cursor()
        cursor.execute(
            "INSERT INTO messages (session_id, uuid, parent_uuid, timestamp, role, content, tool_content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, "uuid-1", None, TS, "assistant", "", None),
        )
        content = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
        entry = make_jsonl_entry("uuid-1", None, TS, "assistant", content)

        updated = update_missing_tool_content(cursor, session_id, [entry], {"uuid-1"})
        memory_db.commit()

        assert updated == 1
        cursor.execute("SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "uuid-1"))
        assert cursor.fetchone()[0] == "[Bash: ls]"

    def test_skips_messages_not_in_existing_uuids(self, memory_db):
        session_id = _setup_session(memory_db, "sess-o")
        cursor = memory_db.cursor()
        cursor.execute(
            "INSERT INTO messages (session_id, uuid, parent_uuid, timestamp, role, content, tool_content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, "uuid-1", None, TS, "assistant", "", None),
        )
        content = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
        entry = make_jsonl_entry("uuid-1", None, TS, "assistant", content)

        updated = update_missing_tool_content(cursor, session_id, [entry], set())

        assert updated == 0
        cursor.execute("SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "uuid-1"))
        assert cursor.fetchone()[0] is None

    def test_does_not_overwrite_non_null_tool_content(self, memory_db):
        session_id = _setup_session(memory_db, "sess-p")
        cursor = memory_db.cursor()
        cursor.execute(
            "INSERT INTO messages (session_id, uuid, parent_uuid, timestamp, role, content, tool_content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, "uuid-1", None, TS, "assistant", "", "[Existing: marker]"),
        )
        content = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
        entry = make_jsonl_entry("uuid-1", None, TS, "assistant", content)

        updated = update_missing_tool_content(cursor, session_id, [entry], {"uuid-1"})

        assert updated == 0
        cursor.execute("SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "uuid-1"))
        assert cursor.fetchone()[0] == "[Existing: marker]"
