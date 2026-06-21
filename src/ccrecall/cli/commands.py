"""ccrecall subcommand definitions.

Each command is a thin cyclopts wrapper that parses typed parameters and calls
the ``run(...)`` logic function in the owning module. Output, exit codes, and
PID-file lifecycle are preserved from the former cm-* entry points; only the
argument-parsing layer changed (argparse -> cyclopts).
"""

from pathlib import Path
from typing import Annotated, Literal

from cyclopts import ArgumentCollection, Group, Parameter
from cyclopts.validators import Number

from ccrecall import recent_chats as recent_chats_mod
from ccrecall import search_conversations as search_mod
from ccrecall import session_tail as session_tail_mod
from ccrecall import token_dashboard as token_dashboard_mod
from ccrecall.cli import app, backfill_app
from ccrecall.db import DEFAULT_DB_PATH, DEFAULT_PROJECTS_DIR
from ccrecall.embeddings import DEFAULT_EMBED_THREADS
from ccrecall.hooks import backfill_embeddings as backfill_embeddings_mod
from ccrecall.hooks import backfill_summaries as backfill_summaries_mod
from ccrecall.hooks import import_conversations as import_mod
from ccrecall.hooks import sync_current as sync_current_mod
from ccrecall.hooks import write_config as write_config_mod

# store_true flags carry no --no-<flag> negation, matching the former argparse.
_FLAG = Parameter(negative=[])


def _exactly_one_query_or_status(arguments: ArgumentCollection) -> None:
    """Group validator: search needs exactly one of --query / --status.

    Runs at parse time so the message renders in cyclopts' boxed error style
    (and exits 2 via the entry-point wrapper), matching every other usage error.
    search_conversations.run() keeps the same guard for direct (non-CLI) callers.
    """
    # arg.tokens is non-empty only when that argument was supplied on the CLI.
    provided = [arg for arg in arguments if arg.tokens]
    if not provided:
        raise ValueError("one of --query/-q or --status is required")
    if len(provided) > 1:
        raise ValueError("--query and --status are mutually exclusive")


# Membership in this group (query + status below) is what triggers the
# exactly-one validator at parse time — the flags carry no logic themselves.
_SEARCH_MODE = Group("Search mode", validator=_exactly_one_query_or_status)

# A few run() parameters are renamed from their CLI flag to avoid shadowing a
# builtin/module: json_mode -> --json (the json module), output_format ->
# --format (the format builtin), list_sessions -> --list (the list builtin). The
# explicit Parameter(name=...) below keeps the user-facing flag name intact.

# Shared flag types mirroring the former cm-* read tools.
_VERBOSE = Annotated[bool, _FLAG, Parameter(name=["--verbose", "-v"], help="Include files_modified and commits.")]
_NOTIFS = Annotated[
    bool, _FLAG, Parameter(name=["--include-notifications"], help="Include task notification messages.")
]
_FORMAT = Annotated[Literal["markdown", "json"], Parameter(name=["--format"], help="Output format.")]
_DB = Annotated[Path, Parameter(name=["--db"], help="Database path.")]
# Default for `tail -n`, sourced from session_tail so the two never drift.
_TAIL_DEFAULT_N = session_tail_mod.DEFAULT_TAIL_EVENTS


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
) -> None:
    """Import Claude Code conversations into the memory DB."""
    import_mod.run(db=db, projects_dir=projects_dir, project=project)


@app.command(name="stats")
def cmd_stats(
    *,
    db: Annotated[Path, Parameter(help="Database path.")] = DEFAULT_DB_PATH,
) -> None:
    """Show memory database statistics."""
    # Read-only DB-global counts: print_stats() shares no PID lifecycle with
    # import.run(), so it can't disturb a concurrent background import.
    import_mod.print_stats(db=db)


@backfill_app.command(name="summaries")
def cmd_backfill_summaries() -> None:
    """Backfill context summaries for branches that lack a current one."""
    backfill_summaries_mod.run()


@backfill_app.command(name="embeddings")
def cmd_backfill_embeddings(
    *,
    status: Annotated[bool, _FLAG, Parameter(help="Report progress and exit without embedding (read-only).")] = False,
    json_mode: Annotated[
        bool, _FLAG, Parameter(name="--json", help="Emit a machine-readable result on stdout.")
    ] = False,
    days: Annotated[
        int | None,
        Parameter(validator=Number(gte=1), help="Only embed branches ended within the last N days (>= 1)."),
    ] = None,
    limit: Annotated[
        int | None,
        Parameter(validator=Number(gte=1), help="Stop after embedding at most N branches this run (>= 1)."),
    ] = None,
    progress_every: Annotated[
        int, Parameter(help="Print a progress line every N newly embedded branches.")
    ] = backfill_embeddings_mod.DEFAULT_PROGRESS_EVERY,
    threads: Annotated[int, Parameter(help="Inference threads.")] = DEFAULT_EMBED_THREADS,
) -> None:
    """Seed historical embeddings for active-leaf branch summaries (opt-in)."""
    try:
        code = backfill_embeddings_mod.run(
            status=status,
            json_mode=json_mode,
            days=days,
            limit=limit,
            progress_every=progress_every,
            threads=threads,
        )
    finally:
        # Status is read-only: never disturb a concurrent backfill's PID marker.
        if not status:
            backfill_embeddings_mod.cleanup_pid()
    raise SystemExit(code)


@app.command(name="recent")
def cmd_recent(
    *,
    n: Annotated[
        int,
        Parameter(
            name=["--n", "-n"],
            validator=Number(gte=1, lte=recent_chats_mod.MAX_RECENT_SESSIONS),
            help=f"Number of sessions (1-{recent_chats_mod.MAX_RECENT_SESSIONS}).",
        ),
    ] = 3,
    sort_order: Annotated[Literal["desc", "asc"], Parameter(name=["--sort-order"], help="Sort order.")] = "desc",
    before: Annotated[str | None, Parameter(help="Sessions before this datetime (ISO).")] = None,
    after: Annotated[str | None, Parameter(help="Sessions after this datetime (ISO).")] = None,
    session: Annotated[str | None, Parameter(help="Filter by session UUID (prefix match).")] = None,
    project: Annotated[str | None, Parameter(help="Filter by project name(s), comma-separated.")] = None,
    path: Annotated[str | None, Parameter(help="Filter by cwd substring (e.g. worktree name).")] = None,
    output_format: _FORMAT = "markdown",
    verbose: _VERBOSE = False,
    include_notifications: _NOTIFS = False,
    db: _DB = DEFAULT_DB_PATH,
) -> None:
    """List recent conversation sessions."""
    recent_chats_mod.run(
        n=n,
        sort_order=sort_order,
        before=before,
        after=after,
        session=session,
        project=project,
        path=path,
        output_format=output_format,
        verbose=verbose,
        include_notifications=include_notifications,
        db=db,
    )


@app.command(name="search")
def cmd_search(
    *,
    query: Annotated[str | None, Parameter(name=["--query", "-q"], group=_SEARCH_MODE, help="Search keywords.")] = None,
    status: Annotated[bool, _FLAG, Parameter(group=_SEARCH_MODE, help="Print diagnostic status and exit.")] = False,
    keyword_only: Annotated[bool, _FLAG, Parameter(help="Skip embedding; keyword search only.")] = False,
    max_results: Annotated[
        int,
        Parameter(
            name=["--max-results"],
            validator=Number(gte=1, lte=search_mod.MAX_SEARCH_RESULTS),
            help=f"Max sessions (1-{search_mod.MAX_SEARCH_RESULTS}).",
        ),
    ] = 5,
    session: Annotated[str | None, Parameter(help="Filter by session UUID (prefix match).")] = None,
    project: Annotated[str | None, Parameter(help="Filter by project name(s), comma-separated.")] = None,
    path: Annotated[str | None, Parameter(help="Filter by cwd substring (e.g. worktree name).")] = None,
    output_format: _FORMAT = "markdown",
    verbose: _VERBOSE = False,
    include_notifications: _NOTIFS = False,
    db: _DB = DEFAULT_DB_PATH,
) -> None:
    """Search conversation sessions (keyword + vector fusion)."""
    search_mod.run(
        query=query,
        status=status,
        keyword_only=keyword_only,
        max_results=max_results,
        session=session,
        project=project,
        path=path,
        output_format=output_format,
        verbose=verbose,
        include_notifications=include_notifications,
        db=db,
    )


@app.command(name="tail")
def cmd_tail(
    selector: Annotated[str | None, Parameter(help="Session id or substring to target.")] = None,
    *,
    list_sessions: Annotated[bool, _FLAG, Parameter(name=["--list"], help="List sessions and exit.")] = False,
    cwd: Annotated[str | None, Parameter(name=["--cwd"], help="Derive project dir from this path.")] = None,
    n: Annotated[int, Parameter(name=["-n"], help="Number of tail events to show.")] = _TAIL_DEFAULT_N,
) -> None:
    """Print the tail of a prior session's transcript for fast resume."""
    raise SystemExit(session_tail_mod.run(selector, list_sessions=list_sessions, cwd=cwd, n=n))


@app.command(name="tokens")
def cmd_tokens() -> None:
    """Ingest token data, refresh the dashboard, and print a slim summary."""
    token_dashboard_mod.run()


@app.command(name="write-config")
def cmd_write_config(
    *,
    defaults: Annotated[bool, _FLAG, Parameter(help="Write recommended defaults without explicit flags.")] = False,
    auto_inject_context: Annotated[
        bool | None, Parameter(name=["--auto-inject-context"], help="Enable session context injection on startup.")
    ] = None,
) -> None:
    """Write or update the ccrecall config from onboarding choices."""
    write_config_mod.run(defaults=defaults, auto_inject_context=auto_inject_context)
