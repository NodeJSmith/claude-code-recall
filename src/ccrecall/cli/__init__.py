"""ccrecall CLI — single entry point consolidating the former cm-* binaries.

Root ``App`` plus a ``backfill`` sub-``App``. Command functions live in
``commands.py`` and register themselves on import (the import at the bottom of
this module triggers that registration).
"""

from importlib.metadata import PackageNotFoundError, version

from cyclopts import App

try:
    _version = version("ccrecall")
except PackageNotFoundError:
    _version = "unknown"

app = App(
    name="ccrecall",
    version=_version,
    version_flags=["--version", "-V"],
    help="Conversation history and semantic search for Claude Code.",
)

backfill_app = App(name="backfill", help="Seed historical summaries and embeddings.")
app.command(backfill_app)

# Importing the commands module registers every subcommand on ``app`` /
# ``backfill_app`` via the @app.command decorators it defines. Kept at the
# bottom so ``app`` and ``backfill_app`` exist before the decorators run.
from ccrecall.cli import commands  # noqa: E402

# Reference the side-effect import so ruff and pyright see it as used; the real
# effect is the subcommand registration that ran at import time.
_ = commands
