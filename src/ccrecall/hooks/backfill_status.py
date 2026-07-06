"""Read-only progress reporting for the embedding backfill (`--status`).

Counts branch/chunk coverage without touching any rows, and formats the
human-readable and JSON status reports consumed by `backfill_embeddings.run`.
"""

import json
import logging
import sqlite3
import sys

from ccrecall.db import CHUNK_EMBEDDABLE_BRANCH_FILTER, CONTENT_ERROR_VERSION, chunk_vec_queryable, get_connection
from ccrecall.embeddings import EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.hooks.backfill_query import EXIT_ABORT, EXIT_OK, build_selection, days_modifier


def count_status(cursor: sqlite3.Cursor, days: int | None) -> dict[str, int]:
    """Count backfill progress without doing any work.

    universe          = chunks belonging to CHUNK_EMBEDDABLE branches.
    done              = chunks with a current-version chunk_vec row.
    total_branches    = all CHUNK_EMBEDDABLE branches — the honest coverage
                        denominator (branch count).
    embedded_branches = embeddable branches that are neither pending nor errored.
    eligible          = branches still needing work (build_selection predicate).
    errored           = branches marked with the content-error sentinel.

    universe/done are chunk-grain; the rest are branch-grain. Branch grain is
    the honest coverage signal: a never-chunked branch contributes 0 to
    universe but 1 to total_branches, so the backlog can't hide in the
    denominator (the misleading "100% embedded, N remaining" report).

    The optional --days recency bound is applied consistently to all queries.
    """
    recency_joined = ""  # for chunk JOIN branches queries
    recency_branch = ""  # for branches-only queries
    recency_params: list = []
    if days is not None:
        recency_joined = " AND branches.ended_at > datetime('now', ?)"
        recency_branch = " AND ended_at > datetime('now', ?)"
        recency_params = [days_modifier(days)]

    # universe: total chunks belonging to CHUNK_EMBEDDABLE branches
    cursor.execute(
        f"""
        SELECT COUNT(*) FROM chunks
        JOIN branches ON chunks.branch_id = branches.id
        WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}{recency_joined}
        """,
        recency_params,
    )
    universe = cursor.fetchone()[0]

    # done: chunks with a current-version embedding AND an existing chunk_vec row
    cursor.execute(
        f"""
        SELECT COUNT(*) FROM chunks
        JOIN branches ON chunks.branch_id = branches.id
        WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}
          AND chunks.embedding_version = ?
          AND chunks.embedding_model = ?
          AND EXISTS (SELECT 1 FROM chunk_vec WHERE chunk_id = chunks.id){recency_joined}
        """,
        [EMBEDDING_VERSION, EMBEDDING_MODEL, *recency_params],
    )
    done = cursor.fetchone()[0]

    # errored: branches at the content-error sentinel
    cursor.execute(
        f"""
        SELECT COUNT(*) FROM branches
        WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}
          AND embedding_version IS {CONTENT_ERROR_VERSION}{recency_branch}
        """,
        recency_params,
    )
    errored = cursor.fetchone()[0]

    # eligible: branches that build_selection would include (branch count)
    where, params = build_selection(days)
    cursor.execute(f"SELECT COUNT(*) FROM branches {where}", params)
    eligible = cursor.fetchone()[0]

    # total_branches: every embeddable branch (branch grain). embedded =
    # total - eligible - errored: a branch is one of embedded / eligible /
    # errored, and eligible/errored are disjoint (build_selection excludes the
    # error sentinel). Note this is stricter than the watermark count in
    # db.branch_embedding_coverage(): build_selection's heal clause counts a
    # watermark-current branch with a missing chunk_vec row as eligible, so on a
    # DB with orphaned vectors `--status` reports fewer embedded than `stats`.
    cursor.execute(
        f"SELECT COUNT(*) FROM branches WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}{recency_branch}",
        recency_params,
    )
    total_branches = cursor.fetchone()[0]

    return {
        # universe/done are chunk-grain, kept for back-compat with older
        # --status --json consumers; total_branches/embedded_branches are the
        # honest branch-grain coverage.
        "universe": universe,
        "done": done,
        "eligible": eligible,
        "errored": errored,
        "total_branches": total_branches,
        "embedded_branches": total_branches - eligible - errored,
    }


def format_duration(seconds: float) -> str:
    """Compact human duration: '45s', '12m03s', '1h07m'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def run_status(
    *,
    days: int | None,
    json_mode: bool,
    settings: dict | None,
    logger: logging.Logger,
) -> int:
    """Report branch coverage (embedded/total) plus eligible/errored counts (read-only)."""
    try:
        with get_connection(settings, load_vec=True) as conn:
            if not chunk_vec_queryable(conn):
                logger.error("Backfill status: sqlite-vec unavailable")
                print("ccrecall backfill embeddings: sqlite-vec unavailable", file=sys.stderr)
                return EXIT_ABORT
            counts = count_status(conn.cursor(), days)
    except (sqlite3.Error, OSError) as e:
        logger.exception("Backfill status: aborted")
        print(f"ccrecall backfill embeddings: aborted: {e}", file=sys.stderr)
        return EXIT_ABORT

    if json_mode:
        print(json.dumps({**counts, "days": days}))
        return EXIT_OK

    total = counts["total_branches"]
    embedded = counts["embedded_branches"]
    pct = (embedded / total * 100) if total else 0.0
    scope = f" (last {days}d)" if days is not None else ""
    print(f"ccrecall backfill embeddings status{scope}:")
    print(f"  branches:  {embedded} / {total} embedded  ({pct:.0f}%)")
    print(f"  remaining: {counts['eligible']} branches")
    if counts["errored"]:
        print(f"  errored:   {counts['errored']} branches  (content errors, won't retry)")
    return EXIT_OK
