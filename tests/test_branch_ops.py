"""Tests for branch_ops link-table writes (#158).

Every test database is normally built from SCHEMA_CORE, so the code's view of
branch_messages and the table's real shape can never diverge in tests. These
tests rebuild the table with drifted DDL on purpose, to assert that a link
write which violates a constraint raises instead of being silently dropped
(the failure mode that hid #155 for a week).
"""

import sqlite3

import pytest

from ccrecall.branch_ops import diff_branch_messages, insert_branch_message_links

DRIFTED_BRANCH_MESSAGES_DDL = """
CREATE TABLE branch_messages (
  branch_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY (branch_id, message_id)
)
"""


def rebuild_branch_messages_with_drift(conn: sqlite3.Connection) -> None:
    """Replace branch_messages with a version carrying an unexpected NOT NULL column."""
    cursor = conn.cursor()
    cursor.execute("DROP INDEX IF EXISTS idx_branch_messages_message")
    cursor.execute("DROP TABLE branch_messages")
    cursor.execute(DRIFTED_BRANCH_MESSAGES_DDL)
    conn.commit()


def test_diff_branch_messages_raises_on_constraint_violation(memory_db):
    rebuild_branch_messages_with_drift(memory_db)
    cursor = memory_db.cursor()

    with pytest.raises(sqlite3.IntegrityError):
        diff_branch_messages(cursor, 1, ["uuid-1"], {"uuid-1": 42})

    # The write must not be silently dropped: either it lands or it raises.
    cursor.execute("SELECT COUNT(*) FROM branch_messages")
    assert cursor.fetchone()[0] == 0


def seed_branch_with_message(conn: sqlite3.Connection) -> tuple[int, int]:
    """Insert a project/session/branch/message chain; return (branch_id, message_id)."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
        ("/home/user/proj", "-home-user-proj", "proj"),
    )
    cursor.execute(
        "INSERT INTO sessions (uuid, project_id, cwd) VALUES (?, ?, ?)",
        ("sess-bo-1", cursor.lastrowid, "/home/user/proj"),
    )
    sess_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count) VALUES (?, ?, 1, 1)",
        (sess_id, "leaf-bo-1"),
    )
    branch_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (sess_id, "msg-bo-1", "user", "hello", "2025-01-01T10:00:00Z"),
    )
    msg_id = cursor.lastrowid
    assert branch_id is not None
    assert msg_id is not None
    return branch_id, msg_id


def test_insert_branch_message_links_absorbs_duplicate_links(memory_db):
    branch_id, msg_id = seed_branch_with_message(memory_db)
    cursor = memory_db.cursor()

    insert_branch_message_links(cursor, branch_id, {msg_id})
    # A concurrent writer creating the same link first must not raise.
    insert_branch_message_links(cursor, branch_id, {msg_id})

    cursor.execute("SELECT COUNT(*) FROM branch_messages")
    assert cursor.fetchone()[0] == 1


def test_insert_branch_message_links_raises_on_foreign_key_violation(memory_db):
    branch_id, _ = seed_branch_with_message(memory_db)
    cursor = memory_db.cursor()

    with pytest.raises(sqlite3.IntegrityError):
        insert_branch_message_links(cursor, branch_id, {99999})
