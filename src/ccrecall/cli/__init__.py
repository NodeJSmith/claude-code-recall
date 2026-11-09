"""ccrecall CLI — single entry point consolidating the former cm-* binaries.

Root ``App`` plus a ``backfill`` sub-``App``. Command functions live in
``commands.py`` and register themselves on import (the import at the bottom of
this module triggers that registration).
"""

import inspect
import logging
import sqlite3
import sys
from importlib.metadata import PackageNotFoundError, version
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated

from cyclopts import App, Group, Parameter
from cyclopts.exceptions import CycloptsError

from ccrecall.cli.context import CLIContext
from ccrecall.config import DEFAULT_DB_PATH, get_db_path, load_settings, load_settings_for_db, setup_logging
from ccrecall.db_base import describe_schema_ahead
from ccrecall.errors import classify_failure, emit_failure
from ccrecall.models import LOGGER_NAME

try:
    _version = version("ccrecall")
except PackageNotFoundError:
    _version = "unknown"

_CLI_PROCESS_NAME = "cli"

app = App(
    name="ccrecall",
    version=_version,
    version_flags=["--version", "-V"],
    help="Conversation history and semantic search for Claude Code.",
    # Plaintext so the examples block keeps its line breaks and literal <…>
    # placeholders (markdown/rst reflow them and strip the angle brackets).
    help_format="plaintext",
    help_epilogue=(
        "Examples:\n"
        "  ccrecall recent --limit 5\n"
        "  ccrecall doctor\n"
        "  ccrecall --json search -q 'auth bug'\n"
        "  ccrecall tail <session-id>"
    ),
)

# Global options live on the meta launcher; group them so they render under one
# heading in `ccrecall --help` rather than scattered among the subcommands.
app.meta.group_parameters = Group("Global Options", sort_key=0)


backfill_app = App(name="backfill", help="Seed historical summaries and embeddings.")
app.command(backfill_app)


@app.meta.default
def launcher(
    *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
    json_mode: Annotated[
        bool,
        Parameter(name=["--json"], help="Emit machine-readable JSON instead of markdown.", negative=[]),
    ] = False,
    debug: Annotated[
        bool,
        Parameter(name=["--debug", "-d"], help="Print log output to stdout as well as the log file.", negative=[]),
    ] = False,
) -> None:
    """Parse global options into a CLIContext, then dispatch to the chosen command.

    The single global ``--json`` is the only output-format surface — commands no
    longer carry their own ``--json``/``--format`` flag, which is what keeps the
    contract from drifting. ``parse_args`` here mirrors the app-level error
    contract (boxed message, raise instead of exit) so ``main`` can force exit 2.
    """
    if debug:
        setup_logging(load_settings(), process_name=_CLI_PROCESS_NAME, verbose=True)
    ctx = CLIContext(json_mode=json_mode, debug=debug)
    # print_error=True is load-bearing: a CycloptsError escaping a meta.default
    # body is NOT re-rendered by the outer app.meta(), so this inner call is the
    # only thing that prints the boxed message. exit_on_error=False makes it
    # raise instead, so main()'s handler can force exit 2.
    command, bound, _ = app.parse_args(tokens, print_error=True, exit_on_error=False)
    # Inject ctx only where the command declares it (recent/search/backfill
    # embeddings) — output-less commands (hooks, tail, import) omit the param and
    # ignore a global --json. The third parse_args return value carries the ctx
    # *type*, not an instance, so it can't do the injection; we set it by hand.
    # ctx must stay keyword-only in every command for **bound.kwargs to carry it.
    if "ctx" in inspect.signature(command).parameters:
        bound.arguments["ctx"] = ctx
    schema_ahead_cause = describe_schema_ahead(
        get_db_path(load_settings_for_db(bound.arguments.get("db", DEFAULT_DB_PATH)))
    )
    if schema_ahead_cause:
        # stderr, so --json output on stdout stays parseable.
        print(
            f"ccrecall: warning: {schema_ahead_cause}. Upgrade ccrecall; commands may fail until then.", file=sys.stderr
        )
    try:
        command(*bound.args, **bound.kwargs)
    except BrokenPipeError:
        # A reader that closed the pipe early (`ccrecall recent | head`) is normal use, not a failure.
        raise
    except (sqlite3.Error, OSError) as exc:
        # One actionable line instead of a traceback; the traceback goes to the
        # log of whichever process this command set up (import, sync, cli, ...).
        log = logging.getLogger(LOGGER_NAME)
        operation = "ccrecall " + " ".join(app.parse_commands(tokens)[0])
        log.exception("command failed", extra={"operation": operation})
        emit_failure(
            classify_failure(
                exc, operation=operation, schema_ahead_cause=schema_ahead_cause, log_path=active_log_path(log)
            )
        )


def active_log_path(logger: logging.Logger) -> Path | None:
    """The file this logger writes to, or None when logging is disabled."""
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            return Path(handler.baseFilename)
    return None


def main() -> None:
    """Console-script entry point.

    Wraps the cyclopts meta app so every argument-parsing error exits 2 — the
    usual usage-error code — carrying cyclopts' boxed message, so parser errors
    agree with the app-level validators. A command that raises its own SystemExit
    bypasses this handler (SystemExit is not a CycloptsError), keeping its code.

    Sets up the "cli" process log before dispatch. Subcommands that are
    themselves detached background workers (sync-current, import, backfill
    summaries/embeddings) call ``setup_logging`` again with their own
    process name, which reconfigures the shared logger to their own log file
    for the rest of this process — this call only "sticks" for direct,
    interactive commands (search, recent, status, tail).
    """
    setup_logging(load_settings(), process_name=_CLI_PROCESS_NAME)
    try:
        app.meta(exit_on_error=False, print_error=True)
    except CycloptsError as exc:
        raise SystemExit(2) from exc


# Importing the commands module registers every subcommand on ``app`` /
# ``backfill_app`` via the @app.command decorators it defines. Kept at the
# bottom so ``app`` and ``backfill_app`` exist before the decorators run. The
# redundant ``as commands`` alias marks it as an intentional side-effect import,
# so ruff and pyright don't flag it unused without needing a separate sentinel.
from ccrecall.cli import commands as commands  # noqa: E402
