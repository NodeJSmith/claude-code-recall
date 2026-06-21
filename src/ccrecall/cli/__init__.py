"""ccrecall CLI — single entry point consolidating the former cm-* binaries.

Root ``App`` plus a ``backfill`` sub-``App``. Command functions live in
``commands.py`` and register themselves on import (the import at the bottom of
this module triggers that registration).
"""

from importlib.metadata import PackageNotFoundError, version

from cyclopts import App
from cyclopts.exceptions import CycloptsError

try:
    _version = version("ccrecall")
except PackageNotFoundError:
    _version = "unknown"

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
        "  ccrecall recent --n 5\n"
        "  ccrecall search -q 'auth bug' --format json\n"
        "  ccrecall tail <session-id>\n"
        "  ccrecall backfill embeddings --status"
    ),
)


def main() -> None:
    """Console-script entry point.

    Wraps the cyclopts app so every argument-parsing error exits 2 — the usual
    usage-error code — carrying cyclopts' boxed message, so parser errors agree
    with the app-level validators. A command that raises its own SystemExit
    bypasses this handler (SystemExit is not a CycloptsError), keeping its code.
    """
    try:
        app(exit_on_error=False, print_error=True)
    except CycloptsError as exc:
        raise SystemExit(2) from exc


backfill_app = App(name="backfill", help="Seed historical summaries and embeddings.")
app.command(backfill_app)

# Importing the commands module registers every subcommand on ``app`` /
# ``backfill_app`` via the @app.command decorators it defines. Kept at the
# bottom so ``app`` and ``backfill_app`` exist before the decorators run.
from ccrecall.cli import commands  # noqa: E402

# Reference the side-effect import so ruff and pyright see it as used; the real
# effect is the subcommand registration that ran at import time.
_ = commands
