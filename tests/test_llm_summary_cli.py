import json
import sys
from unittest.mock import patch

import pytest

from ccrecall.cli import main
from ccrecall.cli.commands import (
    cmd_backfill_llm_summaries,
    cmd_recap_maintain,
    cmd_recap_recover,
    cmd_recap_reset_health,
)
from ccrecall.cli.context import CLIContext
from ccrecall.hooks.backfill_llm_summaries import EXIT_OK


class TestLlmSummaryCli:
    def test_compatibility_command_forwards_selectors_and_global_json(self):
        with (
            patch("ccrecall.cli.commands.backfill_llm_summaries_mod.run", return_value=EXIT_OK) as run,
            pytest.raises(SystemExit) as error,
        ):
            cmd_backfill_llm_summaries(days=3, limit=2, session="session-123")
        assert error.value.code == EXIT_OK
        run.assert_called_once_with(
            days=3, limit=2, session="session-123", verbose=False, current_session=False, json_mode=False
        )

    def test_retry_failures_forwards_without_broad_force(self):
        with (
            patch("ccrecall.cli.commands.backfill_llm_summaries_mod.run", return_value=EXIT_OK) as run,
            pytest.raises(SystemExit),
        ):
            cmd_backfill_llm_summaries(session="session-123", retry_failures=True)
        assert run.call_args.kwargs["retry_failures"] is True
        assert "force" not in run.call_args.kwargs

    def test_recover_calls_shared_drainer_without_manual_selection(self):
        with (
            patch("ccrecall.cli.commands.drain_session_recaps_mod.run", return_value=EXIT_OK) as drain,
            patch("ccrecall.cli.commands.backfill_llm_summaries_mod.run") as select,
            pytest.raises(SystemExit) as error,
        ):
            cmd_recap_recover()
        assert error.value.code == EXIT_OK
        drain.assert_called_once_with()
        select.assert_not_called()

    def test_reset_health_clears_only_provider_health(self):
        with patch("ccrecall.cli.commands.get_recap_connection") as connection:
            connection.return_value.__enter__.return_value = object()
            with patch("ccrecall.cli.commands.reset_health") as reset:
                cmd_recap_reset_health()
        reset.assert_called_once_with(connection.return_value.__enter__.return_value)

    def test_help_describes_the_compatibility_command_without_legacy_capability_flags(self, capsys):
        argv = sys.argv
        try:
            sys.argv = ["ccrecall", "backfill", "llm-summaries", "--help"]
            with pytest.raises(SystemExit) as error:
                main()
        finally:
            sys.argv = argv
        assert error.value.code == 0
        output = capsys.readouterr().out
        assert "--days" in output
        assert "--limit" in output
        assert "--session" in output
        assert "--retry-failures" in output
        assert "--retry " not in output
        assert "--check-capability" not in output
        assert "--force" not in output

    def test_recap_maintain_uses_global_json_context(self, capsys):
        with patch("ccrecall.cli.commands.get_recap_connection") as connection:
            connection.return_value.__enter__.return_value = object()
            # The preview counts through the real prune ordering now, so it is
            # one call rather than two independent candidate queries.
            with patch("ccrecall.cli.commands.preview_retention", return_value=(2, 1)):
                cmd_recap_maintain(ctx=CLIContext(json_mode=True))
        assert json.loads(capsys.readouterr().out) == {
            "session_recap_maintenance": {"attempts": 2, "pruned": False, "runs": 1}
        }

    def test_recap_maintain_retains_human_output(self, capsys):
        with patch("ccrecall.cli.commands.get_recap_connection") as connection:
            connection.return_value.__enter__.return_value = object()
            with patch("ccrecall.cli.commands.preview_retention", return_value=(1, 0)):
                cmd_recap_maintain()
        assert capsys.readouterr().out == "Session Recap maintenance: 1 attempts, 0 runs would prune\n"
