"""Tests for the finding catalog `ccrecall doctor` and `ccrecall status` render."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import sqlite_vec
from conftest import VEC_SKIP, write_four_turns
from whenever import Instant

from ccrecall import findings, health, run_history
from ccrecall.config import PID_KEY_BACKFILL_EMBEDDINGS, PID_KEY_IMPORT, pid_file_path
from ccrecall.db import CONTENT_ERROR_VERSION, get_connection
from ccrecall.db_base import SCHEMA_VERSION
from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.findings import (
    GROUP_ACTION,
    GROUP_NONE,
    GROUP_WORKING,
    VERDICT_HEALTHY,
    VERDICT_NEEDS_ATTENTION,
    VERDICT_WORKING,
    Finding,
    diagnose,
    verdict_for,
)
from ccrecall.ingestion_status import source_fingerprint

pytestmark = VEC_SKIP

HOURS = 3600
LONG_AGO = "2026-01-01T10:00:00.000Z"


@pytest.fixture(autouse=True)
def _model_cached(monkeypatch):
    """The model's disk cache depends on the machine; findings tests assume it's there."""
    monkeypatch.setattr(findings, "is_model_cached_on_disk", lambda: True)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "conversations.db"
    with get_connection({"db_path": str(path)}, load_vec=True):
        pass
    return path


def iso_ago(seconds: float) -> str:
    return Instant.now().subtract(seconds=seconds).format_iso()


def add_session(
    db_path: Path,
    uuid: str,
    *,
    ended_at: str = LONG_AGO,
    exchanges: int = 2,
    chunked: int | None = None,
    stamped: bool = False,
    cap_tokens: int | None = None,
    vectors: bool = True,
    version: int | None = None,
) -> None:
    """A session with ``exchanges`` user/assistant pairs, ``chunked`` of them with chunks."""
    with get_connection({"db_path": str(db_path)}, load_vec=True) as conn:
        project_id = conn.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, 'proj') "
            "ON CONFLICT(key) DO UPDATE SET name = name RETURNING id",
            (f"/p/{uuid}", "proj"),
        ).fetchone()[0]
        session_id = conn.execute("INSERT INTO sessions (uuid, project_id) VALUES (?, ?)", (uuid, project_id)).lastrowid
        branch_version = version if version is not None else (EMBEDDING_VERSION if stamped else 0)
        branch_id = conn.execute(
            "INSERT INTO branches (session_id, leaf_uuid, is_active, ended_at, embedding_version, embedding_model) "
            "VALUES (?, 'leaf', 1, ?, ?, ?)",
            (session_id, ended_at, branch_version, EMBEDDING_MODEL if stamped else None),
        ).lastrowid
        for i in range(exchanges):
            for role, text in (("user", f"question {i}"), ("assistant", f"answer {i}")):
                message_id = conn.execute(
                    "INSERT INTO messages (session_id, uuid, role, content, timestamp, tool_content) "
                    "VALUES (?, ?, ?, ?, ?, '')",
                    (session_id, f"{uuid}-{role}-{i}", role, text, f"2026-01-01T10:00:{i:02d}.000Z"),
                ).lastrowid
                conn.execute(
                    "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (branch_id, message_id)
                )
        for i in range(exchanges if chunked is None else chunked):
            chunk_id = conn.execute(
                "INSERT INTO chunks (branch_id, exchange_index, content_hash, embedding_version, embedding_model, "
                "cap_tokens) VALUES (?, ?, 'h', ?, ?, ?)",
                (branch_id, i, EMBEDDING_VERSION, EMBEDDING_MODEL, cap_tokens),
            ).lastrowid
            if vectors:
                conn.execute(
                    "INSERT INTO chunk_vec (chunk_id, embedding) VALUES (?, ?)",
                    (chunk_id, sqlite_vec.serialize_float32([0.0] * EMBEDDING_DIM)),
                )


def record_backfill(db_path: Path, *, finished_ago: float, outcome: str = run_history.OUTCOME_OK) -> None:
    entry = {
        "outcome": outcome,
        "reason": None,
        "started_at": iso_ago(finished_ago + 60),
        "finished_at": iso_ago(finished_ago),
    }
    last_ok = entry if outcome == run_history.OUTCOME_OK else None
    run_history.runs_path(db_path).write_text(json.dumps({"backfill": {"last": entry, "last_ok": last_ok}}))


def write_marker(pid_key: str, pid: int, *, age_seconds: float = 0) -> None:
    marker = pid_file_path(pid_key)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(pid))
    then = time.time() - age_seconds
    os.utime(marker, (then, then))


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def by_id(diagnosis) -> dict[str, Finding]:
    return {finding.id: finding for finding in diagnosis.findings}


def settings(**overrides) -> dict:
    return {"backfill_stale_hours": 36, "exclude_projects": [], **overrides}


class TestVerdict:
    def test_worst_group_wins(self):
        make = lambda group: Finding(id="x", group=group, message="m")  # noqa: E731
        assert verdict_for([make(GROUP_NONE)]) == VERDICT_HEALTHY
        assert verdict_for([make(GROUP_NONE), make(GROUP_WORKING)]) == VERDICT_WORKING
        assert verdict_for([make(GROUP_WORKING), make(GROUP_ACTION)]) == VERDICT_NEEDS_ATTENTION


def test_fresh_database_is_healthy(db_path):
    diagnosis = diagnose(db_path, settings())

    assert diagnosis.verdict == VERDICT_HEALTHY
    assert set(by_id(diagnosis)) == {"skipped-by-design"}
    assert diagnosis.summary == {"sessions": 0, "embedded_pct": None, "last_sync": None}


def test_missing_database_is_a_finding_and_is_never_created(tmp_path):
    db_path = tmp_path / "missing.db"

    diagnosis = diagnose(db_path, settings())

    assert by_id(diagnosis)["no-database"].group == GROUP_ACTION
    assert by_id(diagnosis)["no-database"].fix == "ccrecall import"
    assert not db_path.exists()


def test_unreadable_database_raises(tmp_path):
    db_path = tmp_path / "bad.db"
    db_path.write_text("not a database")

    with pytest.raises(findings.DatabaseUnreadableError):
        diagnose(db_path, settings())


def test_diagnosis_writes_nothing(db_path, tmp_path):
    filepath = tmp_path / "sess-ok.jsonl"
    write_four_turns(filepath)
    add_session(db_path, "queued", chunked=0)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    siblings = set(db_path.parent.iterdir())

    diagnose(db_path, settings())

    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    # SQLite's own -wal/-shm files reappear on any open; nothing else may.
    sqlite_files = {db_path.with_name(db_path.name + suffix) for suffix in ("-wal", "-shm")}
    assert set(db_path.parent.iterdir()) - sqlite_files == siblings - sqlite_files


def test_schema_behind_skips_database_checks(db_path):
    add_session(db_path, "queued", chunked=0)
    with get_connection({"db_path": str(db_path)}) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")

    found = by_id(diagnose(db_path, settings()))

    assert found["schema-behind"].group == GROUP_ACTION
    assert "embedding-backlog" not in found


def test_schema_ahead_still_runs_checks(db_path):
    add_session(db_path, "queued", chunked=0)
    with get_connection({"db_path": str(db_path)}) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    found = by_id(diagnose(db_path, settings()))

    assert found["schema-ahead"].group == GROUP_ACTION
    assert "embedding-backlog" in found


class TestEmbeddingBacklog:
    def test_overdue_with_no_recent_backfill_needs_action(self, db_path):
        add_session(db_path, "queued", chunked=0)

        backlog = by_id(diagnose(db_path, settings()))["embedding-backlog"]

        assert backlog.group == GROUP_ACTION
        assert backlog.fix == "ccrecall backfill embeddings"
        assert "no backfill has been recorded" in backlog.message

    def test_recent_successful_backfill_keeps_it_working(self, db_path):
        add_session(db_path, "queued", chunked=0)
        record_backfill(db_path, finished_ago=2 * HOURS)

        assert by_id(diagnose(db_path, settings()))["embedding-backlog"].group == GROUP_WORKING

    def test_young_backlog_is_working_even_without_a_backfill(self, db_path):
        add_session(db_path, "queued", chunked=0, ended_at=iso_ago(2 * HOURS))

        assert by_id(diagnose(db_path, settings()))["embedding-backlog"].group == GROUP_WORKING

    def test_live_backfill_keeps_it_working(self, db_path, live_ccrecall_pid):
        add_session(db_path, "queued", chunked=0)
        write_marker(PID_KEY_BACKFILL_EMBEDDINGS, live_ccrecall_pid)

        found = by_id(diagnose(db_path, settings()))

        assert found["embedding-backlog"].group == GROUP_WORKING
        assert found["backfill-running"].group == GROUP_WORKING

    def test_killed_backfill_is_named_when_it_needs_action(self, db_path):
        add_session(db_path, "queued", chunked=0)
        write_marker(PID_KEY_BACKFILL_EMBEDDINGS, dead_pid(), age_seconds=50 * HOURS)

        backlog = by_id(diagnose(db_path, settings()))["embedding-backlog"]

        assert backlog.group == GROUP_ACTION
        assert "was killed" in backlog.message

    def test_each_reason_is_listed(self, db_path):
        add_session(db_path, "queued", chunked=0)
        add_session(db_path, "repaired", exchanges=3, chunked=2)
        add_session(db_path, "no-vectors", vectors=False)
        add_session(db_path, "stamped-gap", exchanges=3, chunked=2, stamped=True)

        diagnosis = diagnose(db_path, settings())

        assert by_id(diagnosis)["embedding-backlog"].count == 4
        reasons = {row.session_id: row.reason for row in diagnosis.sessions["embedding-backlog"]}
        assert reasons == {
            "queued": "queued",
            "repaired": "repaired",
            "no-vectors": "missing-vectors",
            "stamped-gap": "missing-vectors",
        }

    def test_session_active_in_the_last_minutes_is_in_progress_not_backlog(self, db_path):
        add_session(db_path, "live", chunked=0, ended_at=iso_ago(60))

        found = by_id(diagnose(db_path, settings()))

        assert "embedding-backlog" not in found
        assert found["sessions-in-progress"].group == GROUP_NONE


class TestCoverage:
    def test_embedded_and_draft_sessions_count_as_embedded(self, db_path):
        add_session(db_path, "full", stamped=True)
        add_session(db_path, "draft", cap_tokens=4096)
        add_session(db_path, "queued", chunked=0)
        add_session(db_path, "bad", chunked=0, version=CONTENT_ERROR_VERSION)

        diagnosis = diagnose(db_path, settings())

        # 4 sessions; the unembeddable one leaves the denominator; 2 of the other 3 are embedded.
        assert diagnosis.summary["sessions"] == 4
        assert diagnosis.summary["embedded_pct"] == 66

    def test_draft_sessions_are_working_on_it_after_a_recent_backfill(self, db_path):
        add_session(db_path, "draft", cap_tokens=4096)
        record_backfill(db_path, finished_ago=HOURS)

        draft = by_id(diagnose(db_path, settings()))["draft-sessions"]

        assert draft.group == GROUP_WORKING
        assert draft.fix is None

    def test_draft_sessions_never_need_action(self, db_path):
        add_session(db_path, "draft", cap_tokens=4096)

        draft = by_id(diagnose(db_path, settings()))["draft-sessions"]

        assert draft.group == GROUP_NONE
        assert draft.fix == "ccrecall backfill embeddings"

    def test_unembeddable_sessions_are_listed_and_need_nothing(self, db_path):
        add_session(db_path, "bad", chunked=0, version=CONTENT_ERROR_VERSION)

        diagnosis = diagnose(db_path, settings())

        assert by_id(diagnosis)["unembeddable-sessions"].group == GROUP_NONE
        assert [row.session_id for row in diagnosis.sessions["unembeddable-sessions"]] == ["bad"]


class TestIngestion:
    @pytest.fixture
    def gapped(self, db_path, tmp_path):
        """A session whose transcript has four turns but whose database is missing the last two."""
        filepath = tmp_path / "sess-gap.jsonl"
        write_four_turns(filepath)
        old = time.time() - 2 * HOURS
        os.utime(filepath, (old, old))
        with get_connection({"db_path": str(db_path)}) as conn:
            session_id = conn.execute("INSERT INTO sessions (uuid) VALUES ('sess-gap')").lastrowid
            branch_id = conn.execute(
                "INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'a1', 1)", (session_id,)
            ).lastrowid
            for uuid in ("u1", "a1"):
                message_id = conn.execute(
                    "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, ?, 'user', 'x', '')",
                    (session_id, uuid),
                ).lastrowid
                conn.execute(
                    "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (branch_id, message_id)
                )
            conn.execute(
                "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'h', 2)",
                (str(filepath),),
            )
        return filepath

    def test_incomplete_session_needs_repair(self, db_path, gapped):
        diagnosis = diagnose(db_path, settings())

        fixable = by_id(diagnosis)["fixable-sessions"]
        assert fixable.group == GROUP_ACTION
        assert fixable.fix == "ccrecall import --repair-gaps"
        assert [row.session_id for row in diagnosis.sessions["fixable-sessions"]] == ["sess-gap"]

    def test_running_import_makes_it_working(self, db_path, gapped, live_ccrecall_pid):
        write_marker(PID_KEY_IMPORT, live_ccrecall_pid)

        fixable = by_id(diagnose(db_path, settings()))["fixable-sessions"]

        assert fixable.group == GROUP_WORKING
        assert fixable.fix is None

    def test_session_repair_could_not_fix_is_listed_apart_with_no_fix(self, db_path, gapped):
        run_history.write_unfixable(db_path, {"sess-gap": source_fingerprint([gapped])}, replace=True)

        found = by_id(diagnose(db_path, settings()))

        assert "fixable-sessions" not in found
        assert found["unfixable-sessions"].group == GROUP_NONE
        assert found["unfixable-sessions"].fix is None

    def test_changed_transcript_makes_an_unfixable_session_fixable_again(self, db_path, gapped):
        run_history.write_unfixable(db_path, {"sess-gap": "an older fingerprint"}, replace=True)

        found = by_id(diagnose(db_path, settings()))

        assert "unfixable-sessions" not in found
        assert found["fixable-sessions"].group == GROUP_ACTION

    def test_deleted_transcript_needs_nothing(self, db_path, gapped):
        gapped.unlink()

        found = by_id(diagnose(db_path, settings()))

        assert found["deleted-transcripts"].group == GROUP_NONE


class TestRuns:
    def write_runs(self, db_path, kind, outcome, reason):
        entry = {"outcome": outcome, "reason": reason, "started_at": iso_ago(120), "finished_at": iso_ago(60)}
        run_history.runs_path(db_path).write_text(json.dumps({kind: {"last": entry, "last_ok": None}}))

    def test_busy_failure_is_working_on_it(self, db_path):
        self.write_runs(db_path, "import", run_history.OUTCOME_PARTIAL, run_history.REASON_DATABASE_BUSY)

        failed = by_id(diagnose(db_path, settings()))["import-failed"]

        assert failed.group == GROUP_WORKING
        assert failed.fix is None

    def test_other_failure_needs_action_with_its_rerun_command(self, db_path):
        self.write_runs(db_path, "repair", run_history.OUTCOME_FAILED, run_history.REASON_ERROR)

        failed = by_id(diagnose(db_path, settings()))["repair-failed"]

        assert failed.group == GROUP_ACTION
        assert failed.fix == "ccrecall import --repair-gaps"

    def test_later_success_clears_a_failure(self, db_path):
        self.write_runs(db_path, "sync", run_history.OUTCOME_OK, None)

        assert "sync-failed" not in by_id(diagnose(db_path, settings()))

    def test_recent_kill_is_working_on_it(self, db_path):
        write_marker(PID_KEY_IMPORT, dead_pid(), age_seconds=HOURS)

        killed = by_id(diagnose(db_path, settings()))["import-killed"]

        assert killed.group == GROUP_WORKING
        assert killed.fix == "ccrecall import"

    def test_old_kill_needs_nothing(self, db_path):
        write_marker(PID_KEY_IMPORT, dead_pid(), age_seconds=40 * HOURS)

        assert by_id(diagnose(db_path, settings()))["import-killed"].group == GROUP_NONE

    def test_run_older_than_the_limit_is_stuck(self, db_path, live_ccrecall_pid):
        write_marker(PID_KEY_BACKFILL_EMBEDDINGS, live_ccrecall_pid, age_seconds=40 * HOURS)

        stuck = by_id(diagnose(db_path, settings()))["backfill-stuck"]

        assert stuck.group == GROUP_ACTION
        assert str(live_ccrecall_pid) in stuck.message
        assert str(live_ccrecall_pid) in stuck.fix

    def test_last_sync_comes_from_the_last_success(self, db_path):
        entry = {"outcome": "ok", "reason": None, "started_at": iso_ago(70), "finished_at": iso_ago(60)}
        run_history.runs_path(db_path).write_text(json.dumps({"sync": {"last": entry, "last_ok": entry}}))

        assert diagnose(db_path, settings()).summary["last_sync"] == entry["finished_at"]


def test_a_check_that_raises_is_a_finding_and_the_rest_still_run(db_path, monkeypatch):
    add_session(db_path, "queued", chunked=0)

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(findings, "tool_content_findings", broken)

    diagnosis = diagnose(db_path, settings())
    found = by_id(diagnosis)

    assert found["check-failed"].group == GROUP_ACTION
    assert "tool content" in found["check-failed"].message
    assert "RuntimeError" in found["check-failed"].message
    assert "embedding-backlog" in found


def test_embedding_status_failure_is_reported_with_its_fix(db_path):
    health.record_embedding_failure(health.REASON_MODEL_UNAVAILABLE)

    unavailable = by_id(diagnose(db_path, settings()))["embedding-unavailable"]

    assert unavailable.group == GROUP_ACTION
    assert unavailable.fix == "ccrecall backfill embeddings"


def test_model_missing_from_disk_points_to_warm_model(db_path, monkeypatch):
    monkeypatch.setattr(findings, "is_model_cached_on_disk", lambda: False)

    assert by_id(diagnose(db_path, settings()))["embedding-unavailable"].fix == "ccrecall-warm-model"


def test_unwritable_database_needs_action(db_path):
    db_path.chmod(0o400)
    try:
        found = by_id(diagnose(db_path, settings()))
    finally:
        db_path.chmod(0o600)

    assert found["not-writable"].group == GROUP_ACTION


def test_unwritable_database_directory_explains_why_it_cant_be_read(db_path):
    db_path.parent.chmod(0o500)
    try:
        with pytest.raises(findings.DatabaseUnreadableError, match="can't write to"):
            diagnose(db_path, settings())
    finally:
        db_path.parent.chmod(0o700)


def test_every_id_a_finding_can_have_is_in_the_catalog(db_path):
    assert {"backfill-stuck", "import-running", "sync-killed", "repair-failed"} <= findings.FINDING_IDS
    assert "sync-running" not in findings.FINDING_IDS
    assert "repair-killed" not in findings.FINDING_IDS
