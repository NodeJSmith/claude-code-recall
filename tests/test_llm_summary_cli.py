import sys
from unittest.mock import patch

import pytest

from ccrecall.cli import main
from ccrecall.cli.commands import cmd_backfill_llm_summaries
from ccrecall.hooks.backfill_llm_summaries import EXIT_OK


class TestLlmSummaryCli:
    def test_compatibility_command_forwards_selectors_to_the_inert_worker(self):
        with (
            patch("ccrecall.cli.commands.backfill_llm_summaries_mod.run", return_value=EXIT_OK) as run,
            pytest.raises(SystemExit) as error,
        ):
            cmd_backfill_llm_summaries(days=3, limit=2, session="session-123")
        assert error.value.code == EXIT_OK
        run.assert_called_once_with(days=3, limit=2, session="session-123", verbose=False, current_session=False)

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
        assert "--check-capability" not in output
        assert "--force" not in output
