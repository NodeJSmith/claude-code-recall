"""How `ccrecall import` behaves when another process is using the database (#233)."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import make_jsonl_entry, seed_stale_tail_session, write_jsonl

from ccrecall.config import PID_KEY_IMPORT, pid_file_path, remove_pid_file
from ccrecall.db import get_connection
from ccrecall.hooks import import_conversations, import_repair
from ccrecall.hooks.import_conversations import import_project_contained, run
from ccrecall.hooks.import_repair import repair_sessions
from ccrecall.import_log_ops import import_log_source_index
from ccrecall.ingestion_status import find_repairable_sessions

PROJECT_KEY = "-Users-sam-project"


def write_session(project_dir: Path, name: str) -> Path:
    filepath = project_dir / f"{name}.jsonl"
    write_jsonl(
        filepath,
        [
            make_jsonl_entry(f"{name}-u1", None, "2026-01-01T10:00:00Z", "user", "first question"),
            make_jsonl_entry(f"{name}-a1", f"{name}-u1", "2026-01-01T10:00:01Z", "assistant", "first answer"),
        ],
    )
    return filepath


def locked_error() -> sqlite3.OperationalError:
    """What sqlite3 raises for SQLITE_BUSY: a message plus the numeric code."""
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


@pytest.fixture
def db_conn(tmp_path) -> Iterator[sqlite3.Connection]:
    """A real on-disk WAL database, since locking can't be reproduced on :memory:.

    No busy_timeout, so a test that hits a lock fails at once instead of waiting.
    """
    with get_connection({"db_path": str(tmp_path / "memory.db")}) as conn:
        conn.execute("PRAGMA busy_timeout = 0")
        yield conn


@pytest.fixture
def competitor(tmp_path, db_conn) -> Iterator[sqlite3.Connection]:
    """A second connection, standing in for another process's writes."""
    conn = sqlite3.connect(tmp_path / "memory.db", isolation_level=None, timeout=0)
    yield conn
    conn.close()


@pytest.fixture
def project_dir(tmp_path) -> Path:
    path = tmp_path / "projects" / PROJECT_KEY
    path.mkdir(parents=True)
    return path


def imported_sessions(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT uuid FROM sessions")}


def test_commit_by_another_process_during_the_scan_does_not_fail_the_import(
    db_conn, competitor, project_dir, monkeypatch
):
    """A commit landing between the import's reads and its first write used to fail
    that write immediately, because the transaction's read snapshot had gone stale.

    has_pending_tool_content is the first read after upsert_project, so it marks
    that window. The inserted row is arbitrary: only the other process's commit matters.
    """
    write_session(project_dir, "sess-known")
    assert import_project_contained(db_conn, project_dir, None) is not None
    write_session(project_dir, "sess-new")

    real_check = import_conversations.has_pending_tool_content
    commits = []

    def commit_once_then_check(cursor, filepath):
        if not commits:
            commits.append(filepath)
            competitor.execute(
                "INSERT INTO projects (path, key, name) VALUES ('/elsewhere', '-elsewhere', 'elsewhere')"
            )
        return real_check(cursor, filepath)

    monkeypatch.setattr(import_conversations, "has_pending_tool_content", commit_once_then_check)

    assert import_project_contained(db_conn, project_dir, None) is not None
    assert commits, "the competing commit must actually have happened"
    assert imported_sessions(db_conn) == {"sess-known", "sess-new"}


def test_unchanged_project_imports_while_another_process_holds_the_write_lock(db_conn, competitor, project_dir):
    """A project with nothing new has nothing to write, so it must not need the lock."""
    write_session(project_dir, "sess-known")
    assert import_project_contained(db_conn, project_dir, None) is not None

    competitor.execute("BEGIN IMMEDIATE")
    try:
        assert import_project_contained(db_conn, project_dir, None) == (0, 0, 1)
    finally:
        competitor.execute("ROLLBACK")


def test_locked_project_fails_alone_and_imports_on_the_next_run(db_conn, competitor, project_dir):
    """Running out of busy_timeout costs that project, not the rest of the run."""
    write_session(project_dir, "sess-new")

    competitor.execute("BEGIN IMMEDIATE")
    try:
        assert import_project_contained(db_conn, project_dir, None) is None
    finally:
        competitor.execute("ROLLBACK")

    assert not db_conn.in_transaction
    assert imported_sessions(db_conn) == set()

    assert import_project_contained(db_conn, project_dir, None) is not None
    assert imported_sessions(db_conn) == {"sess-new"}


def test_other_database_errors_still_abort_a_project_import(db_conn, project_dir, monkeypatch):
    write_session(project_dir, "sess-new")

    def disk_full(*_args, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(import_conversations, "import_session", disk_full)

    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        import_project_contained(db_conn, project_dir, None)


def seed_two_repair_candidates(memory_db, project_id, tmp_path) -> list:
    for uuid in ("sess-first", "sess-second"):
        seed_stale_tail_session(memory_db, project_id, tmp_path / f"{uuid}.jsonl", uuid=uuid)
    candidates = find_repairable_sessions(memory_db, sources=import_log_source_index(memory_db.cursor()))
    assert {c[0] for c in candidates} == {"sess-first", "sess-second"}
    return candidates


def fail_first_session(monkeypatch, error: Exception) -> None:
    real_import = import_conversations.import_session
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise error
        return real_import(*args, **kwargs)

    monkeypatch.setattr(import_conversations, "import_session", fail_once)


def test_locked_repair_candidate_is_counted_failed_and_the_batch_continues(
    memory_db, project_id, tmp_path, monkeypatch
):
    candidates = seed_two_repair_candidates(memory_db, project_id, tmp_path)
    fail_first_session(monkeypatch, locked_error())

    assert repair_sessions(memory_db, candidates) == (1, 1, 1, 0)


def test_other_database_errors_abort_the_repair_batch_with_its_counts(memory_db, project_id, tmp_path, monkeypatch):
    candidates = seed_two_repair_candidates(memory_db, project_id, tmp_path)
    real_import = import_conversations.import_session
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise sqlite3.OperationalError("database or disk is full")
        return real_import(*args, **kwargs)

    monkeypatch.setattr(import_conversations, "import_session", fail_second)

    messages_before = memory_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    with pytest.raises(import_repair.RepairAborted, match="disk is full") as excinfo:
        repair_sessions(memory_db, candidates)

    assert excinfo.value.counts == (1, 1, 0, 0)
    # What get_connection does when the abort propagates. The reported work must survive.
    memory_db.rollback()
    assert memory_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == messages_before + 1


class TestRun:
    """Whole runs: the overlap guard and the summary. Projects import in sorted order."""

    @pytest.fixture
    def projects_dir(self, tmp_path) -> Path:
        path = tmp_path / "projects"
        for key in ("-aaa-first", "-zzz-second"):
            (path / key).mkdir(parents=True)
            write_session(path / key, f"sess{key}")
        return path

    @pytest.fixture(autouse=True)
    def shared_connection(self, memory_db, monkeypatch) -> None:
        @contextmanager
        def fake_connection(*_args, **_kwargs):
            yield memory_db

        monkeypatch.setattr(import_conversations, "get_connection", fake_connection)
        monkeypatch.setattr(import_conversations, "lower_scheduling_priority", lambda _level: None)

    def test_plain_import_skips_while_another_import_runs(self, memory_db, tmp_path, projects_dir, capsys):
        marker = pid_file_path(PID_KEY_IMPORT)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(os.getppid()))  # a live process that isn't this one
        try:
            run(db=tmp_path / "memory.db", projects_dir=projects_dir)

            assert "another import is already running" in capsys.readouterr().out
            assert imported_sessions(memory_db) == set()
            assert marker.exists(), "run() must not delete a marker it did not acquire"
        finally:
            remove_pid_file(PID_KEY_IMPORT)

    def test_plain_import_releases_the_guard_when_done(self, memory_db, tmp_path, projects_dir):
        run(db=tmp_path / "memory.db", projects_dir=projects_dir)

        assert imported_sessions(memory_db) == {"sess-aaa-first", "sess-zzz-second"}
        assert not pid_file_path(PID_KEY_IMPORT).exists()

    def test_aborted_run_still_reports_what_it_imported(self, tmp_path, projects_dir, monkeypatch, capsys):
        real_import = import_conversations.import_session

        def fail_on_second_project(conn, filepath, project_id, *, force=False):
            if filepath.parent.name == "-zzz-second":
                raise sqlite3.OperationalError("database or disk is full")
            return real_import(conn, filepath, project_id, force=force)

        monkeypatch.setattr(import_conversations, "import_session", fail_on_second_project)

        with pytest.raises(sqlite3.OperationalError, match="disk is full"):
            run(db=tmp_path / "memory.db", projects_dir=projects_dir)

        out = capsys.readouterr().out
        assert "Total: 1 branches, 2 messages imported" in out
        assert "aborted" in out

    def test_aborted_repair_still_reports_what_it_repaired(self, tmp_path, projects_dir, monkeypatch, capsys):
        def abort_after_one(_conn, candidates, on_reclaim):
            raise import_repair.RepairAborted("database or disk is full", (1, 3, 0, 0))

        monkeypatch.setattr(import_repair, "repair_sessions", abort_after_one)
        monkeypatch.setattr(import_conversations.ingestion_status, "find_repairable_sessions", lambda _conn: [])

        with pytest.raises(sqlite3.OperationalError, match="disk is full"):
            run(db=tmp_path / "memory.db", projects_dir=projects_dir, repair_gaps=True)

        assert "Repaired 1 session(s), recovered 3 message(s)" in capsys.readouterr().out

    def test_locked_repair_scan_skips_repair_and_reports_it(self, tmp_path, projects_dir, monkeypatch, capsys):
        def locked_scan(_conn):
            raise locked_error()

        monkeypatch.setattr(import_conversations.ingestion_status, "find_repairable_sessions", locked_scan)

        with pytest.raises(SystemExit) as excinfo:
            run(db=tmp_path / "memory.db", projects_dir=projects_dir, repair_gaps=True)

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "Skipped gap repair" in out
        assert "Total: 2 branches, 4 messages imported" in out

    def test_locked_project_is_reported_and_later_projects_still_import(
        self, memory_db, tmp_path, projects_dir, monkeypatch, capsys
    ):
        real_import = import_conversations.import_session

        def lock_first_project(conn, filepath, project_id, *, force=False):
            if filepath.parent.name == "-aaa-first":
                raise locked_error()
            return real_import(conn, filepath, project_id, force=force)

        monkeypatch.setattr(import_conversations, "import_session", lock_first_project)

        with pytest.raises(SystemExit) as excinfo:
            run(db=tmp_path / "memory.db", projects_dir=projects_dir)

        assert excinfo.value.code == 1
        assert imported_sessions(memory_db) == {"sess-zzz-second"}
        out = capsys.readouterr().out
        assert "Failed to import -aaa-first" in out
        assert "1 project(s) failed" in out
