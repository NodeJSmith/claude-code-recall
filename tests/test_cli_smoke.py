"""Smoke tests for CLI command wiring: argument parsing -> module dispatch.

These verify each cyclopts command function calls its owning module's run()
with the arguments the CLI would parse -- not the underlying logic, which has
its own tests. cmd_backfill_embeddings is covered separately in
test_backfill_embeddings.py and is not repeated here.
"""

from unittest.mock import patch

import pytest
from conftest import stamp_user_version
from cyclopts.exceptions import ValidationError

from ccrecall.cli import app, launcher
from ccrecall.cli.commands import (
    cmd_backfill_summaries,
    cmd_backfill_tool_content,
    cmd_import,
    cmd_recent,
    cmd_search,
    cmd_search_messages,
    cmd_status,
    cmd_sync_current,
    cmd_tail,
)
from ccrecall.cli.context import DEFAULT_CLI_CONTEXT
from ccrecall.config import DEFAULT_DB_PATH
from ccrecall.db import DEFAULT_PROJECTS_DIR, SCHEMA_VERSION
from ccrecall.hooks import backfill_query as backfill_query_mod
from ccrecall.session_tail import DEFAULT_TAIL_EVENTS


class TestSchemaAheadWarning:
    """launcher warns on stderr, before dispatch, when the database is ahead of this code."""

    def _run_import_against(self, tmp_path, user_version: int) -> int:
        db_path = tmp_path / "conversations.db"
        stamp_user_version(db_path, user_version)
        with (
            patch("ccrecall.config.load_settings", return_value={"db_path": str(db_path)}),
            patch("ccrecall.cli.commands.import_mod.run") as mock_run,
        ):
            launcher("import")
        return mock_run.call_count

    def test_ahead_database_warns_on_stderr_and_still_runs_the_command(self, tmp_path, capsys):
        calls = self._run_import_against(tmp_path, SCHEMA_VERSION + 1)

        captured = capsys.readouterr()
        assert f"schema v{SCHEMA_VERSION + 1}" in captured.err
        assert "upgrade ccrecall" in captured.err.lower()
        assert "schema v" not in captured.out
        assert calls == 1

    def test_current_database_prints_no_warning(self, tmp_path, capsys):
        self._run_import_against(tmp_path, SCHEMA_VERSION)

        assert "upgrade ccrecall" not in capsys.readouterr().err.lower()

    def test_checks_the_database_named_by_db_not_the_configured_one(self, tmp_path, capsys):
        configured = tmp_path / "configured.db"
        stamp_user_version(configured, SCHEMA_VERSION)
        ahead = tmp_path / "ahead.db"
        stamp_user_version(ahead, SCHEMA_VERSION + 1)
        with (
            patch("ccrecall.config.load_settings", return_value={"db_path": str(configured)}),
            patch("ccrecall.cli.commands.import_mod.run"),
        ):
            launcher("import", "--db", str(ahead))

        assert f"schema v{SCHEMA_VERSION + 1}" in capsys.readouterr().err


class TestCmdSyncCurrent:
    def test_calls_run_with_input_file(self):
        with patch("ccrecall.cli.commands.sync_current_mod.run") as mock_run:
            cmd_sync_current(input_file=None)
        mock_run.assert_called_once_with(None)


class TestCmdImport:
    def test_calls_run_with_parsed_arguments(self):
        with patch("ccrecall.cli.commands.import_mod.run") as mock_run:
            cmd_import(
                db=DEFAULT_DB_PATH,
                projects_dir=DEFAULT_PROJECTS_DIR,
                project=None,
                ctx=DEFAULT_CLI_CONTEXT,
            )
        mock_run.assert_called_once_with(
            db=DEFAULT_DB_PATH,
            projects_dir=DEFAULT_PROJECTS_DIR,
            project=None,
            verbose=False,
            repair_gaps=False,
        )

    def test_calls_run_with_repair_gaps(self):
        with patch("ccrecall.cli.commands.import_mod.run") as mock_run:
            cmd_import(
                db=DEFAULT_DB_PATH,
                projects_dir=DEFAULT_PROJECTS_DIR,
                project=None,
                repair_gaps=True,
                ctx=DEFAULT_CLI_CONTEXT,
            )
        mock_run.assert_called_once_with(
            db=DEFAULT_DB_PATH,
            projects_dir=DEFAULT_PROJECTS_DIR,
            project=None,
            verbose=False,
            repair_gaps=True,
        )


class TestCmdStatus:
    def test_calls_run_with_parsed_arguments(self):
        with patch("ccrecall.cli.commands.status_mod.run") as mock_run:
            cmd_status(db=DEFAULT_DB_PATH, days=None, check_ingestion=False, ctx=DEFAULT_CLI_CONTEXT)
        mock_run.assert_called_once_with(
            db=DEFAULT_DB_PATH,
            days=None,
            check_ingestion=False,
            output_format="markdown",
        )


class TestCmdBackfillSummaries:
    def test_calls_run_with_verbose_and_exits_with_return_code(self):
        with (
            patch("ccrecall.cli.commands.backfill_summaries_mod.run", return_value=0) as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_backfill_summaries(ctx=DEFAULT_CLI_CONTEXT)
        assert exc_info.value.code == 0
        mock_run.assert_called_once_with(verbose=False, db=DEFAULT_DB_PATH)


class TestCmdBackfillToolContent:
    def test_calls_run_with_parsed_arguments_and_exits_with_return_code(self):
        with (
            patch("ccrecall.cli.commands.backfill_tool_content_mod.run", return_value=0) as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_backfill_tool_content(
                status=False,
                days=None,
                limit=None,
                progress_every=backfill_query_mod.DEFAULT_PROGRESS_EVERY,
                ctx=DEFAULT_CLI_CONTEXT,
            )
        assert exc_info.value.code == 0
        mock_run.assert_called_once_with(
            status=False,
            json_mode=False,
            days=None,
            limit=None,
            progress_every=backfill_query_mod.DEFAULT_PROGRESS_EVERY,
            verbose=False,
            db=DEFAULT_DB_PATH,
        )


class TestCmdRecent:
    def test_calls_run_with_parsed_arguments(self):
        with patch("ccrecall.cli.commands.recent_chats_mod.run") as mock_run:
            cmd_recent(
                limit=3,
                sort_order="desc",
                before=None,
                after=None,
                session=None,
                project=None,
                path=None,
                verbose=False,
                include_notifications=False,
                db=DEFAULT_DB_PATH,
                ctx=DEFAULT_CLI_CONTEXT,
            )
        mock_run.assert_called_once_with(
            n=3,
            sort_order="desc",
            before=None,
            after=None,
            session=None,
            project=None,
            path=None,
            output_format="markdown",
            verbose=False,
            include_notifications=False,
            db=DEFAULT_DB_PATH,
        )


class TestCmdSearch:
    def test_calls_run_with_parsed_arguments(self):
        with patch("ccrecall.cli.commands.search_mod.run") as mock_run:
            cmd_search(
                query="test query",
                status=False,
                keyword_only=False,
                max_results=5,
                session=None,
                project=None,
                path=None,
                before=None,
                after=None,
                verbose=False,
                include_notifications=False,
                db=DEFAULT_DB_PATH,
                ctx=DEFAULT_CLI_CONTEXT,
            )
        mock_run.assert_called_once_with(
            query="test query",
            status=False,
            keyword_only=False,
            max_results=5,
            session=None,
            project=None,
            path=None,
            before=None,
            after=None,
            output_format="markdown",
            verbose=False,
            include_notifications=False,
            db=DEFAULT_DB_PATH,
        )

    def test_positional_query_resolves_identically_to_flag(self):
        """`cmd_search("q")` (positional) must dispatch the same query as `-q`."""
        with patch("ccrecall.cli.commands.search_mod.run") as mock_run:
            cmd_search(
                "test query",
                status=False,
                keyword_only=False,
                max_results=5,
                session=None,
                project=None,
                path=None,
                before=None,
                after=None,
                verbose=False,
                include_notifications=False,
                db=DEFAULT_DB_PATH,
                ctx=DEFAULT_CLI_CONTEXT,
            )
        mock_run.assert_called_once_with(
            query="test query",
            status=False,
            keyword_only=False,
            max_results=5,
            session=None,
            project=None,
            path=None,
            before=None,
            after=None,
            output_format="markdown",
            verbose=False,
            include_notifications=False,
            db=DEFAULT_DB_PATH,
        )

    def test_app_accepts_positional_query(self):
        """End-to-end: `ccrecall search "q"` parses and dispatches like `-q "q"`."""
        with patch("ccrecall.cli.commands.search_mod.run") as mock_run, pytest.raises(SystemExit) as exc_info:
            app(["search", "auth bug"], exit_on_error=False, print_error=False)
        assert exc_info.value.code == 0
        assert mock_run.call_args.kwargs["query"] == "auth bug"

    def test_app_accepts_flagged_query(self):
        with patch("ccrecall.cli.commands.search_mod.run") as mock_run, pytest.raises(SystemExit) as exc_info:
            app(["search", "-q", "auth bug"], exit_on_error=False, print_error=False)
        assert exc_info.value.code == 0
        assert mock_run.call_args.kwargs["query"] == "auth bug"

    def test_app_rejects_both_positional_and_flag(self):
        with pytest.raises(ValidationError, match="cannot both be given"):
            app(["search", "positional query", "-q", "flagged query"], exit_on_error=False, print_error=False)

    def test_app_rejects_no_query_and_no_status(self):
        with pytest.raises(ValidationError, match="a query is required"):
            app(["search"], exit_on_error=False, print_error=False)

    def test_app_accepts_max_results_of_20(self):
        with patch("ccrecall.cli.commands.search_mod.run") as mock_run, pytest.raises(SystemExit) as exc_info:
            app(["search", "-q", "test", "-n", "20"], exit_on_error=False, print_error=False)
        assert exc_info.value.code == 0
        assert mock_run.call_args.kwargs["max_results"] == 20

    def test_app_rejects_max_results_above_20(self):
        with pytest.raises(ValidationError):
            app(["search", "-q", "test", "-n", "21"], exit_on_error=False, print_error=False)


class TestCmdSearchMessages:
    def test_calls_run_messages_with_parsed_arguments(self):
        with patch("ccrecall.cli.commands.search_mod.run_messages") as mock_run:
            cmd_search_messages(
                query="test query",
                max_results=5,
                session=None,
                project=None,
                path=None,
                before=None,
                after=None,
                verbose=False,
                include_notifications=False,
                db=DEFAULT_DB_PATH,
                ctx=DEFAULT_CLI_CONTEXT,
            )
        mock_run.assert_called_once_with(
            query="test query",
            max_results=5,
            session=None,
            project=None,
            path=None,
            before=None,
            after=None,
            output_format="markdown",
            verbose=False,
            include_notifications=False,
            db=DEFAULT_DB_PATH,
        )

    def test_positional_query_resolves_identically_to_flag(self):
        with patch("ccrecall.cli.commands.search_mod.run_messages") as mock_run:
            cmd_search_messages(
                "test query",
                max_results=5,
                session=None,
                project=None,
                path=None,
                before=None,
                after=None,
                verbose=False,
                include_notifications=False,
                db=DEFAULT_DB_PATH,
                ctx=DEFAULT_CLI_CONTEXT,
            )
        mock_run.assert_called_once_with(
            query="test query",
            max_results=5,
            session=None,
            project=None,
            path=None,
            before=None,
            after=None,
            output_format="markdown",
            verbose=False,
            include_notifications=False,
            db=DEFAULT_DB_PATH,
        )

    def test_app_accepts_positional_query(self):
        with patch("ccrecall.cli.commands.search_mod.run_messages") as mock_run, pytest.raises(SystemExit) as exc_info:
            app(["search-messages", "auth bug"], exit_on_error=False, print_error=False)
        assert exc_info.value.code == 0
        assert mock_run.call_args.kwargs["query"] == "auth bug"

    def test_app_accepts_flagged_query(self):
        with patch("ccrecall.cli.commands.search_mod.run_messages") as mock_run, pytest.raises(SystemExit) as exc_info:
            app(["search-messages", "-q", "auth bug"], exit_on_error=False, print_error=False)
        assert exc_info.value.code == 0
        assert mock_run.call_args.kwargs["query"] == "auth bug"

    def test_app_rejects_both_positional_and_flag(self):
        with pytest.raises(ValidationError, match="cannot both be given"):
            app(
                ["search-messages", "positional query", "-q", "flagged query"],
                exit_on_error=False,
                print_error=False,
            )

    def test_app_rejects_no_query(self):
        with pytest.raises(ValidationError, match="a query is required"):
            app(["search-messages"], exit_on_error=False, print_error=False)


class TestCmdTail:
    def test_calls_run_with_parsed_arguments_and_exits_with_return_code(self):
        with (
            patch("ccrecall.cli.commands.session_tail_mod.run", return_value=0) as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_tail(None, list_sessions=False, full=False, cwd=None, n=DEFAULT_TAIL_EVENTS)
        assert exc_info.value.code == 0
        mock_run.assert_called_once_with(
            None,
            list_sessions=False,
            cwd=None,
            n=DEFAULT_TAIL_EVENTS,
            full=False,
        )
