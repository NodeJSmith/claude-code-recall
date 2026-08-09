import json
import logging
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from ccrecall.config import pid_file_path
from ccrecall.file_hashing import transcript_file_hash
from ccrecall.hooks import backfill_llm_summaries as worker
from ccrecall.llm_summarizer import InvocationResult, write_capability_sidecar
from ccrecall.llm_summary_db import get_connection
from ccrecall.summarizer import SUMMARY_VERSION
from ccrecall.summary_enrichment import (
    STATUS_AUTH_REQUIRED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CAPABILITY_UNVERIFIED,
    STATUS_CLAUDE_UNAVAILABLE,
    STATUS_ERROR,
    STATUS_INVALID_OUTPUT,
    STATUS_MISSING_SOURCE,
    STATUS_OK,
    STATUS_RATE_LIMITED,
    STATUS_SOURCE_CHANGED,
    STATUS_SOURCE_UNVERIFIED,
    STATUS_TIMEOUT,
    STATUS_UNSUPPORTED_CLI,
    compute_summary_source_hash,
)


def _uuid(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, label))


def _entry(message_uuid: str, parent_uuid: str | None, timestamp: str, role: str, text: str) -> dict:
    return {
        "uuid": message_uuid,
        "parentUuid": parent_uuid,
        "type": role,
        "timestamp": timestamp,
        "message": {"role": role, "content": text},
    }


def _response_body(active_branch_uuids: set[str], file_path: str = "src/main.py") -> dict:
    uuids = sorted(active_branch_uuids)
    return {
        "title": {"text": "Resume branch", "source_uuids": [uuids[0]]},
        "where_we_left_off": {"text": "Latest state", "source_uuids": [uuids[0]]},
        "how_we_got_here": {"text": "Causal path", "source_uuids": [uuids[1]]},
        "key_decisions": [],
        "attempted_paths": [],
        "open_questions": [],
        "files_and_reasons": [{"path": file_path, "reason": "Changed here", "source_uuids": [uuids[0]]}],
        "continuation_hints": [{"text": "Next step", "source_uuids": [uuids[1]]}],
        "confidence": "high",
    }


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")


def _base_settings(db_path: Path) -> dict:
    return {
        "db_path": str(db_path),
        "llm_summary_timeout_seconds": 30,
        "llm_summary_model": "sonnet",
        "llm_summary_effort": "medium",
        "llm_summary_max_budget_usd": 1.0,
        "llm_summary_min_exchanges": 9,
    }


def _seed_branch(
    db_path: Path,
    transcript_path: Path,
    *,
    session_uuid: str | None = None,
    summary_source_hash: str | None = "current-hash",
    summary_enrichment_status: str | None = None,
    summary_enrichment_error: str | None = None,
    context_summary_json: str = '{"version": 2, "topic": "LLM summaries"}',
    exchange_count: int = 10,
) -> dict[str, object]:
    settings = _base_settings(db_path)
    session_uuid = session_uuid or _uuid(f"session-{db_path.name}")
    user_uuid = _uuid(f"user-{session_uuid}")
    assistant_uuid = _uuid(f"assistant-{session_uuid}")
    transcript_path = transcript_path.with_name(f"{session_uuid}.jsonl")
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        transcript_path,
        [
            _entry(user_uuid, None, "2026-08-07T10:00:00Z", "user", "Investigate the worker bug"),
            _entry(assistant_uuid, user_uuid, "2026-08-07T10:00:01Z", "assistant", "I found the failing path"),
        ],
    )
    file_hash = transcript_file_hash(transcript_path)
    file_stat = transcript_path.stat()

    with get_connection(settings) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO projects (path, key, name) VALUES (?, ?, ?)",
            (str(transcript_path.parent), f"-{session_uuid}", "proj"),
        )
        cursor.execute("SELECT id FROM projects WHERE path = ?", (str(transcript_path.parent),))
        project_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO sessions (uuid, project_id, git_branch, cwd) VALUES (?, ?, ?, ?)",
            (session_uuid, project_id, "main", str(transcript_path.parent)),
        )
        session_id = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO branches (
                session_id, leaf_uuid, is_active, started_at, ended_at, exchange_count,
                files_modified, commits, tool_counts, aggregated_content,
                context_summary, context_summary_json, summary_version,
                summary_enrichment_status, summary_enrichment_error, summary_source_hash
            )
            VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                assistant_uuid,
                "2026-08-07T10:00:00Z",
                "2026-08-07T10:10:00Z",
                exchange_count,
                json.dumps(["src/main.py"]),
                json.dumps(["fix: worker"]),
                json.dumps({"Read": 1}),
                "Investigate the worker bug\nI found the failing path",
                "Deterministic summary",
                context_summary_json,
                SUMMARY_VERSION,
                summary_enrichment_status,
                summary_enrichment_error,
                summary_source_hash,
            ),
        )
        branch_id = cursor.lastrowid
        cursor.executemany(
            """
            INSERT INTO messages (session_id, uuid, role, content, timestamp, tool_content, is_notification)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            [
                (session_id, user_uuid, "user", "Investigate the worker bug", "2026-08-07T10:00:00Z", ""),
                (session_id, assistant_uuid, "assistant", "I found the failing path", "2026-08-07T10:00:01Z", ""),
            ],
        )
        message_ids = [row[0] for row in cursor.execute("SELECT id FROM messages ORDER BY id").fetchall()]
        cursor.executemany(
            "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
            [(branch_id, message_id) for message_id in message_ids],
        )
        cursor.execute(
            """
            INSERT INTO import_log (file_path, file_hash, file_size, file_mtime, messages_imported)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(transcript_path), file_hash, file_stat.st_size, file_stat.st_mtime, 2),
        )

    return {
        "settings": settings,
        "branch_id": branch_id,
        "session_uuid": session_uuid,
        "active_branch_uuids": {user_uuid, assistant_uuid},
        "user_uuid": user_uuid,
        "assistant_uuid": assistant_uuid,
        "transcript_path": transcript_path,
    }


def _write_ok_sidecar(path: Path) -> None:
    write_capability_sidecar(path, status=STATUS_OK, claude_version="1.2.3", fingerprint="fingerprint")


class TestBackfillLlmSummaries:
    class _FakeProcessResult:
        def __init__(self, *, selected: bool, enriched: bool):
            self.selected = selected
            self.enriched = enriched

        def __bool__(self) -> bool:
            return self.enriched

    def test_worker_import_stays_on_llm_summary_db_boundary(self):
        probe = """
import sys
import ccrecall.hooks.backfill_llm_summaries as worker
assert worker.get_connection.__module__ == "ccrecall.llm_summary_db"
assert "ccrecall.db" not in sys.modules
"""
        completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)

        assert completed.returncode == 0, completed.stderr

    def test_main_forwards_current_session_flag_to_run(self, monkeypatch):

        run_kwargs: dict[str, object] = {}

        def fake_run(**kwargs):
            run_kwargs.update(kwargs)
            return worker.EXIT_OK

        monkeypatch.setattr(worker, "run", fake_run)

        assert (
            worker.main(["--days", "3", "--limit", "1", "--session", "session-123", "--current-session"])
            == worker.EXIT_OK
        )
        assert run_kwargs == {
            "days": 3,
            "limit": 1,
            "session": "session-123",
            "current_session": True,
            "force": False,
            "verbose": False,
        }

    def test_run_rejects_current_session_without_session(self):

        with pytest.raises(ValueError, match="--current-session requires --session"):
            worker._run(current_session=True)

    def test_worker_run_never_requests_load_vec_true(self, tmp_path, monkeypatch):

        connection_kwargs: list[dict[str, object]] = []

        class _Ctx:
            def __enter__(self):
                conn = sqlite3.connect(tmp_path / "no-load-vec.db")
                conn.execute("CREATE TABLE IF NOT EXISTS branches (id INTEGER)")
                self.conn = conn
                return conn

            def __exit__(self, exc_type, exc, tb):
                self.conn.close()
                return False

        def fake_get_connection(settings, **kwargs):
            del settings
            connection_kwargs.append(kwargs)
            assert kwargs.get("load_vec") is not True
            return _Ctx()

        monkeypatch.setattr(worker, "get_connection", fake_get_connection)
        monkeypatch.setattr(
            worker,
            "load_settings",
            lambda: {"db_path": str(tmp_path / "unused.db"), "llm_summary_min_exchanges": 9},
        )
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        monkeypatch.setattr(worker, "verify_capability_sidecar", lambda *a, **k: (STATUS_OK, None))
        monkeypatch.setattr(worker, "_select_branch_ids", lambda *a, **k: [])

        assert worker.run() == worker.EXIT_OK
        assert connection_kwargs == [{}]

    def test_run_persists_worker_owned_enrichment_without_rewriting_deterministic_fields(
        self, tmp_path, monkeypatch, capsys
    ):

        db_path = tmp_path / "worker.db"
        seeded = _seed_branch(db_path, tmp_path / "session.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        def fake_invoke(packet_dir, settings, prompt, *, active_branch_uuids, valid_file_paths, run=None):
            assert packet_dir.exists()
            assert settings["llm_summary_model"] == "sonnet"
            assert "branch-outline.json" in prompt
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        assert worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects") == worker.EXIT_OK

        output = capsys.readouterr().out
        assert "processing eligible branches" in output
        assert "complete: 1 branches enriched" in output

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT context_summary, context_summary_json, summary_version,
                       summary_enrichment_json, summary_enrichment_version,
                       summary_enrichment_status, summary_enrichment_source_hash,
                       summary_source_hash
                FROM branches WHERE id = ?
                """,
                (seeded["branch_id"],),
            ).fetchone()

        assert row[0] == "Deterministic summary"
        assert row[1] == '{"version": 2, "topic": "LLM summaries"}'
        assert row[2] == SUMMARY_VERSION
        stored = json.loads(row[3])
        assert stored["version"] == 1
        assert stored["model"] == "sonnet"
        assert row[4] == 1
        assert row[5] == STATUS_OK
        assert row[6] == row[7]

    def test_run_closes_read_connection_before_claude_and_reopens_for_write(self, tmp_path, monkeypatch):

        db_path = tmp_path / "tx.db"
        seeded = _seed_branch(db_path, tmp_path / "session.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        events: list[tuple[str, int]] = []
        real_get_connection = worker.get_connection

        def tracking_get_connection(settings):
            ctx = real_get_connection(settings)

            class _Ctx:
                def __enter__(self):
                    conn = ctx.__enter__()
                    events.append(("enter", id(conn)))
                    self.conn = conn
                    return conn

                def __exit__(self, exc_type, exc, tb):
                    events.append(("exit", id(self.conn)))
                    return ctx.__exit__(exc_type, exc, tb)

            return _Ctx()

        monkeypatch.setattr(worker, "get_connection", tracking_get_connection)
        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            assert events[-1][0] == "exit"
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

        assert [event[0] for event in events[:4]] == ["enter", "exit", "enter", "exit"]

    @pytest.mark.parametrize(
        "status",
        [
            STATUS_CLAUDE_UNAVAILABLE,
            STATUS_UNSUPPORTED_CLI,
            STATUS_AUTH_REQUIRED,
            STATUS_RATE_LIMITED,
            STATUS_BUDGET_EXCEEDED,
            STATUS_TIMEOUT,
            STATUS_INVALID_OUTPUT,
            STATUS_ERROR,
        ],
    )
    def test_failure_statuses_preserve_deterministic_fields(self, tmp_path, monkeypatch, status):

        db_path = tmp_path / f"{status}.db"
        seeded = _seed_branch(db_path, tmp_path / f"{status}.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        monkeypatch.setattr(
            worker,
            "invoke_claude",
            lambda *_args, **_kwargs: InvocationResult(status=status, diagnostic="classified diagnostic"),
        )

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT context_summary, context_summary_json, summary_version,
                       summary_enrichment_status, summary_enrichment_error,
                       summary_enrichment_json
                FROM branches WHERE id = ?
                """,
                (seeded["branch_id"],),
            ).fetchone()

        assert row[0] == "Deterministic summary"
        assert row[1] == '{"version": 2, "topic": "LLM summaries"}'
        assert row[2] == SUMMARY_VERSION
        assert row[3] == status
        assert row[4] == "classified diagnostic"
        assert row[5] is None

    def test_force_only_statuses_are_not_reselected_without_force(self, tmp_path, monkeypatch):

        db_path = tmp_path / "force-only.db"
        seeded = _seed_branch(
            db_path,
            tmp_path / "force-only.jsonl",
            summary_enrichment_status=STATUS_INVALID_OUTPUT,
        )
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        calls = {"count": 0}

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            calls["count"] += 1
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")
        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects", force=True)

        assert calls["count"] == 1

    def test_retryable_and_recovered_statuses_are_reselected(self, tmp_path, monkeypatch):

        db_path = tmp_path / "retryable.db"
        seeded = _seed_branch(
            db_path,
            tmp_path / "retryable.jsonl",
            summary_enrichment_status=STATUS_SOURCE_CHANGED,
        )
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        calls = {"count": 0}

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            calls["count"] += 1
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

        assert calls["count"] == 1

    @pytest.mark.parametrize("status", [STATUS_CAPABILITY_UNVERIFIED, STATUS_UNSUPPORTED_CLI, STATUS_AUTH_REQUIRED])
    def test_capability_blocked_statuses_wait_for_capability_recovery(self, tmp_path, monkeypatch, status):

        db_path = tmp_path / f"capability-{status}.db"
        seeded = _seed_branch(
            db_path,
            tmp_path / f"capability-{status}.jsonl",
            summary_enrichment_status=status,
        )
        sidecar = tmp_path / "capability.json"
        write_capability_sidecar(sidecar, status=status, claude_version="1.2.3", fingerprint="fingerprint")

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        calls = {"count": 0}

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            calls["count"] += 1
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")
        assert calls["count"] == 0

        _write_ok_sidecar(sidecar)
        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")
        assert calls["count"] == 1

    def test_source_failure_status_waits_for_source_recovery_before_reselection(self, tmp_path, monkeypatch):

        db_path = tmp_path / "source-recovery.db"
        seeded = _seed_branch(
            db_path,
            tmp_path / "source-recovery.jsonl",
            summary_enrichment_status=STATUS_SOURCE_CHANGED,
        )
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        seeded["transcript_path"].write_text('{"uuid":"changed"}\n', encoding="utf-8")

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        status_writes: list[str] = []
        invoke_calls = {"count": 0}
        real_write_status = worker._write_status

        def tracking_write_status(*args, status, **kwargs):
            status_writes.append(status)
            return real_write_status(*args, status=status, **kwargs)

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            invoke_calls["count"] += 1
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
            )

        monkeypatch.setattr(worker, "_write_status", tracking_write_status)
        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")
        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

        assert invoke_calls["count"] == 0
        assert status_writes == []

        restored_entries = [
            _entry(
                seeded["user_uuid"],
                None,
                "2026-08-07T10:00:00Z",
                "user",
                "Investigate the worker bug",
            ),
            _entry(
                seeded["assistant_uuid"],
                seeded["user_uuid"],
                "2026-08-07T10:00:01Z",
                "assistant",
                "I found the failing path",
            ),
        ]
        _write_jsonl(seeded["transcript_path"], restored_entries)
        restored_stat = seeded["transcript_path"].stat()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE import_log SET file_hash = ?, file_size = ?, file_mtime = ? WHERE file_path = ?",
                (
                    transcript_file_hash(seeded["transcript_path"]),
                    restored_stat.st_size,
                    restored_stat.st_mtime,
                    str(seeded["transcript_path"]),
                ),
            )
            conn.commit()

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

        assert invoke_calls["count"] == 1

    def test_run_pages_past_unchanged_early_rows_to_reach_later_eligible_branch(self, tmp_path, monkeypatch):

        db_path = tmp_path / "paging.db"
        first = _seed_branch(db_path, tmp_path / "paging-first.jsonl", session_uuid=_uuid("paging-first-session"))
        second = _seed_branch(
            db_path,
            tmp_path / "paging-second.jsonl",
            session_uuid=_uuid("paging-second-session"),
        )
        third = _seed_branch(db_path, tmp_path / "paging-third.jsonl", session_uuid=_uuid("paging-third-session"))
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        monkeypatch.setattr(worker, "BATCH_SIZE", 2)
        monkeypatch.setattr(worker, "load_settings", lambda: first["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        processed_ids: list[int] = []

        def fake_process_branch(branch_id, **_kwargs):
            processed_ids.append(branch_id)
            return branch_id == third["branch_id"]

        monkeypatch.setattr(worker, "_process_branch", fake_process_branch)

        assert worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects") == worker.EXIT_OK
        assert processed_ids == [first["branch_id"], second["branch_id"], third["branch_id"]]

    def test_limit_skips_current_rows_before_counting_first_selected_eligible_branch(self, tmp_path, monkeypatch):

        db_path = tmp_path / "limit-current-skip.db"
        first = _seed_branch(db_path, tmp_path / "limit-first.jsonl", session_uuid=_uuid("limit-first-session"))
        second = _seed_branch(db_path, tmp_path / "limit-second.jsonl", session_uuid=_uuid("limit-second-session"))
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        monkeypatch.setattr(worker, "BATCH_SIZE", 1)
        monkeypatch.setattr(worker, "load_settings", lambda: first["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        processed_ids: list[int] = []

        def fake_process_branch(branch_id, **_kwargs):
            processed_ids.append(branch_id)
            if branch_id == first["branch_id"]:
                return self._FakeProcessResult(selected=False, enriched=False)
            return self._FakeProcessResult(selected=True, enriched=True)

        monkeypatch.setattr(worker, "_process_branch", fake_process_branch)

        assert (
            worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects", limit=1) == worker.EXIT_OK
        )
        assert processed_ids == [first["branch_id"], second["branch_id"]]

    def test_limit_counts_eligible_branch_failure_once_reached(self, tmp_path, monkeypatch):

        db_path = tmp_path / "limit-failure.db"
        first = _seed_branch(db_path, tmp_path / "limit-failure-first.jsonl", session_uuid=_uuid("limit-failure-first"))
        second = _seed_branch(
            db_path,
            tmp_path / "limit-failure-second.jsonl",
            session_uuid=_uuid("limit-failure-second"),
        )
        _seed_branch(db_path, tmp_path / "limit-failure-third.jsonl", session_uuid=_uuid("limit-failure-third"))
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        monkeypatch.setattr(worker, "BATCH_SIZE", 1)
        monkeypatch.setattr(worker, "load_settings", lambda: first["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        processed_ids: list[int] = []

        def fake_process_branch(branch_id, **_kwargs):
            processed_ids.append(branch_id)
            if branch_id == first["branch_id"]:
                return self._FakeProcessResult(selected=False, enriched=False)
            if branch_id == second["branch_id"]:
                return self._FakeProcessResult(selected=True, enriched=False)
            return self._FakeProcessResult(selected=True, enriched=True)

        monkeypatch.setattr(worker, "_process_branch", fake_process_branch)

        assert (
            worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects", limit=1) == worker.EXIT_OK
        )
        assert processed_ids == [first["branch_id"], second["branch_id"]]

    def test_capability_check_budget_exceeded_preserves_recorded_status_and_retries_after_recheck(
        self, tmp_path, monkeypatch
    ):

        db_path = tmp_path / "capability-budget.db"
        seeded = _seed_branch(db_path, tmp_path / "capability-budget.jsonl")
        sidecar = tmp_path / "capability.json"
        write_capability_sidecar(
            sidecar,
            status=STATUS_BUDGET_EXCEEDED,
            claude_version="1.2.3",
            fingerprint="fingerprint",
            diagnostic="synthetic budget hit",
        )

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        invoke_calls = {"count": 0}

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            invoke_calls["count"] += 1
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT summary_enrichment_status, summary_enrichment_error FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()

        assert invoke_calls["count"] == 0
        assert row == (STATUS_BUDGET_EXCEEDED, "capability: synthetic budget hit")

        _write_ok_sidecar(sidecar)
        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

        with sqlite3.connect(db_path) as conn:
            status = conn.execute(
                "SELECT summary_enrichment_status FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()[0]

        assert invoke_calls["count"] == 1
        assert status == STATUS_OK

    def test_branch_level_budget_exceeded_stays_force_only_even_with_capability_recovery(self, tmp_path, monkeypatch):

        db_path = tmp_path / "branch-budget.db"
        seeded = _seed_branch(
            db_path,
            tmp_path / "branch-budget.jsonl",
            summary_enrichment_status=STATUS_BUDGET_EXCEEDED,
            summary_enrichment_error="branch budget hit",
        )
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        invoke_calls = {"count": 0}

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            invoke_calls["count"] += 1
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")
        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects", force=True)

        assert invoke_calls["count"] == 1

    def test_manual_capability_gated_run_prints_check_capability_guidance(self, tmp_path, monkeypatch, capsys):

        db_path = tmp_path / "capability-guidance.db"
        seeded = _seed_branch(db_path, tmp_path / "capability-guidance.jsonl")
        sidecar = tmp_path / "missing-capability.json"

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        invoke_calls = {"count": 0}
        monkeypatch.setattr(
            worker,
            "invoke_claude",
            lambda *_args, **_kwargs: invoke_calls.__setitem__("count", invoke_calls["count"] + 1),
        )

        assert worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects") == worker.EXIT_OK

        output = capsys.readouterr().out
        assert invoke_calls["count"] == 0
        assert "--check-capability" in output
        assert "capability gate blocked" in output

    def test_capability_blocked_branch_refreshes_when_sidecar_status_changes(self, tmp_path, monkeypatch):

        db_path = tmp_path / "capability-status-refresh.db"
        seeded = _seed_branch(
            db_path,
            tmp_path / "capability-status-refresh.jsonl",
            summary_enrichment_status=STATUS_AUTH_REQUIRED,
        )
        sidecar = tmp_path / "capability.json"
        write_capability_sidecar(
            sidecar,
            status=STATUS_RATE_LIMITED,
            claude_version="1.2.3",
            fingerprint="fingerprint",
            diagnostic="retry later",
        )

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        monkeypatch.setattr(worker, "invoke_claude", pytest.fail)

        assert worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects") == worker.EXIT_OK

        with sqlite3.connect(db_path) as conn:
            status = conn.execute(
                "SELECT summary_enrichment_status FROM branches WHERE id = ?", (seeded["branch_id"],)
            ).fetchone()[0]

        assert status == STATUS_RATE_LIMITED

    def test_matching_capability_status_skips_source_resolution(self, tmp_path, monkeypatch):

        db_path = tmp_path / "capability-status-skip.db"
        seeded = _seed_branch(
            db_path,
            tmp_path / "capability-status-skip.jsonl",
            summary_enrichment_status=STATUS_AUTH_REQUIRED,
        )
        sidecar = tmp_path / "capability.json"
        write_capability_sidecar(
            sidecar,
            status=STATUS_AUTH_REQUIRED,
            claude_version="1.2.3",
            fingerprint="fingerprint",
            diagnostic="sign in",
        )

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        monkeypatch.setattr(worker, "_resolve_source_files", pytest.fail)

        assert worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects") == worker.EXIT_OK

    def test_import_log_fidelity_beats_live_projects_lookup_for_historical_runs(self, tmp_path, monkeypatch):

        db_path = tmp_path / "historical-fidelity.db"
        seeded = _seed_branch(db_path, tmp_path / "historical-fidelity.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        seeded["transcript_path"].write_text('{"uuid":"changed"}\n', encoding="utf-8")
        live_projects_dir = tmp_path / "projects" / "proj"
        live_projects_dir.mkdir(parents=True)
        _write_jsonl(
            live_projects_dir / f"{seeded['session_uuid']}.jsonl",
            [
                _entry(
                    seeded["user_uuid"],
                    None,
                    "2026-08-07T10:00:00Z",
                    "user",
                    "Investigate the worker bug",
                ),
                _entry(
                    seeded["assistant_uuid"],
                    seeded["user_uuid"],
                    "2026-08-07T10:00:01Z",
                    "assistant",
                    "I found the failing path",
                ),
            ],
        )

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        invoke_calls = {"count": 0}
        monkeypatch.setattr(
            worker,
            "invoke_claude",
            lambda *_args, **_kwargs: invoke_calls.__setitem__("count", invoke_calls["count"] + 1),
        )

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT summary_enrichment_status, summary_enrichment_error FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()

        assert invoke_calls["count"] == 0
        assert row == (STATUS_SOURCE_CHANGED, STATUS_SOURCE_CHANGED)

    def test_load_import_log_rows_scopes_query_to_session_file_suffix(self, tmp_path):

        db_path = tmp_path / "import-log-scope.db"
        seeded = _seed_branch(db_path, tmp_path / "import-log-scope.jsonl")
        other_session_uuid = _uuid("other-import-log-session")
        other_path = tmp_path / f"{other_session_uuid}.jsonl"
        _write_jsonl(other_path, [_entry(_uuid("other-msg"), None, "2026-08-07T10:00:00Z", "user", "other")])

        traced: list[str] = []
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO import_log (file_path, file_hash, file_size, file_mtime, messages_imported) VALUES (?, ?, ?, ?, ?)",
                (str(other_path), "other-hash", 1, 1.0, 1),
            )
            conn.commit()
            conn.set_trace_callback(traced.append)
            rows = worker._load_import_log_rows(conn.cursor(), seeded["session_uuid"])

        assert rows == [
            (
                str(seeded["transcript_path"]),
                transcript_file_hash(seeded["transcript_path"]),
                seeded["transcript_path"].stat().st_size,
                seeded["transcript_path"].stat().st_mtime,
            )
        ]
        assert any(
            "FROM import_log" in statement
            and "WHERE file_path LIKE" in statement
            and f"%{seeded['session_uuid']}.jsonl" in statement
            for statement in traced
        )

    def test_current_session_run_prefers_live_projects_lookup_over_placeholder_import_log(self, tmp_path, monkeypatch):

        db_path = tmp_path / "current-session.db"
        seeded = _seed_branch(db_path, tmp_path / "current-session.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)
        projects_dir = tmp_path / "projects"
        live_transcript = projects_dir / "proj" / f"{seeded['session_uuid']}.jsonl"
        live_transcript.parent.mkdir(parents=True)
        (tmp_path / "README.md").write_text("", encoding="utf-8")
        _write_jsonl(
            live_transcript,
            [
                _entry(
                    seeded["user_uuid"],
                    None,
                    "2026-08-07T10:00:00Z",
                    "user",
                    "Investigate the worker bug",
                ),
                _entry(
                    seeded["assistant_uuid"],
                    seeded["user_uuid"],
                    "2026-08-07T10:00:01Z",
                    "assistant",
                    "I found the failing path",
                ),
            ],
        )

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE import_log SET file_hash = NULL, file_size = NULL, file_mtime = NULL WHERE file_path = ?",
                (str(seeded["transcript_path"]),),
            )
            conn.commit()

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        invoke_calls = {"count": 0}

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            invoke_calls["count"] += 1
            assert valid_file_paths == {"src/main.py"}
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        with sqlite3.connect(db_path) as conn:
            historical_status, historical_files = worker._resolve_source_files(
                conn.cursor(),
                session_uuid=seeded["session_uuid"],
                projects_dir=projects_dir,
            )
        assert historical_status == STATUS_SOURCE_UNVERIFIED
        assert historical_files == []

        assert (
            worker.run(
                capability_sidecar_path=sidecar,
                projects_dir=projects_dir,
                session=seeded["session_uuid"],
                current_session=True,
            )
            == worker.EXIT_OK
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT summary_enrichment_status, summary_enrichment_error FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()

        assert invoke_calls["count"] == 1
        assert row == (STATUS_OK, None)

    def test_current_session_run_falls_back_to_historical_source_when_unrelated_symlinked_project_exists(
        self, tmp_path, monkeypatch
    ):

        db_path = tmp_path / "current-session-unrelated-symlink.db"
        seeded = _seed_branch(db_path, tmp_path / "current-session-unrelated-symlink.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)
        projects_dir = tmp_path / "projects"
        real_project = tmp_path / "real-project"
        real_project.mkdir(parents=True)
        (real_project / f"{_uuid('other-session')}.jsonl").write_text("{}\n", encoding="utf-8")
        projects_dir.mkdir(parents=True)
        (projects_dir / "linked-project").symlink_to(real_project, target_is_directory=True)

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        invoke_calls = {"count": 0}

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            invoke_calls["count"] += 1
            assert valid_file_paths == {"src/main.py"}
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        assert (
            worker.run(
                capability_sidecar_path=sidecar,
                projects_dir=projects_dir,
                session=seeded["session_uuid"],
                current_session=True,
            )
            == worker.EXIT_OK
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT summary_enrichment_status, summary_enrichment_error FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()

        assert invoke_calls["count"] == 1
        assert row == (STATUS_OK, None)

    def test_current_session_run_rejects_matching_unsafe_live_candidate_instead_of_falling_back_to_historical(
        self, tmp_path, monkeypatch
    ):

        db_path = tmp_path / "current-session-matching-unsafe.db"
        seeded = _seed_branch(db_path, tmp_path / "current-session-matching-unsafe.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj"
        project_dir.mkdir(parents=True)
        (project_dir / f"{seeded['session_uuid']}.jsonl").write_text("{}\n", encoding="utf-8")
        subagents = project_dir / "state" / "subagents"
        subagents.mkdir(parents=True)
        (subagents / f"agent-{seeded['session_uuid']}.jsonl").mkdir()

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        invoke_calls = {"count": 0}

        def fake_invoke(*_args, **_kwargs):
            invoke_calls["count"] += 1
            return InvocationResult(status=STATUS_OK, response_body={})

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        assert (
            worker.run(
                capability_sidecar_path=sidecar,
                projects_dir=projects_dir,
                session=seeded["session_uuid"],
                current_session=True,
            )
            == worker.EXIT_OK
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT summary_enrichment_status FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()

        assert invoke_calls["count"] == 0
        assert row == ("unsafe_source_path",)

    def test_historical_resolution_without_import_log_does_not_scan_live_projects_tree(self, tmp_path):

        db_path = tmp_path / "no-import-log.db"
        seeded = _seed_branch(db_path, tmp_path / "no-import-log.jsonl")
        projects_dir = tmp_path / "projects"
        live_transcript = projects_dir / "proj" / f"{seeded['session_uuid']}.jsonl"
        live_transcript.parent.mkdir(parents=True)
        _write_jsonl(
            live_transcript,
            [
                _entry(seeded["user_uuid"], None, "2026-08-07T10:00:00Z", "user", "Investigate the worker bug"),
                _entry(
                    seeded["assistant_uuid"],
                    seeded["user_uuid"],
                    "2026-08-07T10:00:01Z",
                    "assistant",
                    "I found the failing path",
                ),
            ],
        )

        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM import_log WHERE file_path = ?", (str(seeded["transcript_path"]),))
            conn.commit()
            status, files = worker._resolve_source_files(
                conn.cursor(),
                session_uuid=seeded["session_uuid"],
                projects_dir=projects_dir,
            )

        assert status == STATUS_MISSING_SOURCE
        assert files == []

    def test_current_session_enrichment_accepts_branch_content_file_path_evidence(self, tmp_path, monkeypatch):

        db_path = tmp_path / "current-session-branch-file-evidence.db"
        seeded = _seed_branch(db_path, tmp_path / "current-session-branch-file-evidence.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)
        projects_dir = tmp_path / "projects"
        live_transcript = projects_dir / "proj" / f"{seeded['session_uuid']}.jsonl"
        live_transcript.parent.mkdir(parents=True)
        (live_transcript.parent / "config").mkdir()
        (live_transcript.parent / "docs").mkdir()
        (live_transcript.parent / "Dockerfile").write_text("", encoding="utf-8")
        (live_transcript.parent / "config" / ".gitignore").write_text("", encoding="utf-8")
        (live_transcript.parent / "docs" / "Makefile").write_text("", encoding="utf-8")
        _write_jsonl(
            live_transcript,
            [
                _entry(
                    seeded["user_uuid"],
                    None,
                    "2026-08-07T10:00:00Z",
                    "user",
                    "Investigate the worker bug",
                ),
                {
                    "uuid": seeded["assistant_uuid"],
                    "parentUuid": seeded["user_uuid"],
                    "type": "assistant",
                    "timestamp": "2026-08-07T10:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "I inspected the read-only file."},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "src/read_only.py"}},
                        ],
                    },
                },
            ],
        )

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            assert valid_file_paths == {"src/main.py", "src/read_only.py"}
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, "src/read_only.py"),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        assert (
            worker.run(
                capability_sidecar_path=sidecar,
                projects_dir=projects_dir,
                session=seeded["session_uuid"],
                current_session=True,
            )
            == worker.EXIT_OK
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT summary_enrichment_status, summary_enrichment_json FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()

        assert row[0] == STATUS_OK
        assert json.loads(row[1])["files_and_reasons"][0]["path"] == "src/read_only.py"

    def test_current_session_enrichment_accepts_prose_and_result_file_path_evidence(self, tmp_path, monkeypatch):

        db_path = tmp_path / "current-session-prose-result-file-evidence.db"
        seeded = _seed_branch(db_path, tmp_path / "current-session-prose-result-file-evidence.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)
        projects_dir = tmp_path / "projects"
        live_transcript = projects_dir / "proj" / f"{seeded['session_uuid']}.jsonl"
        live_transcript.parent.mkdir(parents=True)
        (live_transcript.parent / "README.md").write_text("", encoding="utf-8")
        _write_jsonl(
            live_transcript,
            [
                {
                    "uuid": seeded["user_uuid"],
                    "parentUuid": None,
                    "type": "user",
                    "timestamp": "2026-08-07T10:00:00Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Please verify `src/prose_only.py`."}],
                    },
                },
                {
                    "uuid": seeded["assistant_uuid"],
                    "parentUuid": seeded["user_uuid"],
                    "type": "assistant",
                    "timestamp": "2026-08-07T10:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "The generated note is in docs/result-notes.md."},
                            {
                                "type": "tool_result",
                                "content": [{"type": "text", "text": "Wrote logs/result.txt after the check."}],
                            },
                        ],
                    },
                },
            ],
        )

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            assert valid_file_paths == {"src/main.py", "src/prose_only.py", "docs/result-notes.md", "logs/result.txt"}
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, "docs/result-notes.md"),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        assert (
            worker.run(
                capability_sidecar_path=sidecar,
                projects_dir=projects_dir,
                session=seeded["session_uuid"],
                current_session=True,
            )
            == worker.EXIT_OK
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT summary_enrichment_status, summary_enrichment_json FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()

        assert row[0] == STATUS_OK
        assert json.loads(row[1])["files_and_reasons"][0]["path"] == "docs/result-notes.md"

    def test_current_session_enrichment_accepts_root_level_readme_from_prose_and_result_evidence(
        self, tmp_path, monkeypatch
    ):

        db_path = tmp_path / "current-session-root-readme-file-evidence.db"
        seeded = _seed_branch(db_path, tmp_path / "current-session-root-readme-file-evidence.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)
        projects_dir = tmp_path / "projects"
        live_transcript = projects_dir / "proj" / f"{seeded['session_uuid']}.jsonl"
        live_transcript.parent.mkdir(parents=True)
        (tmp_path / "README.md").write_text("", encoding="utf-8")
        _write_jsonl(
            live_transcript,
            [
                {
                    "uuid": seeded["user_uuid"],
                    "parentUuid": None,
                    "type": "user",
                    "timestamp": "2026-08-07T10:00:00Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Please confirm the `README.md` wording."}],
                    },
                },
                {
                    "uuid": seeded["assistant_uuid"],
                    "parentUuid": seeded["user_uuid"],
                    "type": "assistant",
                    "timestamp": "2026-08-07T10:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "I reviewed it."},
                            {
                                "type": "tool_result",
                                "content": [{"type": "text", "text": "Updated README.md after the check."}],
                            },
                        ],
                    },
                },
            ],
        )

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            assert valid_file_paths == {"README.md", "src/main.py"}
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, "README.md"),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        assert (
            worker.run(
                capability_sidecar_path=sidecar,
                projects_dir=projects_dir,
                session=seeded["session_uuid"],
                current_session=True,
            )
            == worker.EXIT_OK
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT summary_enrichment_status, summary_enrichment_json FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()

        assert row[0] == STATUS_OK
        assert json.loads(row[1])["files_and_reasons"][0]["path"] == "README.md"

    def test_current_session_enrichment_uses_project_root_for_root_level_file_evidence(self, tmp_path, monkeypatch):

        db_path = tmp_path / "current-session-project-root-file-evidence.db"
        seeded = _seed_branch(db_path, tmp_path / "current-session-project-root-file-evidence.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)
        projects_dir = tmp_path / "projects"
        live_transcript = projects_dir / "proj" / f"{seeded['session_uuid']}.jsonl"
        project_root = tmp_path / "repo"
        nested_cwd = project_root / "packages" / "app"
        live_transcript.parent.mkdir(parents=True)
        nested_cwd.mkdir(parents=True)
        (project_root / "README.md").write_text("", encoding="utf-8")
        _write_jsonl(
            live_transcript,
            [
                {
                    "uuid": seeded["user_uuid"],
                    "parentUuid": None,
                    "type": "user",
                    "timestamp": "2026-08-07T10:00:00Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Please confirm the `README.md` wording."}],
                    },
                },
                {
                    "uuid": seeded["assistant_uuid"],
                    "parentUuid": seeded["user_uuid"],
                    "type": "assistant",
                    "timestamp": "2026-08-07T10:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "I reviewed it."}],
                    },
                },
            ],
        )

        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE projects SET path = ?", (str(project_root),))
            conn.execute("UPDATE sessions SET cwd = ?", (str(nested_cwd),))
            conn.commit()

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            assert valid_file_paths == {"README.md", "src/main.py"}
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, "README.md"),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        assert (
            worker.run(
                capability_sidecar_path=sidecar,
                projects_dir=projects_dir,
                session=seeded["session_uuid"],
                current_session=True,
            )
            == worker.EXIT_OK
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT summary_enrichment_status, summary_enrichment_json FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()

        assert row[0] == STATUS_OK
        assert json.loads(row[1])["files_and_reasons"][0]["path"] == "README.md"

    def test_current_session_enrichment_rejects_directory_like_slash_paths_but_keeps_tool_inputs(
        self, tmp_path, monkeypatch
    ):

        db_path = tmp_path / "current-session-extensionless-file-evidence.db"
        seeded = _seed_branch(db_path, tmp_path / "current-session-extensionless-file-evidence.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)
        projects_dir = tmp_path / "projects"
        live_transcript = projects_dir / "proj" / f"{seeded['session_uuid']}.jsonl"
        live_transcript.parent.mkdir(parents=True)
        _write_jsonl(
            live_transcript,
            [
                {
                    "uuid": seeded["user_uuid"],
                    "parentUuid": None,
                    "type": "user",
                    "timestamp": "2026-08-07T10:00:00Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Please verify Dockerfile and config/.gitignore."}],
                    },
                },
                {
                    "uuid": seeded["assistant_uuid"],
                    "parentUuid": seeded["user_uuid"],
                    "type": "assistant",
                    "timestamp": "2026-08-07T10:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "I checked docs/Makefile, ignored and/or, and did not treat docs/v2 as a file.",
                            },
                            {"type": "tool_use", "name": "Glob", "input": {"path": "src"}},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "ops/Justfile"}},
                            {
                                "type": "tool_result",
                                "content": [{"type": "text", "text": "Updated Dockerfile after the check."}],
                            },
                        ],
                    },
                },
            ],
        )

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            assert valid_file_paths == {
                "Dockerfile",
                "config/.gitignore",
                "docs/Makefile",
                "ops/Justfile",
                "src/main.py",
            }
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, "Dockerfile"),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        assert (
            worker.run(
                capability_sidecar_path=sidecar,
                projects_dir=projects_dir,
                session=seeded["session_uuid"],
                current_session=True,
            )
            == worker.EXIT_OK
        )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT summary_enrichment_status, summary_enrichment_json FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()

        assert row[0] == STATUS_OK
        assert json.loads(row[1])["files_and_reasons"][0]["path"] == "Dockerfile"

    def test_current_session_run_stops_after_first_selection_page(self, tmp_path, monkeypatch):

        db_path = tmp_path / "current-session-loop-exit.db"
        seeded = _seed_branch(db_path, tmp_path / "current-session-loop-exit.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

        selection_calls: list[int | None] = []
        processed_ids: list[int] = []

        def fake_select_branch_ids(
            _cursor,
            *,
            days,
            limit,
            session_uuid,
            after_branch_id,
            force,
            min_exchanges,
        ):
            assert days is None
            assert limit == 1
            assert session_uuid == seeded["session_uuid"]
            assert force is False
            assert min_exchanges == seeded["settings"]["llm_summary_min_exchanges"]
            selection_calls.append(after_branch_id)
            if after_branch_id is None:
                return [seeded["branch_id"]]
            return [seeded["branch_id"] + 1]

        def fake_process_branch(branch_id, **_kwargs):
            processed_ids.append(branch_id)
            return True

        monkeypatch.setattr(worker, "_select_branch_ids", fake_select_branch_ids)
        monkeypatch.setattr(worker, "_process_branch", fake_process_branch)

        assert (
            worker.run(
                capability_sidecar_path=sidecar,
                projects_dir=tmp_path / "projects",
                current_session=True,
                session=seeded["session_uuid"],
            )
            == worker.EXIT_OK
        )
        assert selection_calls == [None]
        assert processed_ids == [seeded["branch_id"]]

    def test_unexpected_exception_writes_generic_error_status(self, tmp_path, monkeypatch):

        db_path = tmp_path / "unexpected-error.db"
        seeded = _seed_branch(db_path, tmp_path / "unexpected-error.jsonl")
        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        monkeypatch.setattr(
            worker, "invoke_claude", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT summary_enrichment_status, summary_enrichment_error FROM branches WHERE id = ?",
                (seeded["branch_id"],),
            ).fetchone()

        assert row == (STATUS_ERROR, "boom")

    def test_source_hash_change_during_claude_discards_success_and_failure_status_writes(
        self, tmp_path, monkeypatch, capsys
    ):

        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        for run_status in (STATUS_OK, STATUS_INVALID_OUTPUT):
            db_path = tmp_path / f"stale-{run_status}.db"
            seeded = _seed_branch(db_path, tmp_path / f"stale-{run_status}.jsonl")

            monkeypatch.setattr(worker, "load_settings", lambda seeded=seeded: seeded["settings"])
            monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
            monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
            monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")

            def fake_invoke(
                _packet_dir,
                _settings,
                _prompt,
                *,
                active_branch_uuids,
                valid_file_paths,
                run=None,
                db_path=db_path,
                seeded=seeded,
                run_status=run_status,
            ):
                with sqlite3.connect(db_path) as conn:
                    conn.execute(
                        "UPDATE branches SET summary_source_hash = ?, summary_enrichment_status = ? WHERE id = ?",
                        ("newer-hash", STATUS_OK, seeded["branch_id"]),
                    )
                    conn.commit()
                if run_status == STATUS_OK:
                    return InvocationResult(
                        status=STATUS_OK,
                        response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
                    )
                return InvocationResult(status=run_status, diagnostic="stale failure")

            monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

            worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

            assert "complete: 0 branches enriched" in capsys.readouterr().out

            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT summary_source_hash, summary_enrichment_status, summary_enrichment_json FROM branches WHERE id = ?",
                    (seeded["branch_id"],),
                ).fetchone()

            assert row == ("newer-hash", STATUS_OK, None)

    def test_migrated_current_row_gets_canonical_hash_before_packet_and_invalid_state_skips_claude(
        self, tmp_path, monkeypatch
    ):

        sidecar = tmp_path / "capability.json"
        _write_ok_sidecar(sidecar)

        db_path = tmp_path / "migrated.db"
        seeded = _seed_branch(db_path, tmp_path / "migrated.jsonl", summary_source_hash=None)

        monkeypatch.setattr(worker, "load_settings", lambda: seeded["settings"])
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "get_claude_version", lambda run=None: ("1.2.3", None))
        monkeypatch.setattr(worker, "capability_fingerprint", lambda: "fingerprint")
        invoke_calls = {"count": 0}

        def fake_invoke(_packet_dir, _settings, _prompt, *, active_branch_uuids, valid_file_paths, run=None):
            invoke_calls["count"] += 1
            return InvocationResult(
                status=STATUS_OK,
                response_body=_response_body(active_branch_uuids, next(iter(valid_file_paths))),
            )

        monkeypatch.setattr(worker, "invoke_claude", fake_invoke)

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT b.leaf_uuid, b.summary_version, b.context_summary_json, b.aggregated_content,
                       b.exchange_count, b.started_at, b.ended_at, b.files_modified,
                       b.tool_counts, b.commits, s.git_branch, b.summary_source_hash
                FROM branches b
                JOIN sessions s ON s.id = b.session_id
                WHERE b.id = ?
                """,
                (seeded["branch_id"],),
            ).fetchone()

        assert row is not None
        assert invoke_calls["count"] == 1
        assert row[11] == compute_summary_source_hash(
            {
                "leaf_uuid": row[0],
                "summary_version": row[1],
                "context_summary_json": row[2],
                "aggregated_content": row[3],
                "exchange_count": row[4],
                "started_at": row[5],
                "ended_at": row[6],
                "files_modified": row[7],
                "tool_counts": row[8],
                "commits": row[9],
                "git_branch": row[10],
            }
        )

        invalid_db = tmp_path / "invalid.db"
        invalid_seeded = _seed_branch(
            invalid_db,
            tmp_path / "invalid.jsonl",
            summary_source_hash=None,
            context_summary_json="not-json",
        )
        monkeypatch.setattr(worker, "load_settings", lambda: invalid_seeded["settings"])
        invoke_calls["count"] = 0

        worker.run(capability_sidecar_path=sidecar, projects_dir=tmp_path / "projects")

        with sqlite3.connect(invalid_db) as conn:
            row = conn.execute(
                "SELECT summary_source_hash, summary_enrichment_status FROM branches WHERE id = ?",
                (invalid_seeded["branch_id"],),
            ).fetchone()

        assert invoke_calls["count"] == 0
        assert row == (None, None)

    def test_pid_guard_skips_live_worker_reaps_stale_marker_and_cleans_up_on_success_and_failure(
        self, tmp_path, monkeypatch
    ):

        removed: list[str] = []
        monkeypatch.setattr(worker, "load_settings", lambda: {"db_path": str(tmp_path / "unused.db")})
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "remove_pid_file", lambda key: removed.append(key))
        monkeypatch.setattr(worker, "try_acquire_pid_file", lambda _key: False)

        assert worker.run() == worker.EXIT_OK
        assert removed == []

        stale_marker = pid_file_path(worker.PID_KEY)
        stale_marker.parent.mkdir(parents=True, exist_ok=True)
        stale_marker.write_text("999999", encoding="utf-8")
        monkeypatch.undo()

        monkeypatch.setattr(worker, "load_settings", lambda: {"db_path": str(tmp_path / "unused.db")})
        monkeypatch.setattr(worker, "setup_logging", lambda *_args, **_kwargs: logging.getLogger("test-worker"))
        monkeypatch.setattr(worker, "_run", lambda **_kwargs: worker.EXIT_OK)

        assert worker.run() == worker.EXIT_OK
        assert not stale_marker.exists()

        cleanup_calls: list[str] = []
        monkeypatch.setattr(worker, "remove_pid_file", lambda key: cleanup_calls.append(key))
        monkeypatch.setattr(worker, "try_acquire_pid_file", lambda _key: True)
        monkeypatch.setattr(worker, "_run", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

        with pytest.raises(RuntimeError, match="boom"):
            worker.run()

        assert cleanup_calls == [worker.PID_KEY]
