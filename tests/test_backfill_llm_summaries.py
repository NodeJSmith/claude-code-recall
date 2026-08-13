import sys
from unittest.mock import patch

import pytest

from ccrecall.hooks import backfill_llm_summaries as worker


class TestBackfillLlmSummaries:
    def test_worker_import_stays_lightweight(self):
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

    def test_run_is_inert_and_releases_its_pid_guard(self):
        with (
            patch("ccrecall.hooks.backfill_llm_summaries.try_acquire_pid_file", return_value=True),
            patch("ccrecall.hooks.backfill_llm_summaries.remove_pid_file") as remove,
        ):
            assert worker.run(days=1, limit=1, session="session", current_session=True, force=True) == worker.EXIT_OK
        remove.assert_called_once_with(worker.PID_KEY)

    def test_contention_is_a_successful_noop(self):
        with patch("ccrecall.hooks.backfill_llm_summaries.try_acquire_pid_file", return_value=False):
            assert worker.run() == worker.EXIT_OK

    @pytest.mark.parametrize("argv", [["--days", "0"], ["--limit", "0"], ["--current-session"]])
    def test_cli_preserves_selector_validation(self, argv):
        with pytest.raises(SystemExit) as error:
            worker.main(argv)
        assert error.value.code == 2
