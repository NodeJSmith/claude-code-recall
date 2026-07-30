"""Tests for the consolidated ccrecall status reporter."""

import json
import sqlite3
from unittest.mock import patch

from ccrecall.db import get_connection
from ccrecall.status import collect_status, run


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
