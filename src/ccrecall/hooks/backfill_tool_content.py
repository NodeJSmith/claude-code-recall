"""Backfill ``messages.tool_content`` for sessions synced before tool-content
extraction existed, by re-parsing their original JSONL transcript files.

Opt-in: invoke manually via ``ccrecall backfill tool-content``. NOT auto-spawned
on SessionStart (re-parsing every historical transcript is I/O-bound and only
useful once per migration, not on every session).

For each eligible session, re-parses the transcript with the same pipeline
``sync_session`` uses (``parse_all_with_uuids`` + ``find_all_branches``), then:

  - Updates ``tool_content`` on every already-existing ``messages`` row
    (matched by ``session_id, uuid``) whose ``tool_content`` is still NULL.
  - Inserts rows for tool-only turns that forward-sync previously skipped
    entirely (via the shared ``build_message_row``/``insert_new_messages``
    helpers, so the row-construction logic can't drift from the live sync
    path), and links each new row into ``branch_messages``.
  - Rebuilds the branch's ``aggregated_content`` and resets its
    ``embedding_version`` to NULL, so ``backfill embeddings`` re-selects it —
    without the reset, an already-embedded branch would silently never pick up
    the new tool text.

Sessions whose JSONL file no longer exists on disk are logged and skipped
(best-effort; see CLAUDE.md's Migration / Reversibility notes). All writes for
one session are wrapped in a single SAVEPOINT, released only after every step
succeeds — a crash or content error leaves that session untouched, not
half-linked.

Eligibility (a session "still needs tool_content backfill") is defined as
having at least one ``messages`` row with ``tool_content IS NULL`` — the same
condition the v4 migration leaves existing rows in and that forward-sync
never produces (``extract_text_content`` always returns a string, never
None). ``tool_content_eligibility.py`` owns that selection predicate
(``ELIGIBILITY_FROM``/``eligibility_clause``) — factored out into its own
dependency-free module rather than defined here, so ``context_alerts.py``'s
SessionStart sampling check can import it without pulling in this module's
``ccrecall.db`` import chain (and therefore fastembed/onnxruntime) onto the
hot path. It doesn't fit ``backfill_query.build_selection`` (the
chunk-embedding branch universe) either, so only the batch/no-progress-guard
constants are shared from there.

For the same reason, ``--status`` here doesn't call
``backfill_status.run_status``/``count_status``: those are hard-wired to the
chunk-embedding domain (``CHUNK_EMBEDDABLE_BRANCH_FILTER``, ``chunk_vec``,
``EMBEDDING_VERSION``/``EMBEDDING_MODEL``, the content-error sentinel), none
of which has a session-grain tool_content equivalent (there's no "errored"
concept for a re-parse backfill, and the universe is sessions, not chunks).
Only ``format_duration`` — the one grain-agnostic piece — is shared from
``backfill_status``; the counting and report shape are re-derived here from
``tool_content_eligibility``.

The outer batch-loop scaffolding (select → limit cutoff → stuck-batch
detection → dispatch → trailer), which this module *does* share with
``backfill_embeddings.run()``, lives in ``backfill_runner.py``. ``run()``
here keeps its own no-progress recovery (exclude the stuck ids and continue,
rather than aborting) and progress reporting; the per-session exception
taxonomy and SAVEPOINT handling live in ``_backfill_one_session``, called
once per row from ``process_batch``'s per-item loop.
"""

import contextlib
import json
import logging
import sqlite3
import sys
import time
from enum import Enum, auto
from pathlib import Path

from ccrecall.branch_ops import insert_branch_message_links
from ccrecall.config import DEFAULT_DB_PATH, load_settings_for_db, setup_logging
from ccrecall.content import extract_text_content
from ccrecall.db import get_connection
from ccrecall.db_vec import VEC_BUSY_TIMEOUT_MS
from ccrecall.hooks.backfill_query import (
    BACKFILL_BATCH_DELAY_SECONDS,
    BACKFILL_NICE_LEVEL,
    BATCH_SIZE,
    DEFAULT_PROGRESS_EVERY,
    EXIT_ABORT,
    EXIT_OK,
)
from ccrecall.hooks.backfill_runner import limit_reached, lower_scheduling_priority, run_batch_loop
from ccrecall.hooks.backfill_status import format_duration
from ccrecall.hooks.tool_content_eligibility import ELIGIBILITY_FROM, MAX_SQL_PARAMS, eligibility_clause
from ccrecall.import_log_ops import import_log_source_index
from ccrecall.message_ops import insert_new_messages
from ccrecall.parsing import (
    build_aggregated_content,
    find_all_branches,
    is_insertable_message,
    parse_all_with_uuids,
)
from ccrecall.tool_content_status import count_eligible, count_pending_missing_jsonl, count_total_sessions

_PRINT_PREFIX = "ccrecall backfill tool-content"
_LOG_PREFIX = "Backfill tool-content"
_SAVEPOINT_NAME = "session"
_LOCK_RETRIES = 3
_LOCK_BACKOFF_SECONDS = 2.0


class _SessionOutcome(Enum):
    """Result of attempting to backfill one session — process_batch dispatches
    its per-item counter/exclude_ids bookkeeping on this instead of catching
    exceptions itself, keeping its for-loop body flat."""

    BACKFILLED = auto()
    MISSING = auto()
    EMPTY = auto()
    CONTENT_ERROR = auto()
    DB_LOCK = auto()


@contextlib.contextmanager
def savepoint(cursor: sqlite3.Cursor):
    """SAVEPOINT wrapper: releases on success, rolls back + releases on error."""
    cursor.execute(f"SAVEPOINT {_SAVEPOINT_NAME}")
    try:
        yield
    except BaseException:
        cursor.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT_NAME}")
        cursor.execute(f"RELEASE SAVEPOINT {_SAVEPOINT_NAME}")
        raise
    cursor.execute(f"RELEASE SAVEPOINT {_SAVEPOINT_NAME}")


class LockExhaustedError(Exception):
    """Raised when all retry attempts for a transient DB lock are exhausted."""


def backfill_with_retry(
    cursor: sqlite3.Cursor,
    session_id: int,
    session_uuid: str,
    filepaths: list[Path],
    logger: logging.Logger,
) -> bool:
    """Run backfill_session with bounded retry on transient DB locks.

    Returns the result of backfill_session on success. Raises LockExhaustedError
    if all retries are exhausted. Non-lock OperationalErrors (e.g. schema errors
    like "no such column") are not transient, so they're re-raised immediately
    instead of being retried and misreported as a DB-lock skip — they propagate
    to the batch-level abort handler in run(). Other exceptions (OSError,
    content errors) propagate unchanged.
    """
    for attempt in range(_LOCK_RETRIES):
        try:
            with savepoint(cursor):
                return backfill_session(cursor, session_id, filepaths, logger)
        except sqlite3.OperationalError as exc:  # noqa: PERF203 — retry loop; the try/except IS the mechanism, not incidental control flow
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            logger.warning(
                "%s: session %s transient DB error (attempt %s/%s): %s",
                _LOG_PREFIX,
                session_uuid,
                attempt + 1,
                _LOCK_RETRIES,
                exc,
            )
            if attempt < _LOCK_RETRIES - 1:
                time.sleep(_LOCK_BACKOFF_SECONDS * (attempt + 1))
    raise LockExhaustedError(session_uuid)


def run(
    *,
    status: bool = False,
    json_mode: bool = False,
    days: int | None = None,
    limit: int | None = None,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
    verbose: bool = False,
    db: Path = DEFAULT_DB_PATH,
) -> int:
    """Backfill tool_content for existing synced sessions (opt-in; not auto-spawned)."""
    if days is not None and days < 1:
        raise ValueError("days must be >= 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")

    settings = load_settings_for_db(db)
    logger = setup_logging(settings, process_name="backfill-tool-content", verbose=verbose)

    if status:
        return run_tool_content_status(days=days, json_mode=json_mode, settings=settings, logger=logger)

    # Background I/O-bound job: lower scheduling priority so interactive work
    # wins (machines.md thrash risk).
    lower_scheduling_priority(BACKFILL_NICE_LEVEL)

    total_updated = 0
    skipped_missing = 0
    skipped_empty = 0
    skipped_content_error = 0
    skipped_db_lock = 0
    skipped_stuck = 0
    last_progress = 0
    exclude_ids: set[int] = set()
    started = time.monotonic()

    try:
        with get_connection(settings, load_vec=False) as conn:
            # Raise busy_timeout: this backfill contends with the sync hook the
            # same way vec writers do. Reuses VEC_BUSY_TIMEOUT_MS (30s) rather
            # than the base 5s that load_vec=False connections get.
            conn.execute(f"PRAGMA busy_timeout = {VEC_BUSY_TIMEOUT_MS}")
            cursor = conn.cursor()

            filepath_by_uuid = build_filepath_index(cursor, logger)

            total_eligible = count_eligible(cursor, days)
            if limit is not None:
                total_eligible = min(total_eligible, limit)

            logger.info("%s: starting, %s sessions pending", _LOG_PREFIX, total_eligible)
            print(f"{_PRINT_PREFIX}: starting, {total_eligible} pending", file=sys.stderr)

            def select_batch_cb() -> list[tuple[int, str]]:
                return select_batch(cursor, exclude_ids, days)

            def is_limit_reached() -> bool:
                return limit_reached(limit, total_updated)

            def on_stuck(current_ids: list[int]) -> bool:
                nonlocal skipped_stuck
                # Defense-in-depth: with Fix 1 (backfill_session unconditionally
                # stamping remaining NULL rows to '' before returning True), a
                # session should always leave the eligible set on its first
                # attempt, making this path nearly unreachable. Kept as a guard
                # against any future regression that reintroduces a re-selectable
                # no-op session.
                logger.warning(
                    "%s: same batch re-selected (session ids: %s); excluding and continuing",
                    _LOG_PREFIX,
                    current_ids,
                )
                exclude_ids.update(current_ids)
                skipped_stuck += len(current_ids)
                return False

            def process_batch(rows: list[tuple[int, str]]) -> bool:
                nonlocal total_updated, skipped_missing, skipped_empty, skipped_content_error, skipped_db_lock
                nonlocal last_progress
                try:
                    for session_id, session_uuid in rows:
                        if is_limit_reached():
                            break

                        outcome = _backfill_one_session(cursor, session_id, session_uuid, filepath_by_uuid, logger)
                        if outcome is not _SessionOutcome.BACKFILLED:
                            # A skipped session stays eligible forever (the
                            # eligibility WHERE clause alone won't exclude it),
                            # so exclude it for the rest of this run. Only
                            # BACKFILLED means backfill_session guaranteed
                            # (#81 Fix 1) that this session already left the
                            # eligibility predicate's NULL set on its own.
                            exclude_ids.add(session_id)
                            if outcome is _SessionOutcome.MISSING:
                                skipped_missing += 1
                            elif outcome is _SessionOutcome.EMPTY:
                                skipped_empty += 1
                            elif outcome is _SessionOutcome.CONTENT_ERROR:
                                skipped_content_error += 1
                            elif outcome is _SessionOutcome.DB_LOCK:
                                skipped_db_lock += 1
                            else:
                                raise AssertionError(f"unhandled session outcome: {outcome!r}")
                            continue

                        total_updated += 1
                        if total_updated - last_progress >= progress_every:
                            msg = _format_tool_content_progress(
                                total_updated=total_updated,
                                total_eligible=total_eligible,
                                elapsed=time.monotonic() - started,
                            )
                            logger.info("%s: %s", _LOG_PREFIX, msg)
                            print(f"{_PRINT_PREFIX}: {msg}", file=sys.stderr)
                            last_progress = total_updated
                except AssertionError:
                    # An unhandled _SessionOutcome member is a programming bug, not a
                    # per-session failure — let it propagate instead of being reported
                    # as one.
                    raise
                except Exception:
                    logger.exception(
                        "%s: session failure (batch session ids: %s), aborting",
                        _LOG_PREFIX,
                        [r[0] for r in rows],
                    )
                    conn.commit()
                    return False
                return True

            def after_batch(_rows: list[tuple[int, str]]) -> None:
                conn.commit()
                time.sleep(BACKFILL_BATCH_DELAY_SECONDS)

            if not run_batch_loop(
                select_batch=select_batch_cb,
                is_limit_reached=is_limit_reached,
                process_batch=process_batch,
                on_stuck=on_stuck,
                after_batch=after_batch,
            ):
                return EXIT_ABORT
    except (sqlite3.Error, OSError) as e:
        logger.exception("%s: aborted", _LOG_PREFIX)
        print(f"{_PRINT_PREFIX}: aborted: {e}", file=sys.stderr)
        return EXIT_ABORT

    elapsed = time.monotonic() - started
    logger.info(
        "%s complete: %s sessions backfilled, %s skipped (missing JSONL), "
        "%s skipped (no usable branch), %s skipped (content error), "
        "%s skipped (DB lock), %s skipped (stuck) in %s",
        _LOG_PREFIX,
        total_updated,
        skipped_missing,
        skipped_empty,
        skipped_content_error,
        skipped_db_lock,
        skipped_stuck,
        format_duration(elapsed),
    )
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "complete",
                    "backfilled": total_updated,
                    "skipped_missing": skipped_missing,
                    "skipped_empty": skipped_empty,
                    "skipped_content_error": skipped_content_error,
                    "skipped_db_lock": skipped_db_lock,
                    "skipped_stuck": skipped_stuck,
                    "elapsed_seconds": round(elapsed, 1),
                }
            )
        )
    else:
        print(
            f"{_PRINT_PREFIX}: complete — {total_updated} sessions backfilled, "
            f"{skipped_missing} skipped (missing JSONL), {skipped_empty} skipped (no usable branch), "
            f"{skipped_content_error} skipped (content error), "
            f"{skipped_db_lock} skipped (DB lock), {skipped_stuck} skipped (stuck) in {format_duration(elapsed)}",
            file=sys.stderr,
        )
    return EXIT_OK


def _backfill_one_session(
    cursor: sqlite3.Cursor,
    session_id: int,
    session_uuid: str,
    filepath_by_uuid: dict[str, list[Path]],
    logger: logging.Logger,
) -> _SessionOutcome:
    """Attempt to backfill one session; returns a `_SessionOutcome`.

    Session-level failures (missing JSONL, lock exhaustion, vanished file,
    content error, no usable branch) are caught and converted to an outcome
    here so process_batch's per-item loop stays flat. Exceptions outside this
    taxonomy (infra/session failure) still propagate to process_batch's own
    `except Exception` batch-abort handler.
    """
    filepaths = filepath_by_uuid.get(session_uuid)
    if filepaths is None:
        logger.warning("%s: session %s has no on-disk JSONL, skipping", _LOG_PREFIX, session_uuid)
        return _SessionOutcome.MISSING

    try:
        made_change = backfill_with_retry(cursor, session_id, session_uuid, filepaths, logger)
    except LockExhaustedError:
        logger.warning(
            "%s: session %s (id=%s) DB lock persisted after %s retries, skipping",
            _LOG_PREFIX,
            session_uuid,
            session_id,
            _LOCK_RETRIES,
        )
        return _SessionOutcome.DB_LOCK
    except OSError:
        logger.warning("%s: session %s JSONL vanished mid-run, skipping", _LOG_PREFIX, session_uuid)
        return _SessionOutcome.MISSING
    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
        logger.exception(
            "%s: session %s (id=%s) content error, skipping",
            _LOG_PREFIX,
            session_uuid,
            session_id,
        )
        return _SessionOutcome.CONTENT_ERROR

    if not made_change:
        # Entries/branch/branch-row absent: tool_content stays NULL, so the
        # eligibility WHERE clause alone would keep re-selecting this session
        # forever — exclude it for the rest of this run, same as the
        # missing-file case.
        logger.warning("%s: session %s parsed to no usable branch, skipping", _LOG_PREFIX, session_uuid)
        return _SessionOutcome.EMPTY

    return _SessionOutcome.BACKFILLED


def _format_tool_content_progress(*, total_updated: int, total_eligible: int, elapsed: float) -> str:
    """Render one tool-content-backfill progress line, including ETA."""
    remaining = max(0, total_eligible - total_updated)
    rate = total_updated / elapsed if elapsed > 0 else 0.0
    eta = format_duration(remaining / rate) if rate > 0 else "?"
    return (
        f"{total_updated}/{total_eligible} sessions backfilled, "
        f"{remaining} remaining, {format_duration(elapsed)} elapsed, ETA {eta}"
    )


def build_filepath_index(cursor: sqlite3.Cursor, logger: logging.Logger) -> dict[str, list[Path]]:
    """Map session_uuid -> list of file paths for every ``import_log`` entry
    whose JSONL still exists on disk.

    A session backed by the Agent tool produces N files (a parent ``.jsonl``
    plus one ``agent-*.jsonl`` per subagent invocation), all resolving to the
    same session_uuid via ``extract_session_uuid``.  Every file is kept so
    ``backfill_session`` can merge entries from the full set.

    A missing file is logged once here (not per re-selection); a session_uuid
    with zero surviving files gets no index entry, and the caller skips it.
    """
    mapping: dict[str, list[Path]] = {}
    for session_uuid, paths in import_log_source_index(cursor).items():
        if paths["existing"]:
            mapping[session_uuid] = paths["existing"]
        for path in paths["missing"]:
            logger.warning("%s: JSONL missing on disk: %s", _LOG_PREFIX, path)
    return mapping


def select_batch(cursor: sqlite3.Cursor, exclude_ids: set[int], days: int | None) -> list[tuple[int, str]]:
    """Return up to BATCH_SIZE (session_id, session_uuid) pairs still needing
    tool_content backfill, oldest session id first."""
    where, params = eligibility_clause(days, exclude_ids)
    query = f"SELECT DISTINCT s.id, s.uuid {ELIGIBILITY_FROM} {where} ORDER BY s.id LIMIT ?"
    params = [*params, BATCH_SIZE]
    return cursor.execute(query, params).fetchall()


def backfill_session(cursor: sqlite3.Cursor, session_id: int, filepaths: list[Path], logger: logging.Logger) -> bool:
    """Re-parse one session's JSONL file(s) and backfill tool_content.

    A session may be backed by multiple files (parent + subagent transcripts);
    entries from all files are merged before the update/insert passes.

    Updates tool_content for existing rows, inserts previously-skipped
    tool-only rows (linked to the session's branch via branch_messages),
    rebuilds aggregated_content, and resets embedding_version to NULL so the
    branch re-enters `backfill embeddings`'s eligible set.

    Returns False (a no-op) when all files parse to no entries, no branch, or
    the session has no active branch row in the DB — in these cases nothing
    was written and ``messages.tool_content`` stays NULL, so the caller must
    not count it as backfilled.

    Returns True once the write pipeline actually ran. A True return
    guarantees the session has left the eligible set: any ``messages`` row
    still NULL after the update/insert passes (a uuid absent from every
    surviving JSONL file) is stamped to ``''`` before returning, so this
    session's ``tool_content IS NULL`` count is always zero afterward — it
    cannot be re-selected in a later batch, and it stops re-firing the
    SessionStart tool-content alert (issue #81).

    Raises OSError if a file can no longer be opened (race with a concurrent
    delete) — the caller treats that like the missing-file case.
    """
    all_entries: list[dict] = []
    for filepath in filepaths:
        all_entries.extend(parse_all_with_uuids(filepath))
    if not all_entries:
        return False

    branches = find_all_branches(all_entries)
    if not branches:
        return False
    branch = branches[0]

    cursor.execute("SELECT id FROM branches WHERE session_id = ? AND is_active = 1", (session_id,))
    branch_row = cursor.fetchone()
    if branch_row is None:
        return False
    branch_db_id = branch_row[0]

    cursor.execute("SELECT uuid FROM messages WHERE session_id = ? AND uuid IS NOT NULL", (session_id,))
    existing_uuids = {row[0] for row in cursor.fetchall()}

    # UPDATE pass: populate tool_content on every already-existing row. Scoped
    # to entries whose uuid already has a messages row -- independent of
    # valid_branch_uuids, so a row belonging to a historical branch state
    # still gets backfilled rather than staying NULL forever.
    for entry in all_entries:
        if not is_insertable_message(entry):
            continue
        uuid = entry.get("uuid")
        if not uuid or uuid not in existing_uuids:
            continue
        content = entry.get("message", {}).get("content", "")
        _text, _has_tool_use, _has_thinking, _tool_summary, tool_content = extract_text_content(content)
        cursor.execute(
            "UPDATE messages SET tool_content = ? WHERE session_id = ? AND uuid = ? AND tool_content IS NULL",
            (tool_content, session_id, uuid),
        )

    # INSERT pass: tool-only turns previously skipped for lack of any content.
    # insert_new_messages/build_message_row already skip uuids present in
    # existing_uuids, so calling it on the full message list only inserts
    # genuinely new rows — the row-construction logic isn't reimplemented here.
    messages = [e for e in all_entries if is_insertable_message(e)]
    valid_branch_uuids = branch["uuids"]
    before_uuids = set(existing_uuids)
    insert_new_messages(cursor, session_id, messages, valid_branch_uuids, existing_uuids)
    new_uuids = existing_uuids - before_uuids

    if new_uuids:
        new_uuids_list = list(new_uuids)
        uuid_to_msg_id: dict[str, int] = {}
        # -1 reserves one slot for the session_id bound parameter
        for i in range(0, len(new_uuids_list), MAX_SQL_PARAMS - 1):
            chunk = new_uuids_list[i : i + MAX_SQL_PARAMS - 1]
            placeholders = ",".join("?" * len(chunk))
            cursor.execute(
                f"SELECT id, uuid FROM messages WHERE session_id = ? AND uuid IN ({placeholders})",
                (session_id, *chunk),
            )
            uuid_to_msg_id.update({row[1]: row[0] for row in cursor.fetchall()})
        link_ids = {msg_id for uuid in new_uuids if (msg_id := uuid_to_msg_id.get(uuid))}
        insert_branch_message_links(cursor, branch_db_id, link_ids)

    # Rebuild aggregated_content from the branch's existing files/commits
    # metadata (unchanged by this backfill) plus the newly-populated tool
    # content, then reset the embedding and summary watermarks so both
    # `backfill embeddings` and `backfill summaries` re-select this branch —
    # without the resets an already-processed branch would silently never pick
    # up the new tool text. A concurrent sync during backfill may transiently
    # regress aggregated_content; the next sync corrects it.
    cursor.execute("SELECT files_modified, commits FROM branches WHERE id = ?", (branch_db_id,))
    files_json, commits_json = cursor.fetchone()
    files = json.loads(files_json) if files_json else None
    commits = json.loads(commits_json) if commits_json else None
    agg_content = build_aggregated_content(cursor, branch_db_id, files, commits)
    cursor.execute(
        """
        UPDATE branches
        SET aggregated_content = ?, embedding_version = NULL, summary_version = NULL
        WHERE id = ?
        """,
        (agg_content, branch_db_id),
    )

    # Guarantee (#81 Fix 1): a session must leave the eligible set once this
    # function returns True, or it gets re-selected and re-parsed forever and
    # keeps re-firing the SessionStart alert. Any messages row still NULL here
    # has a uuid absent from every surviving JSONL file for this session (the
    # UPDATE pass above only touches uuids it finds in all_entries) — transcript
    # files are append-only, so that uuid can never reappear in a later run.
    # '' is the codebase's existing meaning for "no tool content"; stamping it
    # structurally removes the row from both the backfill eligibility predicate
    # and the alert's coverage check. This runs inside the caller's SAVEPOINT,
    # so it's atomic with the rest of this session's writes.
    cursor.execute(
        "UPDATE messages SET tool_content = '' WHERE session_id = ? AND tool_content IS NULL",
        (session_id,),
    )
    if cursor.rowcount > 0:
        logger.warning(
            "%s: session id=%s had %s row(s) with uuids absent from the surviving JSONL; "
            "marked tool_content='' (unrecoverable)",
            _LOG_PREFIX,
            session_id,
            cursor.rowcount,
        )
    return True


def run_tool_content_status(
    *,
    days: int | None,
    json_mode: bool,
    settings: dict | None,
    logger: logging.Logger,
) -> int:
    """Report session coverage (backfilled/total) for tool_content (read-only)."""
    try:
        with get_connection(settings, load_vec=False) as conn:
            cursor = conn.cursor()
            pending = count_eligible(cursor, days)
            pending_missing = count_pending_missing_jsonl(cursor, days) if pending else 0
            total = count_total_sessions(cursor, days)
    except (sqlite3.Error, OSError) as e:
        logger.exception("%s: status aborted", _LOG_PREFIX)
        print(f"{_PRINT_PREFIX}: aborted: {e}", file=sys.stderr)
        return EXIT_ABORT

    done = total - pending
    pending_backfillable = pending - pending_missing
    if json_mode:
        print(
            json.dumps(
                {
                    "total_sessions": total,
                    "pending_sessions": pending,
                    "pending_backfillable_sessions": pending_backfillable,
                    "pending_missing_jsonl_sessions": pending_missing,
                    "done_sessions": done,
                    "days": days,
                }
            )
        )
        return EXIT_OK

    pct = (done / total * 100) if total else 0.0
    scope = f" (last {days}d)" if days is not None else ""
    print(f"{_PRINT_PREFIX} status{scope}:")
    print(f"  sessions:  {done} / {total} backfilled  ({pct:.0f}%)")
    print(f"  remaining: {pending} sessions")
    if pending:
        print(f"  backfillable: {pending_backfillable} sessions")
    if pending_missing:
        print(f"  missing JSONL: {pending_missing} sessions  (some or all source files missing)")
    return EXIT_OK
