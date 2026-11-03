"""Tests for rendering `ccrecall status` and `ccrecall doctor` from findings."""

import json

import pytest
from conftest import VEC_SKIP
from whenever import Instant

from ccrecall import findings, run_history
from ccrecall.db import get_connection
from ccrecall.findings import GROUP_ACTION, GROUP_NONE, GROUP_WORKING, Diagnosis, Finding, SessionRow
from ccrecall.status import (
    diagnosis_json,
    format_ago,
    run_doctor,
    run_status,
    summary_line,
    validate_list_id,
    verdict_line,
)

pytestmark = VEC_SKIP


@pytest.fixture(autouse=True)
def _model_cached(monkeypatch):
    monkeypatch.setattr(findings, "is_model_cached_on_disk", lambda: True)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "conversations.db"
    with get_connection({"db_path": str(path)}, load_vec=True):
        pass
    return path


def queue_old_session(db_path):
    """One session waiting for embedding, long past backfill_stale_hours, with no backfill recorded."""
    with get_connection({"db_path": str(db_path)}) as conn:
        session_id = conn.execute("INSERT INTO sessions (uuid) VALUES ('queued')").lastrowid
        branch_id = conn.execute(
            "INSERT INTO branches (session_id, leaf_uuid, is_active, ended_at) "
            "VALUES (?, 'leaf', 1, '2026-01-01T10:00:00.000Z')",
            (session_id,),
        ).lastrowid
        message_id = conn.execute(
            "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, 'm', 'user', 'hi', '')",
            (session_id,),
        ).lastrowid
        conn.execute("INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (branch_id, message_id))


def diagnosis_of(*found: Finding, **summary) -> Diagnosis:
    return Diagnosis(
        findings=found,
        summary={"sessions": 7514, "embedded_pct": 98, "last_sync": None, **summary},
        sessions={},
    )


class TestStatus:
    def test_healthy_is_one_verdict_line_and_one_line_of_numbers(self, db_path, capsys):
        run_status(db=db_path)

        lines = capsys.readouterr().out.splitlines()
        assert lines == ["ccrecall: healthy", "0 sessions"]

    def test_not_healthy_points_to_doctor_and_still_exits_zero(self, db_path, capsys):
        queue_old_session(db_path)

        run_status(db=db_path)

        assert capsys.readouterr().out.splitlines()[0] == "ccrecall: needs attention (run `ccrecall doctor`)"

    def test_missing_database_renders_the_finding_and_exits_one(self, tmp_path, capsys):
        db_path = tmp_path / "missing.db"

        with pytest.raises(SystemExit) as exc_info:
            run_status(db=db_path, output_format="json")

        assert exc_info.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["verdict"] == "needs-attention"
        assert "no-database" in {finding["id"] for finding in data["findings"]}
        assert not db_path.exists()

    def test_unreadable_database_exits_one_naming_doctor(self, tmp_path, capsys):
        db_path = tmp_path / "bad.db"
        db_path.write_text("not a database")

        with pytest.raises(SystemExit) as exc_info:
            run_status(db=db_path)

        assert exc_info.value.code == 1
        assert "ccrecall doctor" in capsys.readouterr().err

    def test_json_shape_is_shared_with_doctor(self, db_path, capsys):
        queue_old_session(db_path)

        run_status(db=db_path, output_format="json")
        status_data = json.loads(capsys.readouterr().out)
        with pytest.raises(SystemExit):
            run_doctor(db=db_path, output_format="json")
        doctor_data = json.loads(capsys.readouterr().out)

        assert set(status_data) == {"verdict", "findings", "summary"}
        assert status_data == doctor_data
        assert set(status_data["summary"]) == {"sessions", "embedded_pct", "last_sync"}
        assert set(status_data["findings"][0]) == {"id", "group", "message", "count", "fix", "listable"}


class TestDoctor:
    def test_exits_zero_when_healthy(self, db_path, capsys):
        run_doctor(db=db_path)

        assert capsys.readouterr().out.startswith("ccrecall: healthy\n")

    def test_exits_zero_when_working_on_it(self, db_path, capsys, monkeypatch):
        monkeypatch.setattr(
            findings,
            "skipped_by_design",
            lambda _settings: findings.Found(Finding(id="x", group=GROUP_WORKING, message="busy")),
        )

        run_doctor(db=db_path)

        assert capsys.readouterr().out.startswith("ccrecall: working on it\n")

    def test_exits_one_when_something_needs_action(self, db_path, capsys):
        queue_old_session(db_path)

        with pytest.raises(SystemExit) as exc_info:
            run_doctor(db=db_path)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Needs action" in out
        assert "Fix: ccrecall backfill embeddings" in out
        assert "List them: ccrecall doctor --list embedding-backlog" in out

    def test_exits_one_for_a_missing_database(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            run_doctor(db=tmp_path / "missing.db")

        assert exc_info.value.code == 1

    def test_exits_two_when_the_database_cant_be_read(self, tmp_path, capsys):
        db_path = tmp_path / "bad.db"
        db_path.write_text("not a database")

        with pytest.raises(SystemExit) as exc_info:
            run_doctor(db=db_path)

        assert exc_info.value.code == 2
        assert capsys.readouterr().out == ""

    def test_list_prints_the_sessions_behind_a_finding(self, db_path, capsys):
        queue_old_session(db_path)

        run_doctor(db=db_path, list_id="embedding-backlog")

        assert capsys.readouterr().out.split() == ["queued", "-", "2026-01-01", "queued"]

    def test_list_json_adds_sessions_to_that_finding_only(self, db_path, capsys):
        queue_old_session(db_path)

        run_doctor(db=db_path, output_format="json", list_id="embedding-backlog")

        data = json.loads(capsys.readouterr().out)
        backlog = next(finding for finding in data["findings"] if finding["id"] == "embedding-backlog")
        assert backlog["sessions"] == [
            {"session_id": "queued", "project": None, "date": "2026-01-01T10:00:00.000Z", "reason": "queued"}
        ]
        assert all("sessions" not in finding for finding in data["findings"] if finding["id"] != "embedding-backlog")

    def test_list_of_an_absent_finding_says_so(self, db_path, capsys):
        run_doctor(db=db_path, list_id="deleted-transcripts")

        assert "Nothing to list" in capsys.readouterr().out

    def test_list_of_a_finding_that_names_no_sessions_says_so(self, db_path, capsys):
        run_doctor(db=db_path, list_id="schema-ahead")

        assert "doesn't name sessions" in capsys.readouterr().out

    def test_unknown_list_id_is_a_usage_error_naming_the_valid_ones(self):
        with pytest.raises(ValueError, match="embedding-backlog"):
            validate_list_id("nope")


class TestRendering:
    def test_summary_line_leaves_out_what_isnt_known(self):
        assert summary_line(diagnosis_of(embedded_pct=None)) == "7,514 sessions"

    def test_summary_line_shows_the_last_sync(self):
        now = Instant.now()
        last_sync = now.subtract(minutes=2).format_iso()

        line = summary_line(diagnosis_of(last_sync=last_sync), now=now)

        assert line == "7,514 sessions · 98% embedded · last sync 2 min ago"

    def test_verdict_line_points_to_doctor_only_when_asked_and_not_healthy(self):
        working = diagnosis_of(Finding(id="x", group=GROUP_WORKING, message="m"))
        healthy = diagnosis_of(Finding(id="x", group=GROUP_NONE, message="m"))

        assert verdict_line(working, point_to_doctor=True) == "ccrecall: working on it (run `ccrecall doctor`)"
        assert verdict_line(working) == "ccrecall: working on it"
        assert verdict_line(healthy, point_to_doctor=True) == "ccrecall: healthy"

    def test_json_omits_a_row_reason_that_isnt_set(self):
        diagnosis = Diagnosis(
            findings=(Finding(id="deleted-transcripts", group=GROUP_NONE, message="m", count=1, listable=True),),
            summary={"sessions": 1, "embedded_pct": None, "last_sync": None},
            sessions={"deleted-transcripts": (SessionRow("s1", "proj", "2026-01-01"),)},
        )

        data = diagnosis_json(diagnosis, list_id="deleted-transcripts")

        assert data["findings"][0]["sessions"] == [{"session_id": "s1", "project": "proj", "date": "2026-01-01"}]

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(10, "just now"), (125, "2 min ago"), (7300, "2 h ago"), (86400, "1 day ago"), (3 * 86400, "3 days ago")],
    )
    def test_format_ago(self, seconds, expected):
        now = Instant.now()
        assert format_ago(now.subtract(seconds=seconds), now) == expected


def test_verdict_group_names_are_the_json_contract():
    assert (GROUP_ACTION, GROUP_WORKING, GROUP_NONE) == ("action", "working", "none")
    assert run_history.RUN_KINDS == ("import", "repair", "sync", "backfill")
