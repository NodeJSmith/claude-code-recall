"""ccrecall subcommand definitions.

Each command is a thin cyclopts wrapper that parses typed parameters and calls
the ``run(...)`` logic function in the owning module. Output, exit codes, and
PID-file lifecycle are preserved from the former cm-* entry points; only the
argument-parsing layer changed (argparse -> cyclopts).
"""

from pathlib import Path
from typing import Annotated

from cyclopts import Parameter

from ccrecall.cli import app, backfill_app
from ccrecall.db import DEFAULT_DB_PATH, DEFAULT_PROJECTS_DIR
from ccrecall.hooks import backfill_summaries as backfill_summaries_mod
from ccrecall.hooks import import_conversations as import_mod
from ccrecall.hooks import sync_current as sync_current_mod

# store_true flags carry no --no-<flag> negation, matching the former argparse.
_FLAG = Parameter(negative=[])


@app.command(name="sync-current")
def cmd_sync_current(
    *,
    input_file: Annotated[
        Path | None,
        Parameter(name="--input-file", help="Read hook input from this file instead of stdin."),
    ] = None,
) -> None:
    """Sync the current session into the memory DB (Stop-hook helper)."""
    sync_current_mod.run(input_file)


@app.command(name="import")
def cmd_import(
    *,
    db: Annotated[Path, Parameter(help="Database path.")] = DEFAULT_DB_PATH,
    projects_dir: Annotated[Path, Parameter(help="Projects directory.")] = DEFAULT_PROJECTS_DIR,
    project: Annotated[str | None, Parameter(help="Import only this project (by directory name).")] = None,
    search: Annotated[str | None, Parameter(help="Search conversations instead of importing.")] = None,
    limit: Annotated[int, Parameter(help="Search result limit.")] = 20,
    stats: Annotated[bool, _FLAG, Parameter(help="Show database statistics.")] = False,
) -> None:
    """Import (or search) Claude Code conversations into the memory DB."""
    import_mod.run(db=db, projects_dir=projects_dir, project=project, search=search, limit=limit, stats=stats)


@backfill_app.command(name="summaries")
def cmd_backfill_summaries() -> None:
    """Backfill context summaries for branches that lack a current one."""
    backfill_summaries_mod.run()
