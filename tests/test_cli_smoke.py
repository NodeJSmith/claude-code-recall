"""Smoke tests for CLI command wiring: argument parsing -> module dispatch.

These verify each cyclopts command function calls its owning module's run()
with the arguments the CLI would parse -- not the underlying logic, which has
its own tests. cmd_backfill_embeddings is covered separately in
test_backfill_embeddings.py and is not repeated here.
"""

from unittest.mock import patch

import pytest

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
from ccrecall.db import DEFAULT_PROJECTS_DIR
from ccrecall.hooks import backfill_query as backfill_query_mod
from ccrecall.session_tail import DEFAULT_TAIL_EVENTS


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
    def test_calls_run_with_verbose(self):
        with patch("ccrecall.cli.commands.backfill_summaries_mod.run") as mock_run:
            cmd_backfill_summaries(ctx=DEFAULT_CLI_CONTEXT)
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
