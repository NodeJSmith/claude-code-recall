"""Tests for classifying sessions whose tool-call content is still pending."""

import json

from ccrecall.tool_content_status import classify_pending_sessions


def test_pending_missing_jsonl_counts_only_unrecoverable_pending_rows(memory_db, tmp_path):
    parent = tmp_path / "sess-partial.jsonl"
    missing_agent = tmp_path / "agent-sess-partial.jsonl"
    parent.write_text(
        json.dumps(
            {
                "uuid": "u1",
                "parentUuid": None,
                "type": "user",
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {"role": "user", "content": "recoverable"},
            }
        )
        + "\n"
    )
    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-partial')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    memory_db.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'u1', 1)", (session_id,))
    memory_db.execute(
        "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, 'u1', 'user', 'x', NULL)",
        (session_id,),
    )
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', 1)",
        (str(parent),),
    )
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', 0)",
        (str(missing_agent),),
    )
    memory_db.commit()

    assert classify_pending_sessions(memory_db.cursor(), None)["missing"] == 0

    memory_db.execute("UPDATE messages SET uuid = 'agent-only' WHERE session_id = ?", (session_id,))
    memory_db.commit()

    assert classify_pending_sessions(memory_db.cursor(), None)["missing"] == 1


def test_pending_missing_jsonl_requires_all_pending_rows_recoverable(memory_db, tmp_path):
    parent = tmp_path / "sess-mixed.jsonl"
    missing_agent = tmp_path / "agent-sess-mixed.jsonl"
    parent.write_text(
        json.dumps(
            {
                "uuid": "u1",
                "parentUuid": None,
                "type": "user",
                "timestamp": "2026-01-01T10:00:00Z",
                "message": {"role": "user", "content": "recoverable"},
            }
        )
        + "\n"
    )
    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-mixed')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    memory_db.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'u1', 1)", (session_id,))
    memory_db.execute(
        "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, 'u1', 'user', 'x', NULL)",
        (session_id,),
    )
    memory_db.execute(
        "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, 'agent-only', 'assistant', 'x', NULL)",
        (session_id,),
    )
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', 1)",
        (str(parent),),
    )
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', 1)",
        (str(missing_agent),),
    )
    memory_db.commit()

    assert classify_pending_sessions(memory_db.cursor(), None)["missing"] == 1


def test_session_with_no_import_log_entry_counts_as_missing(memory_db):
    """A session synced via the hook (not the import CLI) has no import_log
    entry. If its JSONL has since been deleted, classify_pending_sessions must
    count it as ``missing`` — not silently skip it, which would inflate
    ``pending_backfillable_sessions`` in the status report."""
    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-no-log')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    memory_db.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'u1', 1)", (session_id,))
    memory_db.execute(
        "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, 'u1', 'user', 'x', NULL)",
        (session_id,),
    )
    memory_db.commit()

    result = classify_pending_sessions(memory_db.cursor(), None)
    assert result == {"missing": 1, "no_usable_branch": 0}


def test_pending_no_usable_branch_counts_surviving_jsonl_that_parses_to_nothing(memory_db, tmp_path):
    """A session whose JSONL survives on disk but parses to no uuid-bearing
    entries (or no branch) is a real backfill no-op — the `missing` bucket
    alone must not treat it as recoverable/backfillable."""
    empty_path = tmp_path / "sess-empty.jsonl"
    empty_path.write_text("\n")

    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-empty')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    memory_db.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'u1', 1)", (session_id,))
    memory_db.execute(
        "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, 'u1', 'user', 'x', NULL)",
        (session_id,),
    )
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', 1)",
        (str(empty_path),),
    )
    memory_db.commit()

    result = classify_pending_sessions(memory_db.cursor(), None)
    assert result["missing"] == 0, "the JSONL file exists, so it isn't missing"
    assert result["no_usable_branch"] == 1


def test_missing_and_no_usable_branch_buckets_never_overlap(memory_db, tmp_path):
    """A session with mixed source availability — one sibling file gone, the
    surviving sibling empty/invalid — must land in exactly one bucket, not
    both, so `pending_backfillable_sessions` can't go negative (#207)."""
    gone_path = tmp_path / "sess-mixed.jsonl"  # recorded in import_log, deleted before this test runs
    surviving_path = tmp_path / "agent-sess-mixed.jsonl"
    surviving_path.write_text("\n")  # exists, but parses to no entries

    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-mixed')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    memory_db.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'u1', 1)", (session_id,))
    memory_db.execute(
        "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, 'u1', 'user', 'x', NULL)",
        (session_id,),
    )
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', 1)",
        (str(gone_path),),
    )
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', 0)",
        (str(surviving_path),),
    )
    memory_db.commit()

    result = classify_pending_sessions(memory_db.cursor(), None)
    assert result == {"missing": 1, "no_usable_branch": 0}, (
        "a session must land in exactly one bucket — never counted in both"
    )


def test_transcript_unreadable_after_index_counts_as_missing(memory_db, tmp_path):
    """`import_log_source_index()` classifies a path as `existing` via
    `Path.exists()`; if it vanishes or becomes unopenable before
    `classify_pending_sessions` reads it (TOCTOU race, permissions change), that
    must land in `missing` rather than raising `OSError` and aborting the whole
    status report (#207)."""
    unreadable_path = tmp_path / "sess-unreadable.jsonl"
    unreadable_path.mkdir()  # exists per Path.exists(), but open() raises IsADirectoryError

    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-unreadable')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    memory_db.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'u1', 1)", (session_id,))
    memory_db.execute(
        "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, 'u1', 'user', 'x', NULL)",
        (session_id,),
    )
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', 1)",
        (str(unreadable_path),),
    )
    memory_db.commit()

    result = classify_pending_sessions(memory_db.cursor(), None)
    assert result == {"missing": 1, "no_usable_branch": 0}
