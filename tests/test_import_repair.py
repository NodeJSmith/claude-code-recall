"""Tests for hooks/import_repair.py's repair_sessions() execution loop."""

import logging
import os
import time
from pathlib import Path

import pytest
from conftest import make_jsonl_entry as _entry
from conftest import write_jsonl as _write_jsonl

from ccrecall import ingestion_status
from ccrecall.hooks import import_conversations, import_repair
from ccrecall.hooks.import_repair import repair_sessions
from ccrecall.import_log_ops import import_log_source_index
from ccrecall.ingestion_status import find_repairable_sessions


@pytest.fixture
def project_id(memory_db):
    cursor = memory_db.cursor()
    cursor.execute(
        "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
        ("/test/project", "-test-project", "test_project"),
    )
    memory_db.commit()
    return cursor.lastrowid


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


def _age_past_grace_window(filepath: Path) -> None:
    old = time.time() - 3600
    os.utime(filepath, (old, old))


def _stale_tail_candidates(memory_db) -> list:
    cursor = memory_db.cursor()
    sources = import_log_source_index(cursor)
    return find_repairable_sessions(memory_db, sources=sources)


def _delete_message(memory_db, session_id: int, uuid: str) -> None:
    """Delete one messages row, clearing its branch_messages FK references first."""
    memory_db.execute(
        "DELETE FROM branch_messages WHERE message_id IN (SELECT id FROM messages WHERE session_id = ? AND uuid = ?)",
        (session_id, uuid),
    )
    memory_db.execute("DELETE FROM messages WHERE session_id = ? AND uuid = ?", (session_id, uuid))


def test_repair_stale_tail_session_recovers_missing_message(memory_db, project_id, tmp_path):
    filepath = tmp_path / "sess-stale.jsonl"
    _write_four_turns(filepath)

    import_conversations.import_session(memory_db, filepath, project_id)
    memory_db.commit()

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-stale",)).fetchone()[0]
    _delete_message(memory_db, session_id, "a2")
    memory_db.commit()
    _age_past_grace_window(filepath)

    candidates = _stale_tail_candidates(memory_db)
    assert [c[0] for c in candidates] == ["sess-stale"]

    result = repair_sessions(memory_db, candidates)

    assert result == (1, 1, 0, 0)
    assert (
        memory_db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND uuid = 'a2'", (session_id,)
        ).fetchone()[0]
        == 1
    )


def test_repair_rerun_on_already_fixed_session_is_idempotent(memory_db, project_id, tmp_path):
    filepath = tmp_path / "sess-stale.jsonl"
    _write_four_turns(filepath)

    import_conversations.import_session(memory_db, filepath, project_id)
    memory_db.commit()

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-stale",)).fetchone()[0]
    _delete_message(memory_db, session_id, "a2")
    memory_db.commit()
    _age_past_grace_window(filepath)

    candidates = _stale_tail_candidates(memory_db)
    first = repair_sessions(memory_db, candidates)
    assert first == (1, 1, 0, 0)

    before_count = memory_db.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]

    # Re-run with the *same* candidate list (as returned before the repair) —
    # the session is already "ok" now, so a fresh find_repairable_sessions()
    # call would return nothing; passing the stale list mirrors a caller that
    # computed candidates once and repairs are attempted from that snapshot.
    second = repair_sessions(memory_db, candidates)

    assert second == (1, 0, 0, 0)
    after_count = memory_db.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]
    assert after_count == before_count, "idempotent re-run must not insert duplicate rows"


def test_unrepairable_candidate_is_not_counted_as_repaired(memory_db, project_id, tmp_path, monkeypatch):
    """A candidate whose source transcript genuinely lacks the expected content
    still classifies as stale_tail/ingestion_gap after a full force-reimport.

    classify_sessions' expected-message heuristic (message_content_parts, via
    _entry_expects_message) and the real per-entry import filter share the
    same function, so a content-level divergence between "classify expects a
    message" and "the importer actually inserts one" cannot be constructed
    from JSONL content alone within this module's tests — ingestion_status's
    own test suite owns verifying that shared classification logic. What this
    task (import_repair.py's execution loop) owns is: given that
    reclassify_session reports the candidate is still stale_tail/ingestion_gap
    after a real, non-raising force-reimport, repair_sessions() must report it
    as unrepairable rather than folding it into "repaired". Mocking
    reclassify_session's return value isolates exactly that handling logic.
    """
    filepath = tmp_path / "sess-unrepairable.jsonl"
    _write_four_turns(filepath)

    import_conversations.import_session(memory_db, filepath, project_id)
    memory_db.commit()

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-unrepairable",)).fetchone()[0]
    _delete_message(memory_db, session_id, "a2")
    memory_db.commit()
    _age_past_grace_window(filepath)

    candidates = _stale_tail_candidates(memory_db)
    assert [c[0] for c in candidates] == ["sess-unrepairable"]

    monkeypatch.setattr(import_repair.ingestion_status, "reclassify_session", lambda *a, **k: "stale_tail")

    result = repair_sessions(memory_db, candidates)

    assert result == (0, 1, 0, 1)


def test_poison_file_candidate_is_counted_failed_and_batch_continues(
    memory_db, project_id, tmp_path, monkeypatch, caplog
):
    poison_path = tmp_path / "sess-poison.jsonl"
    good_path = tmp_path / "sess-good.jsonl"
    _write_four_turns(poison_path)
    _write_four_turns(good_path)

    for filepath, uuid in ((poison_path, "sess-poison"), (good_path, "sess-good")):
        import_conversations.import_session(memory_db, filepath, project_id)
        memory_db.commit()
        session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", (uuid,)).fetchone()[0]
        _delete_message(memory_db, session_id, "a2")
        memory_db.commit()
        _age_past_grace_window(filepath)

    candidates = _stale_tail_candidates(memory_db)
    assert {c[0] for c in candidates} == {"sess-poison", "sess-good"}

    real_import_session = import_conversations.import_session
    real_reclassify = ingestion_status.reclassify_session
    reclassify_calls: list[str] = []

    def _raise_on_poison(conn, filepath, project_id, *, force=False):
        if filepath == poison_path:
            raise RuntimeError("simulated poison transcript")
        return real_import_session(conn, filepath, project_id, force=force)

    def _spy_reclassify(conn, session_uuid, filepaths, **kwargs):
        reclassify_calls.append(session_uuid)
        return real_reclassify(conn, session_uuid, filepaths, **kwargs)

    monkeypatch.setattr(import_repair.import_conversations, "import_session", _raise_on_poison)
    monkeypatch.setattr(import_repair.ingestion_status, "reclassify_session", _spy_reclassify)

    with caplog.at_level(logging.ERROR, logger="ccrecall"):
        result = repair_sessions(memory_db, candidates)

    sessions_repaired, messages_recovered, sessions_failed, sessions_unrepairable = result
    assert sessions_failed == 1
    assert sessions_repaired == 1
    assert sessions_unrepairable == 0
    assert messages_recovered == 1, "only the good candidate's file recovered a message"
    assert reclassify_calls == ["sess-good"], "reclassify_session must not run for the poison candidate"
    assert any(record.exc_info for record in caplog.records if record.levelno >= logging.ERROR)


def test_multifile_candidate_partial_failure_counts_recovered_and_failed(memory_db, project_id, tmp_path, monkeypatch):
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

    import_conversations.import_session(memory_db, parent, project_id)
    memory_db.commit()
    import_conversations.import_session(memory_db, agent, project_id)
    memory_db.commit()

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-multi",)).fetchone()[0]
    _delete_message(memory_db, session_id, "a1")
    _delete_message(memory_db, session_id, "a2")
    memory_db.commit()
    _age_past_grace_window(parent)
    _age_past_grace_window(agent)

    candidates = _stale_tail_candidates(memory_db)
    assert [c[0] for c in candidates] == ["sess-multi"]

    real_import_session = import_conversations.import_session

    def _raise_on_agent(conn, filepath, project_id, *, force=False):
        if filepath == agent:
            raise RuntimeError("simulated poison transcript")
        return real_import_session(conn, filepath, project_id, force=force)

    monkeypatch.setattr(import_repair.import_conversations, "import_session", _raise_on_agent)

    result = repair_sessions(memory_db, candidates)

    sessions_repaired, messages_recovered, sessions_failed, sessions_unrepairable = result
    assert sessions_failed == 1
    assert sessions_repaired == 0
    assert sessions_unrepairable == 0
    assert messages_recovered >= 1, "the successful parent file's recovered message must still be counted"
