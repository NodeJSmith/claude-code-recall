"""Query construction, constants, and PID cleanup for the embedding backfill.

Owns the eligible-branch selection predicate (`build_selection`) and the
constants that both the orchestrator (`backfill_embeddings.run`) and the
status reporter (`backfill_status`) need to agree on.
"""

from ccrecall.config import remove_pid_file
from ccrecall.db import CHUNK_EMBEDDABLE_BRANCH_FILTER, CONTENT_ERROR_VERSION
from ccrecall.embeddings import EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.hooks.tool_content_eligibility import days_modifier

BATCH_SIZE = 20
BACKFILL_BATCH_DELAY_SECONDS = 0.05
DEFAULT_PROGRESS_EVERY = BATCH_SIZE

# Lower scheduling priority for this background CPU job so interactive work wins.
BACKFILL_NICE_LEVEL = 10

EXIT_OK = 0
EXIT_ABORT = 1

# PID key for the self-concurrency guard (this command is manual-only, never
# auto-spawned; the marker is cleaned up by the CLI command on exit).
PID_KEY = "ccrecall-backfill-embeddings"


def cleanup_pid() -> None:
    """Remove the self-concurrency PID marker (no-op if absent)."""
    remove_pid_file(PID_KEY)


def build_selection(days: int | None) -> tuple[str, list]:
    """Return the shared WHERE clause + params for eligible-branch selection (chunk path).

    Eligible = CHUNK_EMBEDDABLE branch (active leaf with at least one message),
    not the content-error sentinel (-1), and needing a chunk embed:
    - watermark-stale: embedding_version IS NULL or below EMBEDDING_VERSION or
      wrong model — this includes version-stale chunks the write path skips;
      the backfill owns their re-embed.
    - heal clause: EXISTS a chunks row without a chunk_vec — catches crash
      victims and post-drop orphans the watermark can't see (design C1).

    summary_version_at_embed is dropped from the predicate: chunk staleness is
    driven by content_hash + EMBEDDING_VERSION, not the summary version.

    Optional recency bound on ended_at via --days; NULL ended_at is excluded
    (SQLite NULL > x is false). Returns a SQL fragment for
    f"... FROM branches {where}".
    """
    where = f"""
        WHERE {CHUNK_EMBEDDABLE_BRANCH_FILTER}
          AND embedding_version IS NOT {CONTENT_ERROR_VERSION}
          AND (
            embedding_version IS NULL
            OR embedding_version < ?
            OR embedding_model IS NOT ?
            OR EXISTS (
              SELECT 1 FROM chunks c
              WHERE c.branch_id = branches.id
                AND NOT EXISTS (SELECT 1 FROM chunk_vec WHERE chunk_id = c.id)
            )
          )
    """
    params: list = [EMBEDDING_VERSION, EMBEDDING_MODEL]
    if days is not None:
        where += "          AND ended_at > datetime('now', ?)\n"
        params.append(days_modifier(days))
    return where, params
