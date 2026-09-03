"""Tests for branch_ops link-table writes (#158).

Every test database is normally built from SCHEMA_CORE, so the code's view of
branch_messages and the table's real shape can never diverge in tests. These
tests rebuild the table with drifted DDL on purpose, to assert that a link
write which violates a constraint raises instead of being silently dropped
(the failure mode that hid #155 for a week).
"""

import sqlite3

import pytest

from ccrecall.branch_ops import diff_branch_messages

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
