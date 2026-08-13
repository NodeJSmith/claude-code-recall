"""Tests for the canonical DB-only Session Recap input boundary."""

import hashlib

import pytest

import ccrecall.recap_input as recap_input
from ccrecall.recap_input import (
    ELIGIBILITY_POLICY_VERSION,
    RECAP_INPUT_CONTRACT_VERSION,
    canonical_json,
    load_recap_input,
    refresh_recap_input,
)


def _seed(cursor):
    cursor.execute("INSERT INTO projects (path, key, name) VALUES ('/project', 'project', 'Project')")
    project_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO sessions (uuid, project_id, git_branch, cwd) VALUES ('session', ?, 'main', '/project')",
        (project_id,),
    )
    session_id = cursor.lastrowid
    cursor.execute(
        """INSERT INTO branches (
            session_id, leaf_uuid, context_summary, context_summary_json, files_modified, commits, tool_counts
        ) VALUES (?, 'leaf', 'Deterministic summary', '{"b":2,"a":1}', '["src/a.py"]', '["abc work"]', '{"Read":2}')""",
        (session_id,),
    )
    return session_id, cursor.lastrowid


def _message(cursor, session_id, uuid, content, tool_content="", notification=0, origin=None):
    cursor.execute(
        """INSERT INTO messages (session_id, uuid, parent_uuid, timestamp, role, content, tool_content, is_notification, origin)
        VALUES (?, ?, NULL, '2026-01-01T00:00:00Z', 'assistant', ?, ?, ?, ?)""",
        (session_id, uuid, content, tool_content, notification, origin),
    )
    return cursor.lastrowid


def test_packet_and_hash_share_one_canonical_projection_without_source_paths(memory_db):
    cursor = memory_db.cursor()
    session_id, branch_id = _seed(cursor)
    second = _message(cursor, session_id, "second", "second", "[Read: src/a.py]")
    first = _message(cursor, session_id, "first", "first")
    cursor.execute("INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, 0)", (branch_id, first))
    cursor.execute(
        "INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, 1)", (branch_id, second)
    )
    before = load_recap_input(cursor, branch_id)
    cursor.execute("INSERT INTO import_log (file_path) VALUES ('/sensitive/transcript.jsonl')")
    after = load_recap_input(cursor, branch_id)

    assert before.projection == after.projection
    assert before.packet == canonical_json(before.projection)
    assert before.input_hash == hashlib.sha256(before.packet).hexdigest()
    assert before.projection["ordered_messages"][0]["uuid"] == "first"
    assert before.projection["ordered_messages"][1]["tool_content"] == "[Read: src/a.py]"
    assert b"transcript.jsonl" not in before.packet


def test_changing_only_session_cwd_does_not_change_packet_or_hash(memory_db):
    cursor = memory_db.cursor()
    session_id, branch_id = _seed(cursor)
    message_id = _message(cursor, session_id, "message", "keep")
    cursor.execute(
        "INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, 0)", (branch_id, message_id)
    )
    before = load_recap_input(cursor, branch_id)

    cursor.execute("UPDATE sessions SET cwd = '/another/local/checkout' WHERE id = ?", (session_id,))
    after = load_recap_input(cursor, branch_id)

    assert after.projection == before.projection
    assert after.packet == before.packet
    assert after.input_hash == before.input_hash


def test_changing_only_files_modified_paths_does_not_change_packet_or_hash(memory_db):
    cursor = memory_db.cursor()
    _, branch_id = _seed(cursor)
    before = load_recap_input(cursor, branch_id)

    cursor.execute(
        "UPDATE branches SET files_modified = ? WHERE id = ?",
        ('["/another/local/checkout/src/a.py"]', branch_id),
    )
    after = load_recap_input(cursor, branch_id)

    assert after.projection == before.projection
    assert after.packet == before.packet
    assert after.input_hash == before.input_hash


@pytest.mark.parametrize(
    ("change", "expected_field"),
    [
        ("position", "ordered_messages"),
        ("content", "ordered_messages"),
        ("tool_content", "ordered_messages"),
        ("metadata", "metadata"),
        ("versions", "input_contract_version"),
    ],
)
def test_canonical_recap_inputs_each_change_packet_and_hash(memory_db, monkeypatch, change, expected_field):
    cursor = memory_db.cursor()
    session_id, branch_id = _seed(cursor)
    first = _message(cursor, session_id, "first", "first")
    second = _message(cursor, session_id, "second", "second")
    cursor.execute("INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, 0)", (branch_id, first))
    cursor.execute(
        "INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, 1)", (branch_id, second)
    )
    before = load_recap_input(cursor, branch_id)

    if change == "position":
        cursor.execute(
            "UPDATE branch_messages SET position = 2 WHERE branch_id = ? AND message_id = ?", (branch_id, first)
        )
        cursor.execute(
            "UPDATE branch_messages SET position = 0 WHERE branch_id = ? AND message_id = ?", (branch_id, second)
        )
    elif change == "content":
        cursor.execute("UPDATE messages SET content = 'changed prose' WHERE id = ?", (first,))
    elif change == "tool_content":
        cursor.execute("UPDATE messages SET tool_content = '[Read: src/a.py]' WHERE id = ?", (first,))
    elif change == "metadata":
        cursor.execute("UPDATE sessions SET git_branch = 'release' WHERE id = ?", (session_id,))
    else:
        monkeypatch.setattr(recap_input, "RECAP_INPUT_CONTRACT_VERSION", RECAP_INPUT_CONTRACT_VERSION + 1)
        monkeypatch.setattr(recap_input, "ELIGIBILITY_POLICY_VERSION", ELIGIBILITY_POLICY_VERSION + 1)

    after = load_recap_input(cursor, branch_id)

    assert after.projection[expected_field] != before.projection[expected_field]
    assert after.packet != before.packet
    assert after.input_hash != before.input_hash


def test_projection_excludes_notifications_external_and_inactive_branches(memory_db):
    cursor = memory_db.cursor()
    session_id, branch_id = _seed(cursor)
    admitted = _message(cursor, session_id, "admitted", "keep")
    notification = _message(cursor, session_id, "notification", "drop", notification=1)
    cursor.execute("INSERT INTO sessions (uuid) VALUES ('other')")
    external = _message(cursor, cursor.lastrowid, "external", "drop")
    for position, message_id in enumerate((admitted, notification, external)):
        cursor.execute(
            "INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, ?)",
            (branch_id, message_id, position),
        )

    recap_input = load_recap_input(cursor, branch_id)
    assert [message["uuid"] for message in recap_input.projection["ordered_messages"]] == ["admitted"]
    cursor.execute("UPDATE branches SET is_active = 0 WHERE id = ?", (branch_id,))
    with pytest.raises(ValueError, match="no active branch"):
        load_recap_input(cursor, branch_id)


def test_refresh_stores_versions_and_only_requeues_content_dependent_terminal_states(memory_db):
    cursor = memory_db.cursor()
    session_id, branch_id = _seed(cursor)
    message_id = _message(cursor, session_id, "message", "before")
    cursor.execute(
        "INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, 0)", (branch_id, message_id)
    )
    cursor.execute(
        "UPDATE branches SET embedding_version = 42, embedding_model = 'unchanged' WHERE id = ?", (branch_id,)
    )
    initial = refresh_recap_input(cursor, branch_id)
    cursor.executemany(
        """INSERT INTO session_recap_jobs (session_id, requested_input_hash, trigger, state, reason, requested_at, updated_at)
        VALUES (?, ?, 'test', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        [
            (session_id, initial.input_hash, "current", None),
        ],
    )
    cursor.execute("UPDATE messages SET content = 'after' WHERE id = ?", (message_id,))
    refreshed = refresh_recap_input(cursor, branch_id)
    branch = cursor.execute(
        "SELECT recap_input_hash, recap_input_contract_version, recap_eligibility_policy_version FROM branches WHERE id = ?",
        (branch_id,),
    ).fetchone()
    job = cursor.execute(
        "SELECT requested_input_hash, state, reason FROM session_recap_jobs WHERE session_id = ?", (session_id,)
    ).fetchone()

    assert refreshed.input_hash != initial.input_hash
    assert branch == (refreshed.input_hash, RECAP_INPUT_CONTRACT_VERSION, ELIGIBILITY_POLICY_VERSION)
    assert job == (refreshed.input_hash, "pending", None)
    assert cursor.execute(
        "SELECT embedding_version, embedding_model FROM branches WHERE id = ?", (branch_id,)
    ).fetchone() == (42, "unchanged")


def test_platform_unsupported_block_is_preserved_when_input_changes(memory_db):
    cursor = memory_db.cursor()
    session_id, branch_id = _seed(cursor)
    message_id = _message(cursor, session_id, "message", "before")
    cursor.execute(
        "INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, 0)", (branch_id, message_id)
    )
    initial = refresh_recap_input(cursor, branch_id)
    cursor.execute(
        """INSERT INTO session_recap_jobs (session_id, requested_input_hash, trigger, state, reason, requested_at, updated_at)
        VALUES (?, ?, 'test', 'blocked', 'platform_unsupported', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        (session_id, initial.input_hash),
    )
    cursor.execute("UPDATE messages SET tool_content = '[Edit: src/a.py]' WHERE id = ?", (message_id,))
    refreshed = refresh_recap_input(cursor, branch_id)
    assert cursor.execute(
        "SELECT requested_input_hash, state, reason FROM session_recap_jobs WHERE session_id = ?", (session_id,)
    ).fetchone() == (
        refreshed.input_hash,
        "blocked",
        "platform_unsupported",
    )


@pytest.mark.parametrize("reason", ["cleanup_failed", "policy_disabled"])
def test_stable_block_is_preserved_when_input_changes(memory_db, reason):
    cursor = memory_db.cursor()
    session_id, branch_id = _seed(cursor)
    message_id = _message(cursor, session_id, "message", "before")
    cursor.execute(
        "INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, 0)", (branch_id, message_id)
    )
    initial = refresh_recap_input(cursor, branch_id)
    cursor.execute(
        """INSERT INTO session_recap_jobs (session_id, requested_input_hash, trigger, state, reason, requested_at, updated_at)
        VALUES (?, ?, 'test', 'blocked', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        (session_id, initial.input_hash, reason),
    )
    cursor.execute("UPDATE messages SET content = 'after' WHERE id = ?", (message_id,))
    refreshed = refresh_recap_input(cursor, branch_id)

    assert cursor.execute(
        "SELECT requested_input_hash, state, reason FROM session_recap_jobs WHERE session_id = ?", (session_id,)
    ).fetchone() == (refreshed.input_hash, "blocked", reason)


def test_content_dependent_block_is_requeued_when_input_changes(memory_db):
    cursor = memory_db.cursor()
    session_id, branch_id = _seed(cursor)
    message_id = _message(cursor, session_id, "message", "before")
    cursor.execute(
        "INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, 0)", (branch_id, message_id)
    )
    initial = refresh_recap_input(cursor, branch_id)
    cursor.execute(
        """INSERT INTO session_recap_jobs (session_id, requested_input_hash, trigger, state, reason, requested_at, updated_at)
        VALUES (?, ?, 'test', 'blocked', 'unusable_output', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        (session_id, initial.input_hash),
    )
    cursor.execute("UPDATE messages SET content = 'after' WHERE id = ?", (message_id,))
    refresh_recap_input(cursor, branch_id)

    assert cursor.execute(
        "SELECT state, reason FROM session_recap_jobs WHERE session_id = ?", (session_id,)
    ).fetchone() == ("pending", None)


def test_decoded_metadata_and_explicit_nulls_are_canonical(memory_db):
    cursor = memory_db.cursor()
    session_id, branch_id = _seed(cursor)
    message_id = _message(cursor, session_id, "tool-only", "", "[Read: src/a.py]")
    cursor.execute(
        "INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, 0)", (branch_id, message_id)
    )
    projection = load_recap_input(cursor, branch_id).projection

    assert projection["deterministic_summary"]["data"] == {"a": 1, "b": 2}
    assert projection["ordered_messages"][0]["origin"] is None
    assert projection["ordered_messages"][0]["content"] == ""
    assert projection["ordered_messages"][0]["tool_content"] == "[Read: src/a.py]"
