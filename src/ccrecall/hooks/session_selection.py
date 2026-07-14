"""Session selection algorithm for the SessionStart context hook.

Selection Algorithm (startup):
  Exclude current session, find most recent substantive
  (>SUBSTANTIVE_EXCHANGE_THRESHOLD exchanges) plus recent short sessions
  in remaining slots.

Selection Algorithm (clear):
  Read handoff file written by SessionEnd hook to hard-link to the exact
  cleared-from session. If not substantive, append most recent substantive
  as supplementary. Falls through to startup logic if handoff file is
  missing, stale, or session not in DB.
"""

import contextlib
import json
import sqlite3
from pathlib import Path

from whenever import Instant

from ccrecall.config import CLEAR_HANDOFF_FILENAME
from ccrecall.formatting import normalize_cwd
from ccrecall.serialization import decode_json_column

# Reject a clear-handoff written more than this many seconds ago (stale guard).
HANDOFF_STALE_SECONDS = 30

# Exchange count above which a session is considered "substantive" (rich enough
# to inject as prior context). Sessions at or below this threshold are "short"
# and fill remaining slots only after a substantive session is found.
SUBSTANTIVE_EXCHANGE_THRESHOLD = 2

# Most recent prior branches scanned when picking sessions to inject.
_CANDIDATE_LIMIT = 20


def _row_to_entry(row) -> dict:
    """Convert a candidate row to an entry dict."""
    (
        _session_id,
        uuid,
        started_at,
        ended_at,
        exchange_count,
        files_json,
        commits_json,
        git_branch,
        branch_db_id,
        context_summary,
    ) = row
    return {
        "uuid": uuid,
        "started_at": started_at,
        "ended_at": ended_at,
        "exchange_count": exchange_count,
        "files_modified": decode_json_column(files_json, []),
        "commits": decode_json_column(commits_json, []),
        "git_branch": git_branch,
        "branch_db_id": branch_db_id,
        "context_summary": context_summary,
    }


_CANDIDATE_BASE = """
    SELECT s.id, s.uuid, b.started_at, b.ended_at, b.exchange_count,
           b.files_modified, b.commits, s.git_branch, b.id as branch_db_id,
           b.context_summary
    FROM sessions s
    JOIN branches b ON b.session_id = s.id AND b.is_active = 1
    WHERE s.project_id = ?
      AND s.uuid != ?
      AND s.parent_session_id IS NULL"""

_CANDIDATE_QUERY = f"{_CANDIDATE_BASE} ORDER BY b.ended_at DESC LIMIT {_CANDIDATE_LIMIT}"

_CANDIDATE_CWD_QUERY = (
    f"{_CANDIDATE_BASE} AND REPLACE(s.cwd, '\\\\', '/') = ? ORDER BY b.ended_at DESC LIMIT {_CANDIDATE_LIMIT}"
)

_SESSION_BY_UUID_QUERY = """
    SELECT s.id, s.uuid, b.started_at, b.ended_at, b.exchange_count,
           b.files_modified, b.commits, s.git_branch, b.id as branch_db_id,
           b.context_summary
    FROM sessions s
    JOIN branches b ON b.session_id = s.id AND b.is_active = 1
    WHERE s.project_id = ?
      AND s.uuid = ?
      AND s.parent_session_id IS NULL
    ORDER BY b.ended_at DESC
    LIMIT 1
"""


def _filter_candidates(rows: list, max_sessions: int) -> list[dict]:
    """Apply the substantive/short filtering to raw candidate rows."""
    short_sessions: list[dict] = []
    substantive = None
    for row in rows:
        entry = _row_to_entry(row)
        if entry["exchange_count"] < SUBSTANTIVE_EXCHANGE_THRESHOLD:
            continue
        if entry["exchange_count"] == SUBSTANTIVE_EXCHANGE_THRESHOLD:
            short_sessions.append(entry)
            continue
        substantive = entry
        break

    if substantive:
        recent_shorts = short_sessions[: max_sessions - 1]
        return [*recent_shorts, substantive]
    return short_sessions[:max_sessions]


def _find_first_substantive(cursor, project_id: int, exclude_uuid: str) -> dict | None:
    """Find the most recent substantive session, excluding a given uuid."""
    cursor.execute(_CANDIDATE_QUERY, (project_id, exclude_uuid))
    for row in cursor.fetchall():
        entry = _row_to_entry(row)
        if entry["exchange_count"] > SUBSTANTIVE_EXCHANGE_THRESHOLD:
            return entry
    return None


def _load_messages_for(cursor, entries: list[dict]) -> None:
    """Load messages for entries that lack a cached context_summary, in-place."""
    uncached_ids = [s["branch_db_id"] for s in entries if not s.get("context_summary")]
    if not uncached_ids:
        return

    placeholders = ",".join("?" * len(uncached_ids))
    cursor.execute(
        f"""
        SELECT bm.branch_id, m.role, m.content, m.timestamp
        FROM branch_messages bm
        JOIN messages m ON bm.message_id = m.id
        WHERE bm.branch_id IN ({placeholders})
          AND COALESCE(m.is_notification, 0) = 0
        ORDER BY bm.branch_id, m.timestamp ASC
    """,
        uncached_ids,
    )

    branch_messages: dict[int, list[dict]] = {}
    for branch_id, role, content, timestamp in cursor.fetchall():
        branch_messages.setdefault(branch_id, []).append({"role": role, "content": content, "timestamp": timestamp})

    for entry in entries:
        if not entry.get("context_summary"):
            entry["messages"] = branch_messages.get(entry["branch_db_id"], [])


def _finalize(entries: list[dict]) -> list[dict]:
    """Strip internal branch_db_id from entries before returning."""
    for entry in entries:
        entry.pop("branch_db_id", None)
    return entries


def _find_cleared_from_session_uuid(db_path: Path, cwd: str) -> str | None:
    """Read and consume the clear-handoff file written by the SessionEnd hook.

    Returns the previous session_id if valid (recent, same cwd), otherwise None.
    Deletes on: valid consumption, stale timestamp, corrupt JSON.
    Preserves on: cwd mismatch (another session may claim it).
    """
    handoff_path = db_path.parent / CLEAR_HANDOFF_FILENAME
    if not handoff_path.exists():
        return None
    try:
        handoff_data = json.loads(handoff_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        with contextlib.suppress(OSError):
            handoff_path.unlink()
        return None

    session_id = handoff_data.get("session_id")
    handoff_cwd = handoff_data.get("cwd")
    timestamp_str = handoff_data.get("timestamp")

    # Validate before consuming: wrong cwd means this handoff belongs to another session.
    # Normalize both sides so worktree paths (.claude/worktrees/<name>) match the base repo.
    if not session_id or normalize_cwd(handoff_cwd or "") != normalize_cwd(cwd):
        return None

    # Stale guard: reject handoffs older than HANDOFF_STALE_SECONDS
    if timestamp_str:
        try:
            written = Instant.parse_iso(timestamp_str)
            # TimeDelta.total() takes a unit string; this is age in seconds.
            age = (Instant.now() - written).total("seconds")
            if age > HANDOFF_STALE_SECONDS:
                with contextlib.suppress(OSError):
                    handoff_path.unlink()
                return None
        except ValueError:
            # Unparseable timestamp — treat as invalid; delete and reject
            with contextlib.suppress(OSError):
                handoff_path.unlink()
            return None

    # Consume the file only after validation passes
    with contextlib.suppress(OSError):
        handoff_path.unlink()

    return session_id


def _select_cleared_sessions(cursor, project_id: int, prev_session_uuid: str, max_sessions: int) -> list[dict] | None:
    """Hard-link to the cleared-from session, plus an optional supplementary substantive one.

    Returns the session list, or None to fall through to startup selection (session not
    in the DB, or it had zero exchanges).
    """
    cursor.execute(_SESSION_BY_UUID_QUERY, (project_id, prev_session_uuid))
    prev_row = cursor.fetchone()
    if not prev_row:
        return None

    cleared_from = _row_to_entry(prev_row)
    if cleared_from["exchange_count"] <= 0:
        return None

    filtered = [cleared_from]
    # If cleared-from is not substantive, add the most recent substantive session.
    if cleared_from["exchange_count"] <= SUBSTANTIVE_EXCHANGE_THRESHOLD and max_sessions > 1:
        supplementary = _find_first_substantive(cursor, project_id, prev_session_uuid)
        if supplementary:
            filtered.append(supplementary)
    return filtered


def select_sessions(
    conn: sqlite3.Connection,
    project_key: str,
    current_session_id: str,
    max_sessions: int,
    source: str = "startup",
    db_path: Path | None = None,
    cwd: str = "",
) -> list[dict]:
    """
    Select sessions for context using the exchange-count algorithm.

    On startup: exclude current session, find most recent substantive + recent shorts.
    On clear: read handoff file written by SessionEnd hook to hard-link to
              the exact cleared-from session by its session_id.
              If cleared-from is not substantive (≤2 exchanges), also append the most
              recent substantive session as supplementary context.
              Falls through to startup logic if cleared-from session can't be identified.
    """
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM projects WHERE key = ?", (project_key,))
    row = cursor.fetchone()
    if not row:
        return []
    project_id = row[0]

    # Clear path: hard-link via handoff file written by SessionEnd hook
    if source == "clear" and db_path is not None:
        prev_session_uuid = _find_cleared_from_session_uuid(db_path, cwd)
        if prev_session_uuid:
            cleared = _select_cleared_sessions(cursor, project_id, prev_session_uuid, max_sessions)
            if cleared is not None:
                _load_messages_for(cursor, cleared)
                return _finalize(cleared)
        # Session not found in DB or no recent /clear — fall through to startup logic

    # Startup path (also fallback for clear with no handoff)
    # Try cwd-scoped first so worktree sessions don't bleed across worktrees.
    normalized_cwd = cwd.replace("\\", "/") if cwd else ""
    if normalized_cwd:
        cursor.execute(_CANDIDATE_CWD_QUERY, (project_id, current_session_id, normalized_cwd))
        filtered = _filter_candidates(cursor.fetchall(), max_sessions)
        if filtered:
            _load_messages_for(cursor, filtered)
            return _finalize(filtered)

    # Fallback: project-wide (all worktrees / cwds under the same project key)
    cursor.execute(_CANDIDATE_QUERY, (project_id, current_session_id))
    filtered = _filter_candidates(cursor.fetchall(), max_sessions)

    if not filtered:
        return []

    _load_messages_for(cursor, filtered)
    return _finalize(filtered)
