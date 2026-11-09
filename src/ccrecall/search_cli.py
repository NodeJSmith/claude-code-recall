"""CLI entry points for conversation search (Entrypoint A/B).

Returns markdown by default (token-efficient), or JSON when output_format="json"
(the CLI maps the global --json flag onto that argument).
"""

import json
import logging
from pathlib import Path

from ccrecall.config import DEFAULT_DB_PATH
from ccrecall.dates import validate_or_exit
from ccrecall.db import get_connection, parse_project_filter, resolve_db_settings
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
from ccrecall.models import LOGGER_NAME
from ccrecall.schema import detect_fts_support
from ccrecall.search_conversations import compute_caveat, search_messages, search_sessions
from ccrecall.search_query import ScopeFilter

log = logging.getLogger(LOGGER_NAME)

# Upper bound on --max-results, single-sourced here and referenced by the CLI
# validator (cli/commands.py) so the clamp and the validator can't drift apart.
MAX_SEARCH_RESULTS = 20


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
    before: str | None = None,
    after: str | None = None,
    output_format: str = "markdown",
    verbose: bool = False,  # noqa: ARG001 — accepted for CLI symmetry; B has no session-level metadata to expand
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

    # Reassignment is required, not stylistic: a non-UTC offset instant is
    # normalized to UTC here, and the original (unnormalized) value would
    # compare incorrectly against the stored UTC timestamps.
    before, after = validate_or_exit(before, after)

    if not db.exists():
        emit_error(
            "Database not found",
            code="db_not_found",
            exit_code=1,
            remediation="Run ccrecall import or start a session with the ccrecall plugin installed.",
        )

    scope = ScopeFilter(projects=projects, session_id=session, path=path, before=before, after=after)

    try:
        settings = resolve_db_settings(db)
        with get_connection(settings, load_vec=True) as conn:
            snippets, ranked = search_messages(conn, query=query, max_results=max_results, scope=scope)

        if output_format == "json":
            json_snippets = [format_snippet_json(s) for s in snippets]
            envelope = build_envelope(query, ranked, json_snippets)
            print(json.dumps(envelope, indent=2))
        else:
            print(format_messages_markdown(snippets, query, ranked))

    except Exception as exc:
        log.exception("message search failed", extra={"query_length": len(query)})
        emit_error(
            str(exc),
            code="search_error",
            exit_code=1,
            remediation="Run ccrecall doctor for diagnostics.",
        )


def run(
    *,
    query: str,
    keyword_only: bool = False,
    max_results: int = 5,
    session: str | None = None,
    project: str | None = None,
    path: str | None = None,
    before: str | None = None,
    after: str | None = None,
    output_format: str = "markdown",
    verbose: bool = False,
    include_notifications: bool = False,  # noqa: ARG001 — accepted for CLI compat; A-path has no transcript hydration
    db: Path = DEFAULT_DB_PATH,
) -> None:
    """Search conversation sessions (keyword + chunk-vector fusion)."""
    if not query.strip():
        emit_error(
            "a query is required",
            code="missing_arg",
            exit_code=2,
            remediation="ccrecall search 'your query'",
        )
    # Backstop for direct callers; the CLI validator rejects out-of-range
    # --max-results before reaching here. Both sides bound on MAX_SEARCH_RESULTS.
    max_results = max(1, min(MAX_SEARCH_RESULTS, max_results))
    projects = parse_project_filter(project)

    # Reassignment is required, not stylistic: a non-UTC offset instant is
    # normalized to UTC here, and the original (unnormalized) value would
    # compare incorrectly against the stored UTC timestamps.
    before, after = validate_or_exit(before, after)

    if not db.exists():
        emit_error(
            "Database not found",
            code="db_not_found",
            exit_code=1,
            remediation="Run ccrecall import or start a session with the ccrecall plugin installed.",
        )

    scope = ScopeFilter(projects=projects, session_id=session, path=path, before=before, after=after)

    try:
        settings = resolve_db_settings(db)

        with get_connection(settings, load_vec=True) as conn:
            fts_level = detect_fts_support(conn)

            cards, ranked = search_sessions(
                conn,
                query=query,
                fts_level=fts_level,
                max_results=max_results,
                scope=scope,
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

    except Exception as exc:
        log.exception("session search failed", extra={"query_length": len(query)})
        emit_error(
            str(exc),
            code="search_error",
            exit_code=1,
            remediation="Run ccrecall doctor for diagnostics.",
        )
