"""Tests for the consolidated ccrecall status reporter."""

import json
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from ccrecall.config import load_settings
from ccrecall.db import get_connection
from ccrecall.embeddings import EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.recap_eligibility import ELIGIBLE_SUBSTANTIVE_PROSE, NO_ELIGIBLE_MESSAGES
from ccrecall.status import collect_status, print_status_report, run
from ccrecall.summarizer import SUMMARY_VERSION
from ccrecall.summary_enrichment import build_stored_enrichment_envelope
from ccrecall.tool_content_status import count_pending_missing_jsonl


def test_collect_status_skips_deep_ingestion_by_default(tmp_path):
    db_path = tmp_path / "status.db"
    with get_connection({"db_path": str(db_path)}, load_vec=False):
        pass

    with patch("ccrecall.status.summarize_ingestion") as summarize:
        status = collect_status(db=db_path)

    summarize.assert_not_called()
    assert status["ingestion"] is None
    assert "tool_content" in status
    assert "embeddings" in status


def test_status_import_stays_outside_provider_boundary():
    probe = """
import sys
import ccrecall.status
assert 'ccrecall.llm_summarizer' not in sys.modules
"""
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30, check=False)
    assert completed.returncode == 0, completed.stderr


def test_collect_status_does_not_create_missing_database(tmp_path):
    db_path = tmp_path / "missing.db"

    try:
        collect_status(db=db_path)
    except FileNotFoundError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("collect_status should reject missing DB paths")

    assert not db_path.exists()


def test_collect_status_check_ingestion_does_not_create_missing_database(tmp_path):
    db_path = tmp_path / "missing-check.db"

    try:
        collect_status(db=db_path, check_ingestion=True)
    except FileNotFoundError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("collect_status should reject missing DB paths")

    assert not db_path.exists()


def test_collect_status_reports_outdated_schema_without_migrating(tmp_path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE projects (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, uuid TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER, uuid TEXT);
        CREATE TABLE branches (id INTEGER PRIMARY KEY, session_id INTEGER, is_active INTEGER);
        """
    )
    conn.close()

    status = collect_status(db=db_path)

    assert status["schema"]["current"] is False
    assert "tool_content" in status["schema"]["reason"]
    assert status["recap"]["capability"] == "unavailable"


def test_collect_status_check_ingestion_reports_outdated_schema_without_migrating(tmp_path):
    db_path = tmp_path / "old-check.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE projects (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, uuid TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER, uuid TEXT);
        CREATE TABLE branches (id INTEGER PRIMARY KEY, session_id INTEGER, is_active INTEGER);
        """
    )
    conn.close()

    with patch("ccrecall.status.summarize_ingestion") as summarize:
        status = collect_status(db=db_path, check_ingestion=True)

    summarize.assert_not_called()
    assert status["schema"]["current"] is False
    assert "tool_content" in status["schema"]["reason"]

    probe = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in probe.execute("PRAGMA table_info(messages)").fetchall()]
        assert "tool_content" not in columns
    finally:
        probe.close()


def test_collect_status_skips_missing_jsonl_scan_when_tool_content_complete(tmp_path):
    db_path = tmp_path / "status.db"
    with get_connection({"db_path": str(db_path)}, load_vec=False):
        pass

    with patch("ccrecall.status.count_pending_missing_jsonl") as count_missing:
        status = collect_status(db=db_path)

    count_missing.assert_not_called()
    assert status["tool_content"]["pending_sessions"] == 0


def test_collect_status_runs_deep_ingestion_when_requested(tmp_path):
    db_path = tmp_path / "status.db"
    with get_connection({"db_path": str(db_path)}, load_vec=False):
        pass

    expected = {"sessions_checked": 0, "ok_sessions": 0}
    with patch("ccrecall.status.summarize_ingestion", return_value=expected) as summarize:
        status = collect_status(db=db_path, check_ingestion=True)

    summarize.assert_called_once()
    assert status["ingestion"] == expected


def test_collect_status_check_ingestion_uses_writable_connection_for_cache_metadata(tmp_path):
    db_path = tmp_path / "status-cache.db"
    with get_connection({"db_path": str(db_path)}, load_vec=False):
        pass

    expected = {"sessions_checked": 0, "ok_sessions": 0}
    writable_sources = {"sess-cache": {"existing": [], "missing": []}}

    def fake_summarize(conn, *, stale_tail_seconds=0, sources):
        assert sources == writable_sources
        conn.execute(
            "INSERT OR REPLACE INTO ingestion_check_cache (session_uuid, source_fingerprint) VALUES (?, ?)",
            ("sess-cache", "fp-1"),
        )
        return expected

    with (
        patch("ccrecall.status.import_log_source_index", return_value=writable_sources) as source_index,
        patch("ccrecall.status.summarize_ingestion", side_effect=fake_summarize) as summarize,
    ):
        status = collect_status(db=db_path, check_ingestion=True)

    source_index.assert_called_once()
    summarize.assert_called_once()
    assert status["ingestion"] == expected

    verify = sqlite3.connect(db_path)
    try:
        row = verify.execute(
            "SELECT session_uuid, source_fingerprint FROM ingestion_check_cache WHERE session_uuid = 'sess-cache'"
        ).fetchone()
        assert row == ("sess-cache", "fp-1")
    finally:
        verify.close()


def test_run_json_outputs_consolidated_payload(tmp_path, capsys):
    db_path = tmp_path / "status.db"
    with get_connection({"db_path": str(db_path)}, load_vec=False):
        pass

    run(db=db_path, output_format="json")

    data = json.loads(capsys.readouterr().out)
    assert data["database"]["sessions"] == 0
    assert data["ingestion"] is None
    assert "tool_content" in data
    assert "embeddings" in data


def test_run_human_mentions_consolidated_sections(tmp_path, capsys):
    db_path = tmp_path / "status.db"
    with get_connection({"db_path": str(db_path)}, load_vec=False):
        pass

    run(db=db_path)

    out = capsys.readouterr().out
    assert "Ingestion:" in out
    assert "Tool content:" in out
    assert "Embeddings:" in out


def test_recap_status_reports_defaults_platform_and_read_only_lifecycle(tmp_path, monkeypatch):
    db_path = tmp_path / "status.db"
    with get_connection({"db_path": str(db_path)}, load_vec=False) as conn:
        conn.execute(
            "INSERT INTO session_recap_provider_health(singleton, retry_after) VALUES (1, '2999-01-01T00:00:00Z')"
        )
    before = db_path.stat().st_mtime_ns
    monkeypatch.setattr("ccrecall.status.posix_process_groups_supported", lambda: False)
    status = collect_status(db=db_path)
    assert status["recap"]["capability"] == "ready"
    assert status["recap"]["defaults"]["model"] == "sonnet"
    assert status["recap"]["platform"] == {"provider_supported": False, "reason": "platform_unsupported"}
    assert status["recap"]["quarantine"]["max_count"] > 0
    assert status["recap"]["latest_attempt_outcomes"] == {}
    assert db_path.stat().st_mtime_ns == before


def test_recap_status_uses_shared_eligibility_evaluator_for_active_branches(tmp_path):
    db_path = tmp_path / "status.db"
    with get_connection({"db_path": str(db_path)}, load_vec=False) as conn:
        for session_id in (1, 2):
            conn.execute("INSERT INTO sessions(id, uuid) VALUES (?, ?)", (session_id, f"session-{session_id}"))
            conn.execute(
                "INSERT INTO branches(session_id, leaf_uuid, is_active, context_summary, context_summary_json, "
                "summary_version) VALUES (?, 'leaf', 1, 'summary', '{}', ?)",
                (session_id, SUMMARY_VERSION),
            )
        conn.execute("INSERT INTO sessions(id, uuid) VALUES (3, 'inactive-session')")
        conn.execute("INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (3, 'old-leaf', 0)")
        for position, (role, content) in enumerate((("user", "u" * 300), ("assistant", "a" * 300), ("user", "next"))):
            conn.execute(
                "INSERT INTO messages(session_id, uuid, role, content) VALUES (1, ?, ?, ?)",
                (f"message-{position}", role, content),
            )
            conn.execute(
                "INSERT INTO branch_messages(branch_id, message_id, position) VALUES (1, last_insert_rowid(), ?)",
                (position,),
            )

    status = collect_status(db=db_path)

    assert status["recap"]["eligibility"]["by_reason"] == {
        ELIGIBLE_SUBSTANTIVE_PROSE: 1,
        NO_ELIGIBLE_MESSAGES: 1,
    }


def test_recap_status_reports_queued_jobs_as_provider_disabled_when_unsupported(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "status.db"
    with get_connection({"db_path": str(db_path)}, load_vec=False) as conn:
        session_id = conn.execute("INSERT INTO sessions(uuid) VALUES ('queued') RETURNING id").fetchone()[0]
        conn.execute(
            "INSERT INTO session_recap_jobs(session_id, trigger, state, requested_at, updated_at) "
            "VALUES (?, 'test', 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (session_id,),
        )
    before = db_path.stat().st_mtime_ns
    monkeypatch.setattr("ccrecall.status.posix_process_groups_supported", lambda: False)

    run(db=db_path, output_format="json")

    status = json.loads(capsys.readouterr().out)
    assert status["recap"]["jobs"]["runnable"] == 0
    assert status["recap"]["jobs"]["provider_disabled_pending"] == 1

    run(db=db_path)

    assert "runnable: 0; provider-disabled pending: 1" in capsys.readouterr().out
    assert db_path.stat().st_mtime_ns == before
    verify = sqlite3.connect(db_path)
    try:
        assert verify.execute("SELECT state, reason FROM session_recap_jobs").fetchone() == ("pending", None)
    finally:
        verify.close()


def test_recap_status_classifies_invalid_renderer_caches_as_stale(tmp_path):
    db_path = tmp_path / "status.db"
    with get_connection({"db_path": str(db_path)}, load_vec=False) as conn:
        for session_id, input_contract, policy, envelope in (
            (
                1,
                0,
                1,
                build_stored_enrichment_envelope(
                    {"summary": "obsolete input contract"},
                    model="test",
                    generated_at="2026-01-01T00:00:00Z",
                    attempt_id=1,
                    recap_input_hash="input",
                ),
            ),
            (
                2,
                1,
                0,
                build_stored_enrichment_envelope(
                    {"summary": "obsolete eligibility policy"},
                    model="test",
                    generated_at="2026-01-01T00:00:00Z",
                    attempt_id=2,
                    recap_input_hash="input",
                ),
            ),
            (
                3,
                1,
                1,
                build_stored_enrichment_envelope(
                    {"summary": "current"},
                    model="test",
                    generated_at="2026-01-01T00:00:00Z",
                    attempt_id=3,
                    recap_input_hash="input",
                ),
            ),
            (4, 1, 1, {"version": 2, "summary": "missing required metadata"}),
        ):
            conn.execute("INSERT INTO sessions(id, uuid) VALUES (?, ?)", (session_id, f"session-{session_id}"))
            conn.execute(
                "INSERT INTO branches(session_id, leaf_uuid, is_active, recap_input_hash, "
                "summary_enrichment_json, summary_enrichment_version, summary_enrichment_status, "
                "summary_enrichment_input_hash, summary_enrichment_input_contract_version, "
                "summary_enrichment_policy_version) VALUES (?, 'leaf', 1, 'input', ?, 2, 'ok', 'input', ?, ?)",
                (session_id, json.dumps(envelope) if isinstance(envelope, dict) else envelope, input_contract, policy),
            )
    before = db_path.stat().st_mtime_ns

    status = collect_status(db=db_path)

    assert status["recap"]["populations"] == {"current": 1, "stale": 3, "legacy": 0, "pre_recap": 0}
    assert db_path.stat().st_mtime_ns == before


def test_recap_status_lists_safe_recovery_commands_for_blocked_work_and_quarantine(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "status.db"
    retryable_uuid = "12345678-1234-1234-1234-123456789abc"
    with get_connection({"db_path": str(db_path)}, load_vec=False) as conn:
        for session_uuid, reason in (
            (retryable_uuid, "timeout_exhausted"),
            ("cleanup", "cleanup_failed"),
            ("internal", "internal_error"),
        ):
            session_id = conn.execute("INSERT INTO sessions(uuid) VALUES (?) RETURNING id", (session_uuid,)).fetchone()[
                0
            ]
            conn.execute(
                "INSERT INTO session_recap_jobs(session_id, trigger, state, reason, requested_at, updated_at) "
                "VALUES (?, 'test', 'blocked', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (session_id, reason),
            )
        conn.execute(
            "INSERT INTO session_recap_attempts(id, session_id, job_session_id, input_hash, input_contract_version, "
            "policy_version, recap_contract_version, claim_token, trigger, state, created_at) "
            "VALUES (1, 2, 2, 'input', 1, 1, 2, 0, 'test', 'cleanup_failed', CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO session_recap_quarantine(attempt_id, path, nonce, byte_size, cleanup_state, created_at) "
            "VALUES (1, '/packet', 'nonce', 1, 'uncertain', CURRENT_TIMESTAMP)"
        )
    before = db_path.stat().st_mtime_ns
    settings = load_settings()
    settings.update({"recap_quarantine_max_count": 1, "recap_quarantine_max_bytes": 1})
    monkeypatch.setattr(
        "ccrecall.status.load_settings",
        lambda: settings,
    )
    status = collect_status(db=db_path)
    # internal_error jobs never self-requeue and --retry-failures matches on state,
    # not reason — so they are retryable and must be named, not buried in a count.
    assert status["recap"]["guidance"]["retry"] == [
        {
            "session": retryable_uuid,
            "command": f"ccrecall backfill llm-summaries --session {retryable_uuid} --retry-failures",
        },
        {
            "session": "internal",
            "command": "ccrecall backfill llm-summaries --session internal --retry-failures",
        },
    ]
    assert status["recap"]["guidance"]["cleanup"] == [{"session": "cleanup", "command": "ccrecall recap recover"}]
    # maintain cannot reduce this quarantine — it skips cleanup-failed attempts
    # whose removal is unproven, which is exactly what holds it open.
    assert status["recap"]["guidance"]["quarantine"] == "ccrecall recap recover"
    print_status_report(status)
    output = capsys.readouterr().out
    assert f"--session {retryable_uuid} --retry-failures" in output
    assert "--session cleanup --retry-failures" not in output
    assert "cleanup recovery (cleanup): ccrecall recap recover" in output
    assert "quarantine recovery: ccrecall recap recover" in output
    assert "force" not in output
    assert db_path.stat().st_mtime_ns == before


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

    assert count_pending_missing_jsonl(memory_db.cursor(), None) == 0

    memory_db.execute("UPDATE messages SET uuid = 'agent-only' WHERE session_id = ?", (session_id,))
    memory_db.commit()

    assert count_pending_missing_jsonl(memory_db.cursor(), None) == 1


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

    assert count_pending_missing_jsonl(memory_db.cursor(), None) == 1


class TestRunEmbeddingWatermarkCoverage:
    """`ccrecall status` reports honest branch-grain embedding coverage."""

    @staticmethod
    def _seed_embeddable_branch(conn, i: int, *, embedded: bool, is_active: int = 1) -> None:
        """Insert an active branch with one message (so it's CHUNK_EMBEDDABLE),
        optionally stamped at the current embedding watermark. ``i`` keys the
        unique columns so repeated calls don't collide."""
        cur = conn.cursor()
        cur.execute("INSERT INTO projects (path, key) VALUES (?, ?)", (f"/p/{i}", f"k-{i}"))
        project_id = cur.lastrowid
        cur.execute("INSERT INTO sessions (uuid, project_id) VALUES (?, ?)", (f"sess-{i}", project_id))
        session_id = cur.lastrowid
        version, model = (EMBEDDING_VERSION, EMBEDDING_MODEL) if embedded else (0, None)
        cur.execute(
            "INSERT INTO branches (session_id, leaf_uuid, is_active, embedding_version, embedding_model) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, f"leaf-{i}", is_active, version, model),
        )
        branch_id = cur.lastrowid
        cur.execute(
            "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, 'user', 'hi', ?)",
            (session_id, f"m-{i}", "2024-01-01T00:00:00Z"),
        )
        cur.execute(
            "INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, 0)",
            (branch_id, cur.lastrowid),
        )
        conn.commit()

    def test_reports_partial_coverage(self, tmp_path, capsys):
        """Two of three embeddable branches embedded → '2/3 branches (67%)'."""
        db_path = tmp_path / "coverage.db"
        with get_connection({"db_path": str(db_path)}, load_vec=False) as conn:
            self._seed_embeddable_branch(conn, 1, embedded=True)
            self._seed_embeddable_branch(conn, 2, embedded=True)
            self._seed_embeddable_branch(conn, 3, embedded=False)
            # An inactive branch must not count toward the embeddable denominator.
            self._seed_embeddable_branch(conn, 4, embedded=False, is_active=0)

        run(db=db_path)
        out = capsys.readouterr().out

        assert "watermark: 2/3 branches" in out

    def test_zero_embeddable_branches(self, tmp_path, capsys):
        """A DB with no embeddable branches reports 0/0 without dividing by zero."""
        db_path = tmp_path / "empty.db"
        with get_connection({"db_path": str(db_path)}, load_vec=False):
            pass

        run(db=db_path)
        out = capsys.readouterr().out

        assert "watermark: 0/0 branches" in out
