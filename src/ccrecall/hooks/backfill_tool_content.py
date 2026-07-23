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
None). This module owns that selection predicate; it doesn't fit
``backfill_query.build_selection`` (the chunk-embedding branch universe), so
only the batch/no-progress-guard constants and ``--days`` helper are shared
from there.

For the same reason, ``--status`` here doesn't call
``backfill_status.run_status``/``count_status``: those are hard-wired to the
chunk-embedding domain (``CHUNK_EMBEDDABLE_BRANCH_FILTER``, ``chunk_vec``,
``EMBEDDING_VERSION``/``EMBEDDING_MODEL``, the content-error sentinel), none
of which has a session-grain tool_content equivalent (there's no "errored"
concept for a re-parse backfill, and the universe is sessions, not chunks).
Only ``format_duration`` — the one grain-agnostic piece — is shared from
``backfill_status``; the counting and report shape are re-derived here from
``_ELIGIBILITY_FROM``/``_eligibility_clause``, this module's own single
source of truth.
"""

import contextlib
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from ccrecall.config import load_settings, setup_logging
from ccrecall.content import extract_text_content
from ccrecall.db import get_connection
from ccrecall.hooks.backfill_query import (
    BACKFILL_BATCH_DELAY_SECONDS,
    BACKFILL_NICE_LEVEL,
    BATCH_SIZE,
    DEFAULT_PROGRESS_EVERY,
    EXIT_ABORT,
    EXIT_OK,
    days_modifier,
)
from ccrecall.hooks.backfill_status import format_duration
from ccrecall.message_ops import insert_new_messages
from ccrecall.parsing import (
    build_aggregated_content,
    extract_session_uuid,
    find_all_branches,
    is_insertable_message,
    parse_all_with_uuids,
)

_PRINT_PREFIX = "ccrecall backfill tool-content"
_LOG_PREFIX = "Backfill tool-content"
_SAVEPOINT_NAME = "session"

# Shared FROM clause for every "sessions needing tool_content backfill" query
# (the eligible-count, the per-batch selection, and --status) so the join
# shape can't drift between them.
_ELIGIBILITY_FROM = """
    FROM messages m
    JOIN sessions s ON s.id = m.session_id
    JOIN branches b ON b.session_id = s.id AND b.is_active = 1
"""


def run(
    *,
    status: bool = False,
    json_mode: bool = False,
    days: int | None = None,
    limit: int | None = None,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
    verbose: bool = False,
) -> int:
    """Backfill tool_content for existing synced sessions (opt-in; not auto-spawned)."""
    if days is not None and days < 1:
        raise ValueError("days must be >= 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")

    settings = load_settings()
    logger = setup_logging(settings, process_name="backfill-tool-content", verbose=verbose)

    if status:
        return _run_status(days=days, json_mode=json_mode, settings=settings, logger=logger)

    # Background I/O-bound job: lower scheduling priority so interactive work
    # wins (machines.md thrash risk). Best-effort — os.nice is POSIX-only.
    with contextlib.suppress(AttributeError, OSError):
        os.nice(BACKFILL_NICE_LEVEL)

    total_updated = 0
    skipped_missing = 0
    skipped_empty = 0
    last_progress = 0
    last_batch_ids: list[int] | None = None
    exclude_ids: set[int] = set()
    started = time.monotonic()

    try:
        with get_connection(settings, load_vec=False) as conn:
            cursor = conn.cursor()

            filepath_by_uuid = _build_filepath_index(cursor, logger)

            total_eligible = _count_eligible(cursor, days)
            if limit is not None:
                total_eligible = min(total_eligible, limit)

            logger.info("%s: starting, %s sessions pending", _LOG_PREFIX, total_eligible)
            print(f"{_PRINT_PREFIX}: starting, {total_eligible} pending", file=sys.stderr)

            while True:
                if limit is not None and total_updated >= limit:
                    break

                rows = _select_batch(cursor, exclude_ids, days)
                if not rows:
                    break

                current_ids = [r[0] for r in rows]
                if current_ids == last_batch_ids:
                    logger.error(
                        "%s: no progress — same batch re-selected; aborting to avoid infinite loop", _LOG_PREFIX
                    )
                    print(
                        f"{_PRINT_PREFIX}: no progress — same batch re-selected, aborting",
                        file=sys.stderr,
                    )
                    return EXIT_ABORT
                last_batch_ids = current_ids

                try:
                    for session_id, session_uuid in rows:
                        if limit is not None and total_updated >= limit:
                            break

                        filepath = filepath_by_uuid.get(session_uuid)
                        if filepath is None:
                            logger.warning("%s: session %s has no on-disk JSONL, skipping", _LOG_PREFIX, session_uuid)
                            skipped_missing += 1
                            exclude_ids.add(session_id)
                            continue

                        cursor.execute(f"SAVEPOINT {_SAVEPOINT_NAME}")
                        try:
                            made_change = _backfill_session(cursor, session_id, filepath)
                        except OSError:
                            cursor.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT_NAME}")
                            cursor.execute(f"RELEASE SAVEPOINT {_SAVEPOINT_NAME}")
                            logger.warning("%s: session %s JSONL vanished mid-run, skipping", _LOG_PREFIX, session_uuid)
                            skipped_missing += 1
                            exclude_ids.add(session_id)
                            continue
                        except Exception:
                            cursor.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT_NAME}")
                            cursor.execute(f"RELEASE SAVEPOINT {_SAVEPOINT_NAME}")
                            raise
                        cursor.execute(f"RELEASE SAVEPOINT {_SAVEPOINT_NAME}")
                        if not made_change:
                            # Entries/branch/branch-row absent: tool_content stays
                            # NULL, so the eligibility WHERE clause alone would keep
                            # re-selecting this session forever — exclude it for the
                            # rest of this run, same as the missing-file case.
                            logger.warning(
                                "%s: session %s parsed to no usable branch, skipping", _LOG_PREFIX, session_uuid
                            )
                            skipped_empty += 1
                            exclude_ids.add(session_id)
                            continue
                        total_updated += 1
                        # No exclude_ids entry needed here: a real backfill sets
                        # tool_content on every matched row, so the eligibility
                        # clause's own `m.tool_content IS NULL` predicate already
                        # keeps this session out of future batches this run —
                        # unlike the two skip paths above, whose rows stay NULL.

                        if total_updated - last_progress >= progress_every:
                            elapsed = time.monotonic() - started
                            remaining = max(0, total_eligible - total_updated)
                            rate = total_updated / elapsed if elapsed > 0 else 0.0
                            eta = format_duration(remaining / rate) if rate > 0 else "?"
                            msg = (
                                f"{total_updated}/{total_eligible} sessions backfilled, "
                                f"{remaining} remaining, {format_duration(elapsed)} elapsed, ETA {eta}"
                            )
                            logger.info("%s: %s", _LOG_PREFIX, msg)
                            print(f"{_PRINT_PREFIX}: {msg}", file=sys.stderr)
                            last_progress = total_updated
                except Exception:
                    # Infra/session failure (sqlite3.Error, unexpected bug): abort the
                    # whole run without marking further rows — they stay eligible on
                    # the next invocation. Commit whatever prior batches landed.
                    logger.exception("%s: session failure, aborting", _LOG_PREFIX)
                    conn.commit()
                    return EXIT_ABORT

                conn.commit()
                time.sleep(BACKFILL_BATCH_DELAY_SECONDS)
    except (sqlite3.Error, OSError) as e:
        logger.exception("%s: aborted", _LOG_PREFIX)
        print(f"{_PRINT_PREFIX}: aborted: {e}", file=sys.stderr)
        return EXIT_ABORT

    elapsed = time.monotonic() - started
    logger.info(
        "%s complete: %s sessions backfilled, %s skipped (missing JSONL), %s skipped (no usable branch) in %s",
        _LOG_PREFIX,
        total_updated,
        skipped_missing,
        skipped_empty,
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
                    "elapsed_seconds": round(elapsed, 1),
                }
            )
        )
    else:
        print(
            f"{_PRINT_PREFIX}: complete — {total_updated} sessions backfilled, "
            f"{skipped_missing} skipped (missing JSONL), {skipped_empty} skipped (no usable branch) "
            f"in {format_duration(elapsed)}",
            file=sys.stderr,
        )
    return EXIT_OK


def _build_filepath_index(cursor: sqlite3.Cursor, logger: logging.Logger) -> dict[str, Path]:
    """Map session_uuid -> file_path for every ``import_log`` entry whose JSONL
    still exists on disk.

    A missing file is logged once here (not per re-selection); the caller
    treats a missing index entry as "no file to backfill this session from"
    and skips it. Subagent transcripts (``agent-<uuid>.jsonl``) resolve to the
    same session_uuid as their parent (``extract_session_uuid`` strips the
    prefix) — whichever import_log row is read last for a given uuid wins,
    harmless since both describe the same session.
    """
    mapping: dict[str, Path] = {}
    for (file_path,) in cursor.execute("SELECT file_path FROM import_log").fetchall():
        path = Path(file_path)
        if path.exists():
            mapping[extract_session_uuid(path)] = path
        else:
            logger.warning("%s: JSONL missing on disk: %s", _LOG_PREFIX, file_path)
    return mapping


def _eligibility_clause(days: int | None, exclude_ids: set[int] | None = None) -> tuple[str, list]:
    """WHERE clause (+ params) for "sessions still needing tool_content backfill".

    Single source of truth for the one-time eligible count and the per-batch
    selection query, mirroring backfill_query.build_selection's pattern so the
    two can't drift. ``exclude_ids`` removes sessions this run already
    attempted (succeeded, errored, or had no on-disk file) so a stalled
    session can't force the no-progress guard to fire on every batch.
    """
    where = "WHERE m.tool_content IS NULL"
    params: list = []
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        where += f" AND s.id NOT IN ({placeholders})"
        params.extend(exclude_ids)
    if days is not None:
        where += " AND b.ended_at > datetime('now', ?)"
        params.append(days_modifier(days))
    return where, params


def _count_eligible(cursor: sqlite3.Cursor, days: int | None) -> int:
    where, params = _eligibility_clause(days)
    return cursor.execute(f"SELECT COUNT(DISTINCT s.id) {_ELIGIBILITY_FROM} {where}", params).fetchone()[0]


def _count_total_sessions(cursor: sqlite3.Cursor, days: int | None) -> int:
    """Count every session with messages (the backfill's universe), for --status."""
    where = "WHERE 1=1"
    params: list = []
    if days is not None:
        where += " AND b.ended_at > datetime('now', ?)"
        params.append(days_modifier(days))
    return cursor.execute(f"SELECT COUNT(DISTINCT s.id) {_ELIGIBILITY_FROM} {where}", params).fetchone()[0]


def _select_batch(cursor: sqlite3.Cursor, exclude_ids: set[int], days: int | None) -> list[tuple[int, str]]:
    """Return up to BATCH_SIZE (session_id, session_uuid) pairs still needing
    tool_content backfill, oldest session id first."""
    where, params = _eligibility_clause(days, exclude_ids)
    query = f"SELECT DISTINCT s.id, s.uuid {_ELIGIBILITY_FROM} {where} ORDER BY s.id LIMIT ?"
    params = [*params, BATCH_SIZE]
    return cursor.execute(query, params).fetchall()


def _backfill_session(cursor: sqlite3.Cursor, session_id: int, filepath: Path) -> bool:
    """Re-parse one session's JSONL and backfill tool_content for its messages.

    Updates tool_content for existing rows, inserts previously-skipped
    tool-only rows (linked to the session's branch via branch_messages),
    rebuilds aggregated_content, and resets embedding_version to NULL so the
    branch re-enters `backfill embeddings`'s eligible set.

    Returns False (a no-op) when the file parses to no entries, no branch, or
    the session has no active branch row in the DB — in these cases nothing
    was written and ``messages.tool_content`` stays NULL, so the caller must
    not count it as backfilled. Returns True once the write pipeline actually
    ran.

    Raises OSError if the file can no longer be opened (race with a concurrent
    delete) — the caller treats that like the missing-file case.
    """
    all_entries = list(parse_all_with_uuids(filepath))
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
    # genuinely new rows -- the row-construction logic isn't reimplemented here.
    messages = [e for e in all_entries if is_insertable_message(e)]
    valid_branch_uuids = branch["uuids"]
    before_uuids = set(existing_uuids)
    insert_new_messages(cursor, session_id, messages, valid_branch_uuids, existing_uuids)
    new_uuids = existing_uuids - before_uuids

    if new_uuids:
        placeholders = ",".join("?" * len(new_uuids))
        cursor.execute(
            f"SELECT id, uuid FROM messages WHERE session_id = ? AND uuid IN ({placeholders})",
            (session_id, *new_uuids),
        )
        uuid_to_msg_id = {row[1]: row[0] for row in cursor.fetchall()}
        for uuid in new_uuids:
            msg_id = uuid_to_msg_id.get(uuid)
            if msg_id:
                cursor.execute(
                    "INSERT OR IGNORE INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
                    (branch_db_id, msg_id),
                )

    # Rebuild aggregated_content from the branch's existing files/commits
    # metadata (unchanged by this backfill) plus the newly-populated tool
    # content, then reset the embedding watermark so `backfill embeddings`
    # re-selects this branch -- without the reset an already-embedded branch
    # would silently never pick up the new text.
    cursor.execute("SELECT files_modified, commits FROM branches WHERE id = ?", (branch_db_id,))
    files_json, commits_json = cursor.fetchone()
    files = json.loads(files_json) if files_json else None
    commits = json.loads(commits_json) if commits_json else None
    agg_content = build_aggregated_content(cursor, branch_db_id, files, commits)
    cursor.execute(
        "UPDATE branches SET aggregated_content = ?, embedding_version = NULL WHERE id = ?",
        (agg_content, branch_db_id),
    )
    return True


def _run_status(
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
            pending = _count_eligible(cursor, days)
            total = _count_total_sessions(cursor, days)
    except (sqlite3.Error, OSError) as e:
        logger.exception("%s: status aborted", _LOG_PREFIX)
        print(f"{_PRINT_PREFIX}: aborted: {e}", file=sys.stderr)
        return EXIT_ABORT

    done = total - pending
    if json_mode:
        print(json.dumps({"total_sessions": total, "pending_sessions": pending, "done_sessions": done, "days": days}))
        return EXIT_OK

    pct = (done / total * 100) if total else 0.0
    scope = f" (last {days}d)" if days is not None else ""
    print(f"{_PRINT_PREFIX} status{scope}:")
    print(f"  sessions:  {done} / {total} backfilled  ({pct:.0f}%)")
    print(f"  remaining: {pending} sessions")
    return EXIT_OK
