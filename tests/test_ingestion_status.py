"""Tests for transcript-vs-DB ingestion diagnostics."""

import os
import time
from pathlib import Path
from unittest.mock import patch

from conftest import make_jsonl_entry as _entry
from conftest import write_jsonl as _write_jsonl

from ccrecall import parsing
from ccrecall.ingestion_status import summarize_ingestion


def _seed_session(memory_db, filepath: Path, db_uuids: list[str]) -> None:
    session_uuid = filepath.stem.removeprefix("agent-")
    memory_db.execute("INSERT INTO sessions (uuid) VALUES (?)", (session_uuid,))
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for uuid in db_uuids:
        memory_db.execute(
            "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, ?, 'user', 'x', '')",
            (session_id, uuid),
        )
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', ?)",
        (str(filepath), len(db_uuids)),
    )
    memory_db.commit()


def _insert_import_log(memory_db, filepath: Path, message_count: int = 0) -> None:
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', ?)",
        (str(filepath), message_count),
    )
    memory_db.commit()


def _cache_row(memory_db, session_uuid: str) -> tuple[str, str, str] | None:
    return memory_db.execute(
        "SELECT session_uuid, source_fingerprint, checked_at FROM ingestion_check_cache WHERE session_uuid = ?",
        (session_uuid,),
    ).fetchone()


def _write_four_turns(filepath: Path) -> None:
    _write_jsonl(
        filepath,
        [
            _entry("u1", None, "2026-01-01T10:00:00Z", "user", "first"),
            _entry("a1", "u1", "2026-01-01T10:00:01Z", "assistant", "answer"),
            _entry("u2", "a1", "2026-01-01T10:00:02Z", "user", "second"),
            _entry("a2", "u2", "2026-01-01T10:00:03Z", "assistant", "answer"),
        ],
    )


def test_pending_tail_for_recent_contiguous_suffix(memory_db, tmp_path):
    filepath = tmp_path / "sess-tail.jsonl"
    _write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1"])

    status = summarize_ingestion(memory_db)

    assert status["pending_tail_sessions"] == 1
    assert status["pending_tail_turns"] == 2
    assert status["ingestion_gap_sessions"] == 0


def test_stale_tail_for_old_contiguous_suffix(memory_db, tmp_path):
    filepath = tmp_path / "sess-stale.jsonl"
    _write_four_turns(filepath)
    old = time.time() - 3600
    os.utime(filepath, (old, old))
    _seed_session(memory_db, filepath, ["u1", "a1"])

    status = summarize_ingestion(memory_db)

    assert status["stale_tail_sessions"] == 1
    assert status["stale_tail_turns"] == 2
    assert status["pending_tail_sessions"] == 0


def test_middle_missing_uuid_is_ingestion_gap(memory_db, tmp_path):
    filepath = tmp_path / "sess-gap.jsonl"
    _write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "u2", "a2"])

    status = summarize_ingestion(memory_db)

    assert status["ingestion_gap_sessions"] == 1
    assert status["ingestion_gap_turns"] == 1
    assert status["pending_tail_sessions"] == 0


def test_missing_source_when_no_transcript_survives(memory_db, tmp_path):
    filepath = tmp_path / "sess-gone.jsonl"
    _seed_session(memory_db, filepath, ["u1"])

    status = summarize_ingestion(memory_db)

    assert status["missing_source_sessions"] == 1
    assert status["sessions_checked"] == 1


def test_complete_session_is_ok(memory_db, tmp_path):
    filepath = tmp_path / "sess-ok.jsonl"
    _write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"])

    status = summarize_ingestion(memory_db)

    assert status["ok_sessions"] == 1
    assert status["pending_tail_sessions"] == 0
    assert status["ingestion_gap_sessions"] == 0


def test_ok_session_records_cache_and_unchanged_second_run_skips_parsing(memory_db, tmp_path):
    filepath = tmp_path / "sess-cache.jsonl"
    _write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"])

    first = summarize_ingestion(memory_db)

    cached = _cache_row(memory_db, "sess-cache")
    assert first["sessions_checked"] == 1
    assert first["ok_sessions"] == 1
    assert cached is not None

    with patch(
        "ccrecall.ingestion_status.parse_all_with_uuids", side_effect=AssertionError("cache hit should skip parsing")
    ):
        second = summarize_ingestion(memory_db)

    assert second["sessions_checked"] == 1
    assert second["ok_sessions"] == 1
    assert _cache_row(memory_db, "sess-cache") == cached


def test_transcript_change_invalidates_cache_and_reparses(memory_db, tmp_path):
    filepath = tmp_path / "sess-cache-change.jsonl"
    _write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"])

    summarize_ingestion(memory_db)
    first_cache = _cache_row(memory_db, "sess-cache-change")
    assert first_cache is not None

    _write_jsonl(
        filepath,
        [
            _entry("u1", None, "2026-01-01T10:00:00Z", "user", "first"),
            _entry("a1", "u1", "2026-01-01T10:00:01Z", "assistant", "answer"),
            _entry("u2", "a1", "2026-01-01T10:00:02Z", "user", "second updated"),
            _entry("a2", "u2", "2026-01-01T10:00:03Z", "assistant", "answer"),
        ],
    )

    with patch("ccrecall.ingestion_status.parse_all_with_uuids", wraps=parsing.parse_all_with_uuids) as parse_all:
        status = summarize_ingestion(memory_db)

    assert parse_all.call_count == 1
    assert status["ok_sessions"] == 1
    assert _cache_row(memory_db, "sess-cache-change")[1] != first_cache[1]


def test_problem_session_is_not_cached_and_is_reparsed(memory_db, tmp_path):
    filepath = tmp_path / "sess-problem.jsonl"
    _write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1"])

    first = summarize_ingestion(memory_db)

    assert first["pending_tail_sessions"] == 1
    assert _cache_row(memory_db, "sess-problem") is None

    with patch("ccrecall.ingestion_status.parse_all_with_uuids", wraps=parsing.parse_all_with_uuids) as parse_all:
        second = summarize_ingestion(memory_db)

    assert parse_all.call_count == 1
    assert second["pending_tail_sessions"] == 1
    assert _cache_row(memory_db, "sess-problem") is None


def test_multifile_session_uses_parent_chain_not_import_log_order(memory_db, tmp_path):
    parent = tmp_path / "sess-multi.jsonl"
    agent = tmp_path / "agent-sess-multi.jsonl"
    _write_jsonl(
        parent,
        [
            _entry("u1", None, "2026-01-01T10:00:00Z", "user", "first"),
            _entry("a1", "u1", "2026-01-01T10:00:01Z", "assistant", "answer"),
        ],
    )
    _write_jsonl(
        agent,
        [
            _entry("u2", "a1", "2026-01-01T10:00:02Z", "user", "second"),
            _entry("a2", "u2", "2026-01-01T10:00:03Z", "assistant", "answer"),
        ],
    )
    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-multi')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for uuid in ["u1", "a1"]:
        memory_db.execute(
            "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, ?, 'user', 'x', '')",
            (session_id, uuid),
        )
    _insert_import_log(memory_db, agent, 0)
    _insert_import_log(memory_db, parent, 2)

    status = summarize_ingestion(memory_db)

    assert status["pending_tail_sessions"] == 1
    assert status["pending_tail_turns"] == 2
    assert status["ingestion_gap_sessions"] == 0


def test_multifile_equal_timestamp_prefers_deeper_agent_leaf(memory_db, tmp_path):
    parent = tmp_path / "sess-equal.jsonl"
    agent = tmp_path / "agent-sess-equal.jsonl"
    shared_ts = "2026-01-01T10:00:01Z"
    _write_jsonl(
        parent,
        [
            _entry("u1", None, "2026-01-01T10:00:00Z", "user", "first"),
            _entry("a1", "u1", shared_ts, "assistant", "answer"),
        ],
    )
    _write_jsonl(agent, [_entry("a2", "a1", shared_ts, "assistant", "agent follow-up")])
    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-equal')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for uuid in ["u1", "a1"]:
        memory_db.execute(
            "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, ?, 'user', 'x', '')",
            (session_id, uuid),
        )
    _insert_import_log(memory_db, agent, 1)
    _insert_import_log(memory_db, parent, 2)

    status = summarize_ingestion(memory_db)

    assert status["pending_tail_sessions"] == 1
    assert status["pending_tail_turns"] == 1
    assert status["ok_sessions"] == 0


def test_import_log_only_session_is_not_counted(memory_db, tmp_path):
    filepath = tmp_path / "filtered-away.jsonl"
    _insert_import_log(memory_db, filepath, 0)

    status = summarize_ingestion(memory_db)

    assert status["sessions_checked"] == 0
    assert status["missing_source_sessions"] == 0


def test_partial_multifile_source_loss_counts_as_missing_source(memory_db, tmp_path):
    parent = tmp_path / "sess-partial.jsonl"
    missing_agent = tmp_path / "agent-sess-partial.jsonl"
    _write_jsonl(parent, [_entry("u1", None, "2026-01-01T10:00:00Z", "user", "first")])
    _seed_session(memory_db, parent, ["u1"])
    _insert_import_log(memory_db, missing_agent, 0)

    status = summarize_ingestion(memory_db)

    assert status["sessions_checked"] == 1
    assert status["missing_source_sessions"] == 1
    assert status["ok_sessions"] == 0


def test_partial_multifile_source_loss_stays_missing_source_after_prior_ok_cache(memory_db, tmp_path):
    parent = tmp_path / "sess-partial-cache.jsonl"
    agent = tmp_path / "agent-sess-partial-cache.jsonl"
    _write_jsonl(
        parent,
        [
            _entry("u1", None, "2026-01-01T10:00:00Z", "user", "first"),
            _entry("a1", "u1", "2026-01-01T10:00:01Z", "assistant", "answer"),
        ],
    )
    _write_jsonl(
        agent,
        [
            _entry("u2", "a1", "2026-01-01T10:00:02Z", "user", "second"),
            _entry("a2", "u2", "2026-01-01T10:00:03Z", "assistant", "answer"),
        ],
    )
    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-partial-cache')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for uuid in ["u1", "a1", "u2", "a2"]:
        memory_db.execute(
            "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, ?, 'user', 'x', '')",
            (session_id, uuid),
        )
    _insert_import_log(memory_db, parent, 2)
    _insert_import_log(memory_db, agent, 2)

    first = summarize_ingestion(memory_db)

    assert first["ok_sessions"] == 1
    assert _cache_row(memory_db, "sess-partial-cache") is not None

    agent.unlink()

    with patch(
        "ccrecall.ingestion_status.parse_all_with_uuids",
        side_effect=AssertionError("missing source should bypass cache and parsing"),
    ):
        second = summarize_ingestion(memory_db)

    assert second["sessions_checked"] == 1
    assert second["missing_source_sessions"] == 1
    assert second["ok_sessions"] == 0
