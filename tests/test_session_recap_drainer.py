import ast
import io
import json
import sqlite3
import threading
from unittest.mock import patch

import pytest

import ccrecall.recap_input as recap_input
from ccrecall.hooks import drain_session_recaps, session_end
from ccrecall.hooks.durability import journal_lock
from ccrecall.llm_summarizer import STATUS_INVALID_OUTPUT, InvocationResult, invoke_claude
from ccrecall.llm_summary_db import get_connection
from ccrecall.recap_input import load_recap_input
from ccrecall.recap_state import create_run, upsert_job

UUID = "12345678-1234-1234-1234-123456789abc"


def _settings(tmp_path):
    return {
        "db_path": str(tmp_path / "recaps.db"),
        "llm_summaries_enabled": True,
        "llm_summary_model": "sonnet",
        "llm_summary_effort": "medium",
        "llm_summary_timeout_seconds": 10,
        "llm_summary_max_budget_usd": 1.0,
        "recap_job_lease_seconds": 60,
        "recap_runtime_lease_seconds": 60,
        "recap_cooldown_max_seconds": 3600,
        "recap_quarantine_max_count": 10,
        "recap_quarantine_max_bytes": 100000,
        "logging_enabled": False,
    }


def test_session_end_falls_back_without_migrating_and_emits_exact_empty_object(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(session_end, "load_settings", lambda: settings)
    spawned = []
    monkeypatch.setattr(session_end, "_spawn_drainer", lambda: spawned.append(True))
    output = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps({"session_id": UUID}))), patch("sys.stdout", output):
        session_end.main()
    assert output.getvalue().strip() == "{}"
    assert session_end.journal_path(tmp_path / "recaps.db", UUID).exists()
    assert spawned == [True]
    assert not (tmp_path / "recaps.db").exists()


def _run_session_end(payload, settings):
    output = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", output):
        session_end.main()
    assert output.getvalue().strip() == "{}"


def _insert_recap_session(settings):
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute(
            "INSERT INTO branches(session_id, leaf_uuid, is_active, recap_input_hash) VALUES (1, 'leaf', 1, 'input')"
        )


def test_session_end_records_committed_intent_when_db_is_ready(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _insert_recap_session(settings)
    monkeypatch.setattr(session_end, "load_settings", lambda: settings)
    monkeypatch.setattr(session_end, "posix_process_groups_supported", lambda: True)
    spawned = []
    monkeypatch.setattr(session_end, "_spawn_drainer", lambda: spawned.append(True))
    _run_session_end({"session_id": UUID}, settings)
    with get_connection(settings) as conn:
        assert conn.execute("SELECT requested_input_hash, trigger, state FROM session_recap_jobs").fetchone() == (
            "input",
            "session_end",
            "pending",
        )
    assert not session_end.journal_path(tmp_path / "recaps.db", UUID).exists()
    assert spawned == [True]


def test_session_end_journals_when_db_is_busy(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _insert_recap_session(settings)
    monkeypatch.setattr(session_end, "load_settings", lambda: settings)
    monkeypatch.setattr(session_end, "posix_process_groups_supported", lambda: True)
    spawned = []
    monkeypatch.setattr(session_end, "_spawn_drainer", lambda: spawned.append(True))
    lock = sqlite3.connect(settings["db_path"])
    lock.execute("BEGIN EXCLUSIVE")
    try:
        _run_session_end({"session_id": UUID}, settings)
    finally:
        lock.rollback()
        lock.close()
    assert session_end.journal_path(tmp_path / "recaps.db", UUID).exists()
    assert spawned == [True]


def test_session_end_invalid_uuid_writes_neither_intent_nor_journal(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _insert_recap_session(settings)
    monkeypatch.setattr(session_end, "load_settings", lambda: settings)
    monkeypatch.setattr(session_end, "_spawn_drainer", lambda: (_ for _ in ()).throw(AssertionError()))
    _run_session_end({"session_id": "not-a-uuid"}, settings)
    with get_connection(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_recap_jobs").fetchone() == (0,)
    assert not list(tmp_path.glob(f"{session_end.JOURNAL_PREFIX}*"))


def test_session_end_unsupported_platform_records_blocked_intent_without_spawn(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _insert_recap_session(settings)
    monkeypatch.setattr(session_end, "load_settings", lambda: settings)
    monkeypatch.setattr(session_end, "posix_process_groups_supported", lambda: False)
    monkeypatch.setattr(session_end, "_spawn_drainer", lambda: (_ for _ in ()).throw(AssertionError()))
    _run_session_end({"session_id": UUID}, settings)
    with get_connection(settings) as conn:
        assert conn.execute("SELECT state, reason FROM session_recap_jobs").fetchone() == (
            "blocked",
            "platform_unsupported",
        )
    assert not session_end.journal_path(tmp_path / "recaps.db", UUID).exists()


def test_fallback_journal_concurrent_writers_keep_newest_request(tmp_path):
    database = tmp_path / "recaps.db"
    requested_at = [f"2026-08-12T10:00:{second:02d}Z" for second in range(20)]
    start = threading.Barrier(len(requested_at))
    threads = [
        threading.Thread(
            target=lambda value=value: (start.wait(), session_end.write_fallback_journal(database, UUID, value))
        )
        for value in requested_at
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert json.loads(session_end.journal_path(database, UUID).read_text())["requested_at"] == max(requested_at)


def test_fallback_journal_staged_stale_writer_cannot_regress_marker(tmp_path, monkeypatch):
    database = tmp_path / "recaps.db"
    older = "2026-08-12T10:00:00Z"
    newer = "2026-08-12T10:01:00Z"
    entered = threading.Event()
    release = threading.Event()
    original_read_text = type(session_end.journal_path(database, UUID)).read_text

    def paused_read_text(path, *args, **kwargs):
        if threading.current_thread().name == "older":
            entered.set()
            assert release.wait(2)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.read_text", paused_read_text)
    old_writer = threading.Thread(target=session_end.write_fallback_journal, args=(database, UUID, older), name="older")
    old_writer.start()
    assert entered.wait(2)
    new_writer = threading.Thread(target=session_end.write_fallback_journal, args=(database, UUID, newer))
    new_writer.start()
    release.set()
    old_writer.join(2)
    new_writer.join(2)
    assert not old_writer.is_alive()
    assert not new_writer.is_alive()
    assert json.loads(session_end.journal_path(database, UUID).read_text())["requested_at"] == newer


def test_journal_replay_upserts_before_deleting_marker(tmp_path):
    settings = _settings(tmp_path)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
    session_end.write_fallback_journal(tmp_path / "recaps.db", UUID, "2026-08-12T10:00:00Z")
    assert drain_session_recaps.replay_journal(settings) == 1
    assert not session_end.journal_path(tmp_path / "recaps.db", UUID).exists()
    with get_connection(settings) as conn:
        assert conn.execute("SELECT trigger FROM session_recap_jobs").fetchone() == ("session_end",)


def test_journal_replay_syncs_directory_only_after_successful_marker_removal(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
    session_end.write_fallback_journal(tmp_path / "recaps.db", UUID, "2026-08-12T10:00:00Z")
    marker = session_end.journal_path(tmp_path / "recaps.db", UUID)
    synced = []

    def fsync_directory(path):
        assert path == marker.parent
        assert not marker.exists()
        synced.append(path)

    monkeypatch.setattr(drain_session_recaps, "fsync_directory", fsync_directory)
    assert drain_session_recaps.replay_journal(settings) == 1
    assert synced == [marker.parent]

    session_end.write_fallback_journal(tmp_path / "recaps.db", UUID, "2026-08-12T10:00:00Z")
    monkeypatch.setattr(
        drain_session_recaps,
        "upsert_job",
        lambda *_args: (_ for _ in ()).throw(OSError("replay failed")),
    )
    assert drain_session_recaps.replay_journal(settings) == 0
    assert synced == [marker.parent]


def test_replay_holds_marker_lock_until_newer_writer_can_replace_and_converge(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    database = tmp_path / "recaps.db"
    older = "2026-08-12T10:00:00Z"
    newer = "2026-08-12T10:01:00Z"
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
    session_end.write_fallback_journal(database, UUID, older)
    replay_read = threading.Event()
    release_replay = threading.Event()
    writer_started = threading.Event()
    marker = session_end.journal_path(database, UUID)
    original_read_text = type(marker).read_text

    def paused_read_text(path, *args, **kwargs):
        if threading.current_thread().name == "replay":
            replay_read.set()
            assert release_replay.wait(2)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.read_text", paused_read_text)
    replay = threading.Thread(target=drain_session_recaps.replay_journal, args=(settings,), name="replay")
    replay.start()
    assert replay_read.wait(2)
    writer = threading.Thread(
        target=lambda: (writer_started.set(), session_end.write_fallback_journal(database, UUID, newer)), name="writer"
    )
    writer.start()
    assert writer_started.wait(2)
    release_replay.set()
    replay.join(2)
    writer.join(2)
    assert not replay.is_alive()
    assert not writer.is_alive()
    assert json.loads(marker.read_text())["requested_at"] == newer
    assert drain_session_recaps.replay_journal(settings) == 1
    assert not marker.exists()


def test_journal_lock_releases_after_replay_error(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    marker = session_end.journal_path(tmp_path / "recaps.db", UUID)
    session_end.write_fallback_journal(tmp_path / "recaps.db", UUID, "2026-08-12T10:00:00Z")
    monkeypatch.setattr(
        drain_session_recaps,
        "sync_session_for_finalization",
        lambda *_args: (_ for _ in ()).throw(OSError("sync failed")),
    )
    assert drain_session_recaps.replay_journal(settings) == 0
    acquired = threading.Event()

    def acquire() -> None:
        with journal_lock(marker):
            acquired.set()

    thread = threading.Thread(target=acquire)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert acquired.is_set()


@pytest.mark.parametrize("failure", ["file", "directory"])
def test_fallback_writer_propagates_fsync_failure(tmp_path, monkeypatch, failure):
    database = tmp_path / "recaps.db"
    if failure == "file":
        monkeypatch.setattr(session_end.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("file fsync")))
    else:
        monkeypatch.setattr(
            session_end, "fsync_directory", lambda _path: (_ for _ in ()).throw(OSError("directory fsync"))
        )
    with pytest.raises(OSError, match=f"{failure} fsync"):
        session_end.write_fallback_journal(database, UUID, "2026-08-12T10:00:00Z")


def test_session_end_logs_fallback_fsync_failure_and_keeps_hook_stdout(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(session_end, "load_settings", lambda: settings)
    monkeypatch.setattr(session_end, "fsync_directory", lambda _path: (_ for _ in ()).throw(OSError("fsync")))
    errors = []
    monkeypatch.setattr(session_end, "log_hook_exception", lambda name: errors.append(name))
    output = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps({"session_id": UUID}))), patch("sys.stdout", output):
        session_end.main()
    assert output.getvalue().strip() == "{}"
    assert errors == ["session-end"]


def test_replay_quarantines_malformed_marker(tmp_path):
    settings = _settings(tmp_path)
    marker = tmp_path / f"{session_end.JOURNAL_PREFIX}{UUID}.json"
    marker.write_text("not json")
    assert drain_session_recaps.replay_journal(settings) == 0
    assert marker.with_suffix(".json.bad").exists()


def test_replay_quarantines_marker_missing_contract_keys(tmp_path):
    settings = _settings(tmp_path)
    marker = tmp_path / f"{session_end.JOURNAL_PREFIX}{UUID}.json"
    marker.write_text(json.dumps({"version": 1, "session_uuid": UUID}))
    assert drain_session_recaps.replay_journal(settings) == 0
    assert marker.with_suffix(".json.bad").exists()


def test_replay_malformed_quarantine_cannot_consume_concurrent_valid_replacement(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    database = tmp_path / "recaps.db"
    marker = session_end.journal_path(database, UUID)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
    marker.write_text("not json")
    replay_read = threading.Event()
    release_replay = threading.Event()
    writer_started = threading.Event()
    original_read_text = type(marker).read_text

    def paused_read_text(path, *args, **kwargs):
        if threading.current_thread().name == "replay":
            replay_read.set()
            assert release_replay.wait(2)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.read_text", paused_read_text)
    replay = threading.Thread(target=drain_session_recaps.replay_journal, args=(settings,), name="replay")
    replay.start()
    assert replay_read.wait(2)
    writer = threading.Thread(
        target=lambda: (
            writer_started.set(),
            session_end.write_fallback_journal(database, UUID, "2026-08-12T10:01:00Z"),
        ),
        name="writer",
    )
    writer.start()
    assert writer_started.wait(2)
    release_replay.set()
    replay.join(2)
    writer.join(2)
    assert not replay.is_alive()
    assert not writer.is_alive()
    assert marker.with_suffix(".json.bad").exists()
    assert json.loads(marker.read_text())["requested_at"] == "2026-08-12T10:01:00Z"
    assert drain_session_recaps.replay_journal(settings) == 1
    assert not marker.exists()


def test_session_end_journals_valid_unknown_session_for_stop_race(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with get_connection(settings):
        pass
    monkeypatch.setattr(session_end, "load_settings", lambda: settings)
    spawned = []
    monkeypatch.setattr(session_end, "_spawn_drainer", lambda: spawned.append(True))
    output = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps({"session_id": UUID}))), patch("sys.stdout", output):
        session_end.main()
    assert output.getvalue().strip() == "{}"
    assert session_end.journal_path(tmp_path / "recaps.db", UUID).exists()
    assert spawned == [True]


def test_replay_final_sync_captures_valid_unknown_session(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    session_end.write_fallback_journal(tmp_path / "recaps.db", UUID, "2026-08-12T10:00:00Z")

    def final_sync(_settings, session_uuid):
        assert session_uuid == UUID
        with get_connection(settings) as conn:
            conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
            conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
            conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
        return 1

    monkeypatch.setattr(drain_session_recaps, "sync_session_for_finalization", final_sync)
    assert drain_session_recaps.replay_journal(settings) == 1
    assert not session_end.journal_path(tmp_path / "recaps.db", UUID).exists()
    with get_connection(settings) as conn:
        assert conn.execute("SELECT state FROM session_recap_jobs").fetchone() == ("pending",)


def test_session_end_source_stays_outside_provider_and_migration_boundaries():
    source = session_end.__file__
    with open(source, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert "ccrecall.llm_summarizer" not in modules
    assert "ccrecall.llm_summary_db" not in modules
    assert "ccrecall.recap_state" not in modules
    assert not any("embed" in name or "onnx" in name for name in imports)


def test_recovery_reclaims_expired_unlaunched_changed_input_attempt(tmp_path):
    settings = _settings(tmp_path)
    packet = tmp_path / "packet.json"
    packet.write_text("sensitive")
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
        upsert_job(conn, 1, "input", "session_end", "2026-08-12T10:00:00Z")
        token = conn.execute(
            "UPDATE session_recap_jobs SET state = 'claimed', claim_token = 1, lease_expires_at = '2000-01-01T00:00:00Z' "
            "WHERE session_id = 1 RETURNING claim_token"
        ).fetchone()[0]
        attempt = conn.execute(
            "INSERT INTO session_recap_attempts (session_id, job_session_id, input_hash, input_contract_version, "
            "policy_version, recap_contract_version, claim_token, trigger, state, created_at, packet_path) "
            "VALUES (1, 1, 'older-input', 1, 1, 2, ?, 'session_end', 'reserved', '2026-08-12T10:00:00Z', ?) RETURNING id",
            (token, str(packet)),
        ).fetchone()[0]
        conn.execute("UPDATE session_recap_jobs SET active_attempt_id = ? WHERE session_id = 1", (attempt,))
    proven, recovered = drain_session_recaps._recover_expired_claims(settings, "2026-08-12T10:01:00Z")
    assert proven
    assert recovered == {1}
    assert not packet.exists()
    with get_connection(settings) as conn:
        assert conn.execute("SELECT state, cleanup_state FROM session_recap_attempts").fetchone() == (
            "abandoned",
            "verified_removed",
        )
        assert conn.execute("SELECT state, active_attempt_id FROM session_recap_jobs").fetchone() == ("pending", None)


def test_recovery_run_drains_requeued_same_generation_attempt(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    packet = tmp_path / "packet.json"
    packet.write_text("sensitive")
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
        recap = load_recap_input(conn.cursor(), 1)
        conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = 1", (recap.input_hash,))
        upsert_job(conn, 1, recap.input_hash, "session_end", "2026-08-12T10:00:00Z")
        conn.execute(
            "UPDATE session_recap_jobs SET state = 'claimed', claim_token = 1, lease_expires_at = '2000-01-01T00:00:00Z'"
        )
        attempt = conn.execute(
            "INSERT INTO session_recap_attempts (session_id, job_session_id, input_hash, input_contract_version, "
            "policy_version, recap_contract_version, claim_token, trigger, state, created_at, packet_path) "
            "VALUES (1, 1, ?, 1, 1, 2, 1, 'session_end', 'reserved', '2026-08-12T10:00:00Z', ?) RETURNING id",
            (recap.input_hash, str(packet)),
        ).fetchone()[0]
        conn.execute("UPDATE session_recap_jobs SET active_attempt_id = ?", (attempt,))
    monkeypatch.setattr(drain_session_recaps, "load_settings", lambda: settings)
    monkeypatch.setattr(drain_session_recaps, "try_acquire_pid_file", lambda _key: True)
    monkeypatch.setattr(drain_session_recaps, "remove_pid_file", lambda _key: None)
    monkeypatch.setattr(drain_session_recaps, "sync_session_for_finalization", lambda *_args: 0)
    assert drain_session_recaps.run() == 0
    with get_connection(settings) as conn:
        assert conn.execute("SELECT state FROM session_recap_jobs").fetchone() == ("excluded",)


def test_recovery_identity_mismatch_blocks_and_quarantines(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
        upsert_job(conn, 1, "input", "session_end", "2026-08-12T10:00:00Z")
        conn.execute(
            "UPDATE session_recap_jobs SET state = 'claimed', claim_token = 1, lease_expires_at = '2000-01-01T00:00:00Z'"
        )
        attempt = conn.execute(
            "INSERT INTO session_recap_attempts (session_id, job_session_id, input_hash, input_contract_version, "
            "policy_version, recap_contract_version, claim_token, trigger, state, created_at, packet_path, packet_nonce, "
            "owner_pid, process_group_id, process_started_at) VALUES (1, 1, 'input', 1, 1, 2, 1, 'session_end', "
            "'running', '2026-08-12T10:00:00Z', '/owner/packet', 'nonce', 99, 99, 'old') RETURNING id"
        ).fetchone()[0]
        conn.execute("UPDATE session_recap_jobs SET active_attempt_id = ?", (attempt,))
    monkeypatch.setattr(drain_session_recaps, "process_group_absent", lambda _group: False)
    monkeypatch.setattr(drain_session_recaps, "_terminate_exact_group", lambda *_args: False)
    proven, recovered = drain_session_recaps._recover_expired_claims(settings, "2026-08-12T10:01:00Z")
    assert not proven
    assert not recovered
    with get_connection(settings) as conn:
        assert conn.execute("SELECT state, reason FROM session_recap_jobs").fetchone() == ("blocked", "cleanup_failed")
        assert conn.execute("SELECT state, cleanup_state FROM session_recap_attempts").fetchone() == (
            "cleanup_failed",
            "uncertain",
        )
        assert conn.execute("SELECT cleanup_state FROM session_recap_quarantine").fetchone() == ("uncertain",)


def test_concurrent_drainers_admit_one_provider_invocation(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        for session_id, session_uuid in enumerate((UUID, "87654321-4321-4321-4321-cba987654321"), 1):
            conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (session_uuid,))
            conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (?, 'leaf', 1)", (session_id,))
            recap = load_recap_input(conn.cursor(), session_id)
            conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = ?", (recap.input_hash, session_id))
            upsert_job(conn, session_id, recap.input_hash, "session_end", "2026-08-12T10:00:00Z")
    monkeypatch.setattr(drain_session_recaps, "sync_session_for_finalization", lambda *_args: 0)
    monkeypatch.setattr(
        drain_session_recaps, "evaluate_branch", lambda *_args: type("Decision", (), {"eligible": True})()
    )
    monkeypatch.setattr(drain_session_recaps, "posix_process_groups_supported", lambda: True)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def invoke(*_args, **_kwargs):
        calls.append(True)
        started.set()
        assert release.wait(2)
        return InvocationResult(STATUS_INVALID_OUTPUT)

    monkeypatch.setattr(drain_session_recaps, "invoke_claude", invoke)
    first = threading.Thread(target=drain_session_recaps._process_job, args=(settings, 1, 1))
    first.start()
    assert started.wait(2)
    assert drain_session_recaps._process_job(settings, 2, 2)
    release.set()
    first.join(2)
    assert not first.is_alive()
    assert calls == [True]


def test_drainer_initial_admission_denial_cancels_attempt_and_releases_provider(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings["recap_quarantine_max_count"] = 0
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
        recap = load_recap_input(conn.cursor(), 1)
        conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = 1", (recap.input_hash,))
        upsert_job(conn, 1, recap.input_hash, "session_end", "2026-08-12T10:00:00Z")
    monkeypatch.setattr(drain_session_recaps, "sync_session_for_finalization", lambda *_args: 0)
    monkeypatch.setattr(
        drain_session_recaps, "evaluate_branch", lambda *_args: type("Decision", (), {"eligible": True})()
    )
    monkeypatch.setattr(drain_session_recaps, "posix_process_groups_supported", lambda: True)
    assert drain_session_recaps._process_job(settings, 1, 1)
    with get_connection(settings) as conn:
        assert conn.execute("SELECT state, active_attempt_id FROM session_recap_jobs").fetchone() == ("pending", None)
        assert conn.execute("SELECT state FROM session_recap_attempts").fetchone() == ("cancelled_before_launch",)
        assert conn.execute("SELECT probe_active FROM session_recap_provider_health").fetchone() == (0,)


@pytest.mark.parametrize("error", [FileNotFoundError(), OSError()])
def test_drainer_spawn_failure_cleans_reservation_and_opens_cooldown(tmp_path, monkeypatch, error):
    settings = _settings(tmp_path)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
        recap = load_recap_input(conn.cursor(), 1)
        conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = 1", (recap.input_hash,))
        upsert_job(conn, 1, recap.input_hash, "session_end", "2026-08-12T10:00:00Z")
    monkeypatch.setattr(drain_session_recaps, "sync_session_for_finalization", lambda *_args: 0)
    monkeypatch.setattr(
        drain_session_recaps, "evaluate_branch", lambda *_args: type("Decision", (), {"eligible": True})()
    )
    monkeypatch.setattr(drain_session_recaps, "posix_process_groups_supported", lambda: True)
    monkeypatch.setattr(
        drain_session_recaps,
        "invoke_claude",
        lambda path, controls, **kwargs: invoke_claude(
            path, controls, popen=lambda *_args, **_kwargs: (_ for _ in ()).throw(error), **kwargs
        ),
    )
    assert not drain_session_recaps._process_job(settings, 1, 1)
    with get_connection(settings) as conn:
        assert conn.execute("SELECT state, active_attempt_id, reason FROM session_recap_jobs").fetchone() == (
            "pending",
            None,
            "global_abort",
        )
        assert conn.execute("SELECT state, cleanup_state FROM session_recap_attempts").fetchone() == (
            "global_abort",
            "verified_removed",
        )
        assert conn.execute(
            "SELECT probe_active, reason, diagnostic FROM session_recap_provider_health"
        ).fetchone() == (
            0,
            "claude_unavailable" if isinstance(error, FileNotFoundError) else "error",
            "provider_error",
        )


def test_drainer_spawn_cleanup_uncertainty_blocks_and_quarantines(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
        recap = load_recap_input(conn.cursor(), 1)
        conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = 1", (recap.input_hash,))
        upsert_job(conn, 1, recap.input_hash, "session_end", "2026-08-12T10:00:00Z")
    monkeypatch.setattr(drain_session_recaps, "sync_session_for_finalization", lambda *_args: 0)
    monkeypatch.setattr(
        drain_session_recaps, "evaluate_branch", lambda *_args: type("Decision", (), {"eligible": True})()
    )
    monkeypatch.setattr(drain_session_recaps, "posix_process_groups_supported", lambda: True)
    monkeypatch.setattr("ccrecall.llm_summarizer.remove_packet", lambda _path: False)
    monkeypatch.setattr(
        drain_session_recaps,
        "invoke_claude",
        lambda path, controls, **kwargs: invoke_claude(
            path, controls, popen=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()), **kwargs
        ),
    )
    assert not drain_session_recaps._process_job(settings, 1, 1)
    with get_connection(settings) as conn:
        assert conn.execute("SELECT state, reason FROM session_recap_jobs").fetchone() == ("blocked", "cleanup_failed")
        assert conn.execute("SELECT state, cleanup_state FROM session_recap_attempts").fetchone() == (
            "cleanup_failed",
            "uncertain",
        )
        assert conn.execute("SELECT cleanup_state FROM session_recap_quarantine").fetchone() == ("uncertain",)


def test_drainer_materialization_discards_each_stale_guard_without_replacing_recap(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    for change in ("branch", "token", "hash", "contract"):
        database = tmp_path / f"{change}.db"
        case_settings = {**settings, "db_path": str(database)}
        with get_connection(case_settings) as conn:
            conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
            conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
            conn.execute(
                "INSERT INTO branches(session_id, leaf_uuid, is_active, summary_enrichment_json) "
                "VALUES (1, 'leaf', 1, '{\"summary\":\"old recap\"}')"
            )
            recap = load_recap_input(conn.cursor(), 1)
            conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = 1", (recap.input_hash,))
            upsert_job(conn, 1, recap.input_hash, "session_end", "2026-08-12T10:00:00Z")
            conn.execute("UPDATE session_recap_jobs SET state = 'claimed', claim_token = 7")
            attempt = conn.execute(
                "INSERT INTO session_recap_attempts (session_id, job_session_id, input_hash, input_contract_version, "
                "policy_version, recap_contract_version, claim_token, trigger, state, created_at) "
                "VALUES (1, 1, ?, 1, 1, 2, 7, 'session_end', 'running', '2026-08-12T10:00:00Z') RETURNING id",
                (recap.input_hash,),
            ).fetchone()[0]
            conn.execute("UPDATE session_recap_jobs SET active_attempt_id = ?", (attempt,))
            if change == "branch":
                conn.execute("UPDATE branches SET is_active = 0 WHERE id = 1")
            elif change == "token":
                conn.execute("UPDATE session_recap_jobs SET claim_token = 8")
            elif change == "contract":
                monkeypatch.setattr(recap_input, "RECAP_INPUT_CONTRACT_VERSION", 99)
            else:
                conn.execute("UPDATE branches SET recap_input_hash = 'changed' WHERE id = 1")
        assert not drain_session_recaps._materialize(
            case_settings, 1, 1, 7, attempt, recap.input_hash, {"summary": "new recap"}, "sonnet"
        )
        with get_connection(case_settings) as conn:
            assert conn.execute("SELECT summary_enrichment_json FROM branches WHERE id = 1").fetchone() == (
                '{"summary":"old recap"}',
            )


def test_drainer_does_not_invoke_when_runtime_lease_is_contended(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
        upsert_job(conn, 1, None, "session_end", "2026-08-12T10:00:00Z")
        conn.execute(
            "INSERT INTO session_recap_runtime(singleton, owner_pid, lease_expires_at) VALUES (1, 99, '2999-01-01T00:00:00Z')"
        )
    monkeypatch.setattr(drain_session_recaps, "load_settings", lambda: settings)
    monkeypatch.setattr(drain_session_recaps, "try_acquire_pid_file", lambda _key: True)
    monkeypatch.setattr(drain_session_recaps, "remove_pid_file", lambda _key: None)
    monkeypatch.setattr(
        drain_session_recaps, "invoke_claude", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())
    )
    assert drain_session_recaps.run() == 0


def test_manual_controls_are_used_for_attempt_invocation_and_materialization(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
        recap = load_recap_input(conn.cursor(), 1)
        conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = 1", (recap.input_hash,))
        upsert_job(conn, 1, recap.input_hash, "manual", "2026-08-12T10:00:00Z")
    monkeypatch.setattr(drain_session_recaps, "sync_session_for_finalization", lambda *_args: 0)
    monkeypatch.setattr(
        drain_session_recaps, "evaluate_branch", lambda *_args: type("Decision", (), {"eligible": True})()
    )
    monkeypatch.setattr(drain_session_recaps, "posix_process_groups_supported", lambda: True)
    seen = []

    def invoke(_path, effective, **_kwargs):
        seen.append(effective.copy())
        return InvocationResult(STATUS_INVALID_OUTPUT)

    monkeypatch.setattr(drain_session_recaps, "invoke_claude", invoke)
    assert drain_session_recaps._process_job(
        settings, 1, 1, controls={"model": "opus", "max_budget_usd": 2.5, "timeout_seconds": 42}
    )
    assert seen[0]["llm_summary_model"] == "opus"
    assert seen[0]["llm_summary_max_budget_usd"] == 2.5
    assert seen[0]["llm_summary_timeout_seconds"] == 42
    assert settings["llm_summary_model"] == "sonnet"
    with get_connection(settings) as conn:
        assert conn.execute("SELECT model, max_budget_usd, timeout_seconds FROM session_recap_attempts").fetchone() == (
            "opus",
            2.5,
            42,
        )


def test_drainer_limit_never_falls_through_to_unowned_pending_work(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        candidates = []
        for session_id, session_uuid in enumerate((UUID, "87654321-4321-4321-4321-cba987654321"), 1):
            conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (session_uuid,))
            conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (?, 'leaf', 1)", (session_id,))
            recap = load_recap_input(conn.cursor(), session_id)
            conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = ?", (recap.input_hash, session_id))
            upsert_job(conn, session_id, recap.input_hash, "manual", "2026-08-12T10:00:00Z")
            candidates.append((session_id, recap.input_hash, "pending"))
        run_id = create_run(conn, "manual", "{}", candidates, "2026-08-12T10:00:00Z", attempt_limit=1)
    monkeypatch.setattr(drain_session_recaps, "load_settings", lambda: settings)
    monkeypatch.setattr(drain_session_recaps, "try_acquire_pid_file", lambda _key: True)
    monkeypatch.setattr(drain_session_recaps, "remove_pid_file", lambda _key: None)
    monkeypatch.setattr(drain_session_recaps, "sync_session_for_finalization", lambda *_args: 0)
    monkeypatch.setattr(
        drain_session_recaps, "evaluate_branch", lambda *_args: type("Decision", (), {"eligible": True})()
    )
    monkeypatch.setattr(drain_session_recaps, "posix_process_groups_supported", lambda: True)
    invocations = []
    monkeypatch.setattr(
        drain_session_recaps,
        "invoke_claude",
        lambda *_args, **_kwargs: (invocations.append(True), InvocationResult(STATUS_INVALID_OUTPUT))[1],
    )

    assert drain_session_recaps.run() == 0
    assert invocations == [True]
    with get_connection(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_recap_attempts").fetchone() == (1,)
        assert conn.execute(
            "SELECT final_disposition FROM session_recap_run_candidates WHERE run_id = ? ORDER BY session_id", (run_id,)
        ).fetchall() == [("attempted",), ("deferred_by_limit",)]


def test_drainer_terminalizes_changed_manual_snapshot_and_processes_current_generation(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
        original = load_recap_input(conn.cursor(), 1)
        conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = 1", (original.input_hash,))
        upsert_job(conn, 1, original.input_hash, "manual", "2026-08-12T10:00:00Z")
        run_id = create_run(
            conn, "manual", "{}", [(1, original.input_hash, "pending")], "2026-08-12T10:00:00Z", attempt_limit=None
        )
        conn.execute(
            "INSERT INTO messages(session_id, uuid, role, content) VALUES (1, 'changed', 'user', 'changed input')"
        )
        message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO branch_messages(branch_id, message_id, position) VALUES (1, ?, 0)", (message_id,))
        current = load_recap_input(conn.cursor(), 1)
        conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = 1", (current.input_hash,))
        upsert_job(conn, 1, current.input_hash, "session_end", "2026-08-12T10:01:00Z")
    monkeypatch.setattr(drain_session_recaps, "load_settings", lambda: settings)
    monkeypatch.setattr(drain_session_recaps, "try_acquire_pid_file", lambda _key: True)
    monkeypatch.setattr(drain_session_recaps, "remove_pid_file", lambda _key: None)
    monkeypatch.setattr(drain_session_recaps, "sync_session_for_finalization", lambda *_args: 0)
    monkeypatch.setattr(
        drain_session_recaps, "evaluate_branch", lambda *_args: type("Decision", (), {"eligible": True})()
    )
    monkeypatch.setattr(drain_session_recaps, "posix_process_groups_supported", lambda: True)
    invocations = []
    monkeypatch.setattr(
        drain_session_recaps,
        "invoke_claude",
        lambda *_args, **_kwargs: (invocations.append(True), InvocationResult(STATUS_INVALID_OUTPUT))[1],
    )

    assert drain_session_recaps.run() == 0
    assert invocations == [True]
    with get_connection(settings) as conn:
        assert conn.execute("SELECT state FROM session_recap_runs WHERE id = ?", (run_id,)).fetchone() == ("complete",)
        assert conn.execute(
            "SELECT final_disposition, started_attempt_id FROM session_recap_run_candidates WHERE run_id = ?", (run_id,)
        ).fetchone() == ("stale_input_changed", None)
        assert conn.execute("SELECT input_hash, trigger FROM session_recap_attempts").fetchone() == (
            current.input_hash,
            "session_end",
        )


def test_drainer_global_abort_only_marks_the_run_that_owned_the_failed_attempt(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        candidates = []
        for session_id, session_uuid in enumerate((UUID, "87654321-4321-4321-4321-cba987654321"), 1):
            conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (session_uuid,))
            conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (?, 'leaf', 1)", (session_id,))
            recap = load_recap_input(conn.cursor(), session_id)
            conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = ?", (recap.input_hash, session_id))
            upsert_job(conn, session_id, recap.input_hash, "manual", "2026-08-12T10:00:00Z")
            candidates.append((session_id, recap.input_hash, "pending"))
        failed_run = create_run(conn, "manual", "{}", [candidates[0]], "2026-08-12T10:00:00Z", attempt_limit=None)
        other_run = create_run(conn, "manual", "{}", [candidates[1]], "2026-08-12T10:00:00Z", attempt_limit=None)
    monkeypatch.setattr(drain_session_recaps, "load_settings", lambda: settings)
    monkeypatch.setattr(drain_session_recaps, "try_acquire_pid_file", lambda _key: True)
    monkeypatch.setattr(drain_session_recaps, "remove_pid_file", lambda _key: None)
    monkeypatch.setattr(drain_session_recaps, "_process_job", lambda *_args, **_kwargs: False)

    assert drain_session_recaps.run() == 0

    with get_connection(settings) as conn:
        assert conn.execute("SELECT state FROM session_recap_runs WHERE id = ?", (failed_run,)).fetchone() == (
            "incomplete",
        )
        assert conn.execute(
            "SELECT final_disposition FROM session_recap_run_candidates WHERE run_id = ?", (failed_run,)
        ).fetchone() == ("deferred_after_abort",)
        assert conn.execute("SELECT state FROM session_recap_runs WHERE id = ?", (other_run,)).fetchone() == (
            "complete",
        )
        assert conn.execute(
            "SELECT final_disposition FROM session_recap_run_candidates WHERE run_id = ?", (other_run,)
        ).fetchone() == ("deferred_by_limit",)


def test_drainer_does_not_finalize_run_created_after_empty_dequeue(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(drain_session_recaps, "load_settings", lambda: settings)
    monkeypatch.setattr(drain_session_recaps, "try_acquire_pid_file", lambda _key: True)
    monkeypatch.setattr(drain_session_recaps, "remove_pid_file", lambda _key: None)
    original_next_pending = drain_session_recaps._next_pending
    created_run = None

    def next_pending_then_create(conn, now):
        nonlocal created_run
        assert original_next_pending(conn, now) is None
        conn.execute("INSERT INTO projects(path, key) VALUES ('/p', 'p')")
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, 1)", (UUID,))
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (1, 'leaf', 1)")
        recap = load_recap_input(conn.cursor(), 1)
        conn.execute("UPDATE branches SET recap_input_hash = ? WHERE id = 1", (recap.input_hash,))
        upsert_job(conn, 1, recap.input_hash, "manual", now)
        created_run = create_run(conn, "manual", "{}", [(1, recap.input_hash, "pending")], now, attempt_limit=None)
        return

    monkeypatch.setattr(drain_session_recaps, "_next_pending", next_pending_then_create)

    assert drain_session_recaps.run() == 0

    with get_connection(settings) as conn:
        assert conn.execute("SELECT state FROM session_recap_runs WHERE id = ?", (created_run,)).fetchone() == (
            "running",
        )
        assert conn.execute(
            "SELECT final_disposition FROM session_recap_run_candidates WHERE run_id = ?", (created_run,)
        ).fetchone() == (None,)
        assert conn.execute("SELECT state FROM session_recap_jobs WHERE session_id = 1").fetchone() == ("pending",)
