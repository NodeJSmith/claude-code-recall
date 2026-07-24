"""ccrecall subcommand definitions.

Each command is a thin cyclopts wrapper that parses typed parameters and calls
the ``run(...)`` logic function in the owning module. Output, exit codes, and
PID-file lifecycle are preserved from the former cm-* entry points; only the
argument-parsing layer changed (argparse -> cyclopts).
"""

import logging
from pathlib import Path
from typing import Annotated, Literal

from cyclopts import ArgumentCollection, Group, Parameter
from cyclopts.validators import Number

from ccrecall import recent_chats as recent_chats_mod
from ccrecall import search_cli as search_mod
from ccrecall import session_tail as session_tail_mod
from ccrecall.cli import app, backfill_app
from ccrecall.cli.context import DEFAULT_CLI_CONTEXT, CLIContextParam
from ccrecall.config import DEFAULT_DB_PATH, load_settings
from ccrecall.db import DEFAULT_PROJECTS_DIR, get_connection
from ccrecall.embeddings import DEFAULT_EMBED_THREADS
from ccrecall.hooks import backfill_embeddings as backfill_embeddings_mod
from ccrecall.hooks import backfill_query as backfill_query_mod
from ccrecall.hooks import backfill_summaries as backfill_summaries_mod
from ccrecall.hooks import backfill_tool_content as backfill_tool_content_mod
from ccrecall.hooks import import_conversations as import_mod
from ccrecall.hooks import sync_current as sync_current_mod
from ccrecall.models import LOGGER_NAME

# store_true flags carry no --no-<flag> negation, matching the former argparse.
_FLAG = Parameter(negative=[])


def _exactly_one_query_or_status(arguments: ArgumentCollection) -> None:
    """Group validator: search needs exactly one of --query / --status.

    Runs at parse time so the message renders in cyclopts' boxed error style
    (and exits 2 via the entry-point wrapper), matching every other usage error.
    search_cli.run() keeps the same guard for direct (non-CLI) callers.
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

# Output format is global: the meta launcher's --json fills ctx.json_mode, which
# read commands map to their run()'s output_format kwd. list_sessions -> --list
# is renamed to avoid shadowing the list builtin; Parameter(name=...) keeps the
# user-facing flag intact.

# Shared flag types mirroring the former cm-* read tools.
_VERBOSE = Annotated[bool, _FLAG, Parameter(name=["--verbose", "-v"], help="Expand files, commits, and tool counts.")]
_NOTIFS = Annotated[
    bool, _FLAG, Parameter(name=["--include-notifications"], help="Include task notification messages.")
]
_DB = Annotated[Path, Parameter(name=["--db"], help="Database path.")]
# Default for `tail -n`, sourced from session_tail so the two never drift.
_TAIL_DEFAULT_N = session_tail_mod.DEFAULT_TAIL_EVENTS
# Default result counts for the recent/search commands.
_DEFAULT_RECENT_N = 3
_DEFAULT_SEARCH_MAX_RESULTS = 5


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
    ctx: CLIContextParam = DEFAULT_CLI_CONTEXT,
) -> None:
    """Import Claude Code conversations into the memory DB."""
    import_mod.run(db=db, projects_dir=projects_dir, project=project, verbose=ctx.debug)


def _count_multi_active_branch_sessions(db: Path) -> int:
    """Return the count of sessions with more than one active branch.

    Session-keyed branch identity (branch_ops.upsert_branch) should make this
    always zero going forward — this is a standing invariant check, not an
    expected condition. Read-only: shares no PID lifecycle with import.run().
    """
    settings = load_settings()
    if db != DEFAULT_DB_PATH:
        settings["db_path"] = str(db)
    with get_connection(settings, load_vec=False) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT session_id, COUNT(*) as cnt FROM branches WHERE is_active = 1 GROUP BY session_id HAVING cnt > 1"
        )
        return len(cursor.fetchall())


@app.command(name="stats")
def cmd_stats(
    *,
    db: Annotated[Path, Parameter(help="Database path.")] = DEFAULT_DB_PATH,
) -> None:
    """Show memory database statistics."""
    # Read-only DB-global counts: print_stats() shares no PID lifecycle with
    # import.run(), so it can't disturb a concurrent background import.
    import_mod.print_stats(db=db)

    violations = _count_multi_active_branch_sessions(db)
    print(f"Branch invariant violations: {violations} session(s) with multiple active branches")
    if violations:
        logging.getLogger(LOGGER_NAME).warning(
            "branch invariant violated: %d session(s) have more than one active branch", violations
        )


@backfill_app.command(name="summaries")
def cmd_backfill_summaries(
    *,
    ctx: CLIContextParam = DEFAULT_CLI_CONTEXT,
) -> None:
    """Backfill context summaries for branches that lack a current one."""
    backfill_summaries_mod.run(verbose=ctx.debug)


@backfill_app.command(name="embeddings")
def cmd_backfill_embeddings(
    *,
    status: Annotated[bool, _FLAG, Parameter(help="Report progress and exit without embedding (read-only).")] = False,
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
    ] = backfill_query_mod.DEFAULT_PROGRESS_EVERY,
    threads: Annotated[int, Parameter(help="ONNX intra-op threads per inference call.")] = DEFAULT_EMBED_THREADS,
    ctx: CLIContextParam = DEFAULT_CLI_CONTEXT,
) -> None:
    """Seed historical embeddings for active-leaf branch summaries (opt-in)."""
    try:
        code = backfill_embeddings_mod.run(
            status=status,
            json_mode=ctx.json_mode,
            days=days,
            limit=limit,
            progress_every=progress_every,
            threads=threads,
            verbose=ctx.debug,
        )
    finally:
        # Status is read-only: never disturb a concurrent backfill's PID marker.
        if not status:
            backfill_query_mod.cleanup_pid()
    raise SystemExit(code)


@backfill_app.command(name="tool-content")
def cmd_backfill_tool_content(
    *,
    status: Annotated[bool, _FLAG, Parameter(help="Report progress and exit without backfilling (read-only).")] = False,
    days: Annotated[
        int | None,
        Parameter(
            validator=Number(gte=1), help="Only backfill sessions with a branch ended within the last N days (>= 1)."
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Parameter(validator=Number(gte=1), help="Stop after backfilling at most N sessions this run (>= 1)."),
    ] = None,
    progress_every: Annotated[
        int, Parameter(help="Print a progress line every N newly backfilled sessions.")
    ] = backfill_query_mod.DEFAULT_PROGRESS_EVERY,
    ctx: CLIContextParam = DEFAULT_CLI_CONTEXT,
) -> None:
    """Re-parse existing sessions' JSONL files to populate tool_content (opt-in)."""
    # No PID_KEY/cleanup_pid() pair here, unlike cmd_backfill_embeddings above:
    # that marker is only ever written by _spawn_background, and embeddings
    # backfill is (like this command) never auto-spawned — so its cleanup_pid()
    # call is a no-op today too. Not mirroring dead ceremony onto a new command.
    code = backfill_tool_content_mod.run(
        status=status,
        json_mode=ctx.json_mode,
        days=days,
        limit=limit,
        progress_every=progress_every,
        verbose=ctx.debug,
    )
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
    ] = _DEFAULT_RECENT_N,
    sort_order: Annotated[Literal["desc", "asc"], Parameter(name=["--sort-order"], help="Sort order.")] = "desc",
    before: Annotated[str | None, Parameter(help="Sessions before this datetime (ISO).")] = None,
    after: Annotated[str | None, Parameter(help="Sessions after this datetime (ISO).")] = None,
    session: Annotated[str | None, Parameter(help="Filter by session UUID (prefix match).")] = None,
    project: Annotated[str | None, Parameter(help="Filter by project name(s), comma-separated.")] = None,
    path: Annotated[str | None, Parameter(help="Filter by cwd substring (e.g. worktree name).")] = None,
    verbose: _VERBOSE = False,
    include_notifications: _NOTIFS = False,
    db: _DB = DEFAULT_DB_PATH,
    ctx: CLIContextParam = DEFAULT_CLI_CONTEXT,
) -> None:
    """List recent conversation sessions.

    With --json: {sessions: [{uuid, project, started_at, ended_at, git_branch,
    messages}], total_sessions, total_messages}
    """
    recent_chats_mod.run(
        n=n,
        sort_order=sort_order,
        before=before,
        after=after,
        session=session,
        project=project,
        path=path,
        output_format=ctx.output_format,
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
            name=["--max-results", "-n", "--n"],
            validator=Number(gte=1, lte=search_mod.MAX_SEARCH_RESULTS),
            help=f"Max sessions (1-{search_mod.MAX_SEARCH_RESULTS}).",
        ),
    ] = _DEFAULT_SEARCH_MAX_RESULTS,
    session: Annotated[str | None, Parameter(help="Filter by session UUID (prefix match).")] = None,
    project: Annotated[str | None, Parameter(help="Filter by project name(s), comma-separated.")] = None,
    path: Annotated[str | None, Parameter(help="Filter by cwd substring (e.g. worktree name).")] = None,
    verbose: _VERBOSE = False,
    include_notifications: _NOTIFS = False,
    db: _DB = DEFAULT_DB_PATH,
    ctx: CLIContextParam = DEFAULT_CLI_CONTEXT,
) -> None:
    """Search conversation sessions (keyword + vector fusion).

    With --json: {query, ranked, count, results: [{score, session_uuid, handle, project, git_branch, topic, ...}]}
    """
    search_mod.run(
        query=query,
        status=status,
        keyword_only=keyword_only,
        max_results=max_results,
        session=session,
        project=project,
        path=path,
        output_format=ctx.output_format,
        verbose=verbose,
        include_notifications=include_notifications,
        db=db,
    )


@app.command(name="search-messages")
def cmd_search_messages(
    *,
    query: Annotated[str, Parameter(name=["--query", "-q"], help="Search query (required).")],
    max_results: Annotated[
        int,
        Parameter(
            name=["--max-results", "-n", "--n"],
            validator=Number(gte=1, lte=search_mod.MAX_SEARCH_RESULTS),
            help=f"Max matched exchanges (1-{search_mod.MAX_SEARCH_RESULTS}).",
        ),
    ] = _DEFAULT_SEARCH_MAX_RESULTS,
    session: Annotated[str | None, Parameter(help="Filter by session UUID (prefix match).")] = None,
    project: Annotated[str | None, Parameter(help="Filter by project name(s), comma-separated.")] = None,
    path: Annotated[str | None, Parameter(help="Filter by cwd substring (e.g. worktree name).")] = None,
    include_notifications: _NOTIFS = False,
    db: _DB = DEFAULT_DB_PATH,
    ctx: CLIContextParam = DEFAULT_CLI_CONTEXT,
) -> None:
    """Search matched exchanges by semantic similarity (chunk-KNN, Entrypoint B).

    Returns matched exchanges ranked by chunk distance — not rolled up to session,
    so multiple matches within one session all appear as separate results.

    With --json: {query, ranked, count, results: [{score, session_uuid, handle, exchange_index, user, assistant, ...}]}
    """
    search_mod.run_messages(
        query=query,
        max_results=max_results,
        session=session,
        project=project,
        path=path,
        output_format=ctx.output_format,
        include_notifications=include_notifications,
        db=db,
    )


@app.command(name="tail")
def cmd_tail(
    selector: Annotated[str | None, Parameter(help="Session id or substring to target.")] = None,
    *,
    list_sessions: Annotated[bool, _FLAG, Parameter(name=["--list"], help="List sessions and exit.")] = False,
    full: Annotated[
        bool, _FLAG, Parameter(name=["--full"], help="Print full untruncated last instruction and assistant message.")
    ] = False,
    cwd: Annotated[str | None, Parameter(name=["--cwd"], help="Derive project dir from this path.")] = None,
    n: Annotated[
        int, Parameter(name=["-n", "--n", "--lines"], help="Number of tail events to show.")
    ] = _TAIL_DEFAULT_N,
) -> None:
    """Print the tail of a prior session's transcript for fast resume."""
    raise SystemExit(session_tail_mod.run(selector, list_sessions=list_sessions, cwd=cwd, n=n, full=full))
