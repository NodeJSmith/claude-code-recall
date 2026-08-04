"""Tests for the consolidated ccrecall status reporter."""

import json
import sqlite3
from unittest.mock import patch

from ccrecall.db import get_connection
from ccrecall.embeddings import EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.status import collect_status, run
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
        cur.execute("INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (branch_id, cur.lastrowid))
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
