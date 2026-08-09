import sys
from unittest.mock import patch

import pytest

from ccrecall.cli.commands import cmd_backfill_llm_summaries
from ccrecall.config import DEFAULT_SETTINGS
from ccrecall.hooks.backfill_llm_summaries import EXIT_OK


class TestLlmSummaryCli:
    def test_manual_command_delegates_selected_session_even_when_auto_enrichment_is_disabled(self):
        with (
            patch(
                "ccrecall.cli.commands.backfill_llm_summaries_mod.run",
                return_value=EXIT_OK,
            ) as mock_run,
            patch("ccrecall.cli.commands.backfill_llm_summaries_mod.run_capability_check") as mock_check,
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_backfill_llm_summaries(session="session-123", force=True)

        assert exc_info.value.code == EXIT_OK
        mock_check.assert_not_called()
        mock_run.assert_called_once_with(
            days=None,
            limit=None,
            session="session-123",
            force=True,
            verbose=False,
            current_session=False,
        )

    def test_capability_check_delegates_without_running_worker(self):
        with (
            patch("ccrecall.cli.commands.backfill_llm_summaries_mod.run") as mock_run,
            patch(
                "ccrecall.cli.commands.backfill_llm_summaries_mod.run_capability_check",
                return_value=EXIT_OK,
            ) as mock_check,
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_backfill_llm_summaries(check_capability=True)

        assert exc_info.value.code == EXIT_OK
        mock_run.assert_not_called()
        mock_check.assert_called_once_with(verbose=False)

    def test_help_discloses_opt_in_data_sharing_and_budget_threshold_behavior(self, capsys):
        from ccrecall.cli import main

        argv = sys.argv
        try:
            sys.argv = ["ccrecall", "backfill", "llm-summaries", "--help"]
            with pytest.raises(SystemExit) as exc_info:
                main()
        finally:
            sys.argv = argv

        assert exc_info.value.code == 0
        output = capsys.readouterr().out
        assert "selected local transcript content" in output
        assert "branch/session metadata" in output
        assert "source-path provenance" in output
        assert "Claude Code auth" in output
        assert f"${DEFAULT_SETTINGS['llm_summary_max_budget_usd']:.2f}" in output
        assert "budget stop" in output
        assert "threshold" in output
        assert "not a guaranteed maximum" in output
        assert "charge" in output

    def test_check_capability_is_mutually_exclusive_with_selectors(self, capsys):
        from ccrecall.cli import main

        argv = sys.argv
        try:
            sys.argv = ["ccrecall", "backfill", "llm-summaries", "--check-capability", "--days", "3"]
            with pytest.raises(SystemExit) as exc_info:
                main()
        finally:
            sys.argv = argv

        assert exc_info.value.code == 2
        assert "mutually exclusive" in capsys.readouterr().err

    def test_direct_call_reuses_same_check_capability_exclusivity_guard(self, capsys):
        with (
            patch("ccrecall.cli.commands.backfill_llm_summaries_mod.run") as mock_run,
            patch("ccrecall.cli.commands.backfill_llm_summaries_mod.run_capability_check") as mock_check,
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_backfill_llm_summaries(check_capability=True, session="session-123")

        assert exc_info.value.code == 2
        mock_run.assert_not_called()
        mock_check.assert_not_called()
        assert (
            "--check-capability is mutually exclusive with --days, --limit, --session, and --force"
            in capsys.readouterr().err
        )

    def test_help_shows_canonical_flags(self, capsys):
        from ccrecall.cli import main

        argv = sys.argv
        try:
            sys.argv = ["ccrecall", "backfill", "llm-summaries", "--help"]
            with pytest.raises(SystemExit):
                main()
        finally:
            sys.argv = argv

        output = capsys.readouterr().out
        assert "--days" in output
        assert "--limit" in output
        assert "--session" in output
        assert "--force" in output
        assert "--check-capability" in output
        assert "Process at most N eligible branch candidates" in output
