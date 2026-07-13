"""CLI entry points for conversation search (Entrypoint A/B) and status.

Returns markdown by default (token-efficient), or JSON when output_format="json"
(the CLI maps the global --json flag onto that argument).
"""

import json
import sqlite3
import sys
from pathlib import Path

from ccrecall.config import DEFAULT_DB_PATH
from ccrecall.db import (
    branch_embedding_coverage,
    chunk_vec_queryable,
    get_connection,
    parse_project_filter,
    resolve_db_settings,
)
from ccrecall.embeddings import DEPS_AVAILABLE, EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.errors import emit_error
from ccrecall.formatting import (
    apply_scores,
    build_envelope,
    format_card_json,
    format_card_markdown,
    format_result_list_markdown,
    format_snippet_json,
    format_snippet_markdown,
)
from ccrecall.schema import detect_fts_support
from ccrecall.search_conversations import compute_caveat, search_messages, search_sessions

# Upper bound on --max-results, single-sourced here and referenced by the CLI
# validator (cli/commands.py) so the clamp and the validator can't drift apart.
MAX_SEARCH_RESULTS = 10


def format_markdown(cards: list[dict], query: str, ranked: bool, verbose: bool = False) -> str:
    """Format session cards as markdown.

    verbose=True expands each card's files_modified/commits/tool_counts;
    the JSON path always carries the full lists regardless.
    """
    if not cards:
        return f"No sessions found for query: {query}"

    normalized = apply_scores(cards, ranked)
    card_markdowns = [format_card_markdown(c, verbose=verbose) for c in normalized]
    return format_result_list_markdown(ranked, card_markdowns)


def format_messages_markdown(snippets: list[dict], query: str, ranked: bool) -> str:
    """Format matched-exchange snippets as markdown (Entrypoint B)."""
    if not snippets:
        return f"No messages found for query: {query}"

    normalized = apply_scores(snippets, ranked)
    snippet_markdowns = [format_snippet_markdown(s) for s in normalized]
    return format_result_list_markdown(ranked, snippet_markdowns)


def run_messages(
    *,
    query: str,
    max_results: int = 5,
    session: str | None = None,
    project: str | None = None,
    path: str | None = None,
    output_format: str = "markdown",
    include_notifications: bool = False,  # noqa: ARG001 — accepted for surface symmetry; moot on B (no fetch)
    db: Path = DEFAULT_DB_PATH,
) -> None:
    """Search matched exchanges (Entrypoint B — chunk-KNN without rollup).

    On a vec0-unavailable machine (or any pre-KNN failure) emits a well-formed
    empty ranked:false envelope and returns normally, so the process exits 0
    rather than erroring. A missing DB or an unexpected error exits 1.
    """
    max_results = max(1, min(MAX_SEARCH_RESULTS, max_results))
    projects = parse_project_filter(project)

    if not db.exists():
        emit_error(
            "Database not found",
            code="db_not_found",
            exit_code=1,
            remediation="Run ccrecall import or start a session with the ccrecall plugin installed.",
        )

    try:
        settings = resolve_db_settings(db)
        with get_connection(settings, load_vec=True) as conn:
            snippets, ranked = search_messages(
                conn,
                query=query,
                max_results=max_results,
                projects=projects,
                session_id=session,
                path=path,
            )

        if output_format == "json":
            json_snippets = [format_snippet_json(s) for s in snippets]
            envelope = build_envelope(query, ranked, json_snippets)
            print(json.dumps(envelope, indent=2))
        else:
            print(format_messages_markdown(snippets, query, ranked))

    except Exception as e:
        emit_error(
            str(e),
            code="search_error",
            exit_code=1,
            remediation="Check ccrecall search --status for diagnostics.",
        )


def print_status(settings: dict | None) -> None:
    """Print diagnostic status and exit 0."""
    # Open one connection with vec loaded; chunk_vec_queryable probes whether
    # the vec table is usable — get_connection already loaded the extension
    # iff it could, so the table-existence probe is sufficient.
    try:
        with get_connection(settings, load_vec=True) as conn:
            is_vec = chunk_vec_queryable(conn)
            print(f"vec extension: {'yes' if is_vec else 'no'}")

            # Model: name + whether the embedding stack imports. Deliberately does
            # not call model_available() — that constructs the fastembed model and
            # would download it (~120 MB) on a cold cache, which a read-only status
            # must not do.
            print(f"model: {EMBEDDING_MODEL} (deps {'available' if DEPS_AVAILABLE else 'missing'})")

            # Chunk coverage (current-version chunks / total chunks) and branch watermark
            try:
                total_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
                current_chunks = conn.execute(
                    "SELECT count(*) FROM chunks WHERE embedding_version = ? AND embedding_model = ?",
                    (EMBEDDING_VERSION, EMBEDDING_MODEL),
                ).fetchone()[0]
                print(f"chunk coverage: {current_chunks}/{total_chunks} chunks at current version")

                # Branch watermark: branches whose every current exchange is embedded.
                embedded_branches, total_branches = branch_embedding_coverage(conn)
                print(f"embedded branches: {embedded_branches}/{total_branches} (watermark)")
            except sqlite3.Error as e:
                print(f"chunk coverage: error ({e})")
                print(f"embedded branches: error ({e})")
    except (sqlite3.Error, OSError):
        print("vec extension: no")
        print(f"model: {EMBEDDING_MODEL} (deps {'available' if DEPS_AVAILABLE else 'missing'})")
        print("chunk coverage: error (could not open database)")
        print("embedded branches: error (could not open database)")

    sys.exit(0)


def run(
    *,
    query: str | None = None,
    status: bool = False,
    keyword_only: bool = False,
    max_results: int = 5,
    session: str | None = None,
    project: str | None = None,
    path: str | None = None,
    output_format: str = "markdown",
    verbose: bool = False,
    include_notifications: bool = False,  # noqa: ARG001 — accepted for CLI compat; A-path has no transcript hydration
    db: Path = DEFAULT_DB_PATH,
) -> None:
    """Search conversation sessions (keyword + chunk-vector fusion)."""
    if not status and not query:
        emit_error(
            "one of --query/-q or --status is required",
            code="missing_arg",
            exit_code=2,
            remediation="ccrecall search -q 'your query' or ccrecall search --status",
        )
    if status and query:
        emit_error(
            "--query and --status are mutually exclusive",
            code="conflicting_args",
            exit_code=2,
            remediation="Use --query to search, or --status to check diagnostics — not both.",
        )

    # Backstop for direct callers; the CLI validator rejects out-of-range
    # --max-results before reaching here. Both sides bound on MAX_SEARCH_RESULTS.
    max_results = max(1, min(MAX_SEARCH_RESULTS, max_results))
    projects = parse_project_filter(project)

    if not db.exists():
        if status:
            print("vec extension: no")
            print(f"model: {EMBEDDING_MODEL} (deps {'available' if DEPS_AVAILABLE else 'missing'})")
            print("chunk coverage: error (database not found)")
            print("embedded branches: error (database not found)")
            sys.exit(0)
        emit_error(
            "Database not found",
            code="db_not_found",
            exit_code=1,
            remediation="Run ccrecall import or start a session with the ccrecall plugin installed.",
        )

    try:
        settings = resolve_db_settings(db)

        if status:
            print_status(settings)
            return  # print_status calls sys.exit(0), but be explicit

        # Past the status branch with the xor-validation above satisfied, query is
        # guaranteed present (status is False, so a missing query already exited 2).
        assert query is not None  # noqa: S101 — type-checker narrowing; the real guard is the exit above

        with get_connection(settings, load_vec=True) as conn:
            fts_level = detect_fts_support(conn)

            cards, ranked = search_sessions(
                conn,
                query=query,
                fts_level=fts_level,
                max_results=max_results,
                projects=projects,
                session_id=session,
                path=path,
                keyword_only=keyword_only,
            )
            caveat = compute_caveat(conn)

        if output_format == "json":
            json_cards = [format_card_json(c) for c in cards]
            envelope = build_envelope(query, ranked, json_cards)
            envelope["caveat"] = caveat
            print(json.dumps(envelope, indent=2))
        else:
            md = format_markdown(cards, query, ranked, verbose=verbose)
            if caveat is not None:
                md += f"\n\n_{caveat}_"
            print(md)

    except Exception as e:
        emit_error(
            str(e),
            code="search_error",
            exit_code=1,
            remediation="Check ccrecall search --status for diagnostics.",
        )
