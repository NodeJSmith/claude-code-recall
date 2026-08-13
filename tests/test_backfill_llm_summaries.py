import json
import sys
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from ccrecall.hooks import backfill_llm_summaries as worker
from ccrecall.llm_summary_db import get_connection
from ccrecall.recap_state import prune_retention


class TestBackfillLlmSummaries:
    def test_worker_import_stays_outside_provider_boundary(self):
        probe = """
import sys
import ccrecall.hooks.backfill_llm_summaries
assert 'ccrecall.db' not in sys.modules
assert 'ccrecall.llm_summarizer' not in sys.modules
"""
        completed = __import__("subprocess").run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=30, check=False
        )
        assert completed.returncode == 0, completed.stderr

    def test_run_releases_its_pid_guard_when_schema_is_unavailable(self, monkeypatch):
        @contextmanager
        def unavailable_connection(_settings):
            yield object()

        monkeypatch.setattr(worker, "get_connection", unavailable_connection)
        monkeypatch.setattr(worker, "recap_schema_capability", lambda _conn: "unavailable")
        with (
            patch("ccrecall.hooks.backfill_llm_summaries.try_acquire_pid_file", return_value=True),
            patch("ccrecall.hooks.backfill_llm_summaries.remove_pid_file") as remove,
        ):
            assert worker.run(days=1, limit=1, session="session", current_session=True) == worker.EXIT_OK
        remove.assert_called_once_with(worker.PID_KEY)

    def test_contention_is_a_successful_noop(self):
        with patch("ccrecall.hooks.backfill_llm_summaries.try_acquire_pid_file", return_value=False):
            assert worker.run() == worker.EXIT_OK

    @pytest.mark.parametrize("json_mode", [False, True])
    def test_unsupported_platform_blocks_manual_run_without_invoking_drainer(
        self, tmp_path, monkeypatch, capsys, json_mode
    ):
        settings = {"db_path": str(tmp_path / "recaps.db")}
        with get_connection(settings) as conn:
            conn.execute("INSERT INTO sessions(uuid) VALUES ('session')")
            conn.execute(
                "INSERT INTO branches(session_id, leaf_uuid, is_active, recap_input_hash, files_modified) "
                "VALUES (1, 'leaf', 1, 'input', '[\"changed.py\"]')"
            )
        monkeypatch.setattr(worker, "load_settings", lambda: settings)
        monkeypatch.setattr(worker, "posix_process_groups_supported", lambda: False)
        monkeypatch.setattr(
            worker, "evaluate_branch", lambda _cursor, _branch_id: type("Decision", (), {"eligible": True})()
        )
        with (
            patch("ccrecall.hooks.backfill_llm_summaries.try_acquire_pid_file", return_value=True),
            patch("ccrecall.hooks.backfill_llm_summaries.subprocess.run") as drain,
        ):
            assert worker.run(session="session", retry_failures=True, json_mode=json_mode) == worker.EXIT_OK
        drain.assert_not_called()
        with get_connection(settings) as conn:
            assert conn.execute("SELECT state, reason FROM session_recap_jobs").fetchone() == (
                "blocked",
                "platform_unsupported",
            )
            assert conn.execute("SELECT state FROM session_recap_runs").fetchone() == ("complete",)
        output = capsys.readouterr().out
        if json_mode:
            result = json.loads(output)
            assert result["provider_work_disabled"] is True
            assert result["provider_work_reason"] == "platform_unsupported"
            assert result["session_recap_run"]["final_dispositions"] == {"platform_unsupported": 1}
        else:
            assert "provider work disabled: platform_unsupported" in output

    @pytest.mark.parametrize("argv", [["--days", "0"], ["--limit", "0"], ["--current-session"]])
    def test_cli_preserves_selector_validation(self, argv):
        with pytest.raises(SystemExit) as error:
            worker.main(argv)
        assert error.value.code == 2

    def test_compatibility_entry_point_uses_retry_failures_name(self):
        with patch.object(worker, "run", return_value=worker.EXIT_OK) as run:
            assert worker.main(["--retry-failures"]) == worker.EXIT_OK
        assert run.call_args.kwargs["retry_failures"] is True

    def test_compatibility_entry_point_rejects_old_retry_name(self):
        with pytest.raises(SystemExit) as error:
            worker.main(["--retry"])
        assert error.value.code == 2

    def test_retention_prunes_run_candidates_before_runs(self, tmp_path):
        settings = {"db_path": str(tmp_path / "recaps.db")}
        with get_connection(settings) as conn:
            conn.execute("INSERT INTO sessions(uuid) VALUES ('session')")
            run_id = conn.execute(
                "INSERT INTO session_recap_runs(trigger, started_at, state) VALUES ('manual', '2000-01-01T00:00:00Z', 'complete')"
            ).lastrowid
            conn.execute(
                "INSERT INTO session_recap_run_candidates(run_id, session_id, initial_disposition) VALUES (?, 1, 'pending')",
                (run_id,),
            )
            assert prune_retention(conn, "2020-01-01T00:00:00Z", limit=10) == (0, 1)
            assert conn.execute("SELECT COUNT(*) FROM session_recap_runs").fetchone()[0] == 0

    def test_retention_prunes_old_completed_run_attempt_with_its_candidate(self, tmp_path):
        settings = {"db_path": str(tmp_path / "recaps.db")}
        with get_connection(settings) as conn:
            conn.execute("INSERT INTO sessions(uuid) VALUES ('session')")
            conn.execute(
                "INSERT INTO session_recap_jobs(session_id, requested_input_hash, trigger, state, requested_at, updated_at, retry_lineage) "
                "VALUES (1, 'new', 'manual', 'current', '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z', 1)"
            )
            attempt_id = conn.execute(
                "INSERT INTO session_recap_attempts(session_id, job_session_id, input_hash, input_contract_version, policy_version, "
                "recap_contract_version, claim_token, trigger, state, created_at, finished_at, retry_lineage) "
                "VALUES (1, 1, 'old', 1, 1, 2, 1, 'manual', 'timeout', '2000-01-01T00:00:00Z', "
                "'2000-01-01T00:00:00Z', 0) RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO session_recap_attempts(session_id, job_session_id, input_hash, input_contract_version, policy_version, "
                "recap_contract_version, claim_token, trigger, state, created_at, finished_at, retry_lineage) "
                "VALUES (1, 1, 'new', 1, 1, 2, 2, 'manual', 'succeeded', '2020-01-01T00:00:00Z', "
                "'2020-01-01T00:00:00Z', 1)"
            )
            run_id = conn.execute(
                "INSERT INTO session_recap_runs(trigger, started_at, state) VALUES ('manual', '2000-01-01T00:00:00Z', 'complete')"
            ).lastrowid
            conn.execute(
                "INSERT INTO session_recap_run_candidates(run_id, session_id, initial_disposition, started_attempt_id) "
                "VALUES (?, 1, 'pending', ?)",
                (run_id, attempt_id),
            )

            assert prune_retention(conn, "2020-01-01T00:00:01Z", limit=10) == (1, 1)
            assert conn.execute("SELECT COUNT(*) FROM session_recap_attempts").fetchone() == (1,)
            assert conn.execute("SELECT COUNT(*) FROM session_recap_run_candidates").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM session_recap_runs").fetchone() == (0,)
