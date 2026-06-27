#!/usr/bin/env python3
"""Load previous session context from the memory database for the SessionStart hook.

Selection Algorithm (startup):
  Exclude current session, find most recent substantive (>2 exchanges)
  plus recent short sessions (2 exchanges) in remaining slots.

Selection Algorithm (clear):
  Read handoff file written by SessionEnd hook to hard-link to the exact
  cleared-from session. If not substantive (≤2 exchanges), append most
  recent substantive as supplementary. Falls through to startup logic
  if handoff file is missing, stale, or session not in DB.

Output: JSON with hookSpecificOutput for context injection
"""

import contextlib
import json
import logging
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError
from whenever import Instant

from ccrecall.db import (
    CLEAR_HANDOFF_FILENAME,
    DEFAULT_SETTINGS,
    get_db_connection,
    get_db_path,
    load_config,
    load_settings,
    setup_logging,
)
from ccrecall.formatting import (
    format_time_full,
    get_project_key,
    normalize_cwd,
)
from ccrecall.health import (
    ALERT_CANT_PERSIST,
    ALERT_EMBEDDINGS_FAILING,
    build_alert_block,
    evaluate_alerts,
    probe_db,
    probe_filesystem,
    read_embedding_status,
)
from ccrecall.models import LOGGER_NAME, HookInput
from ccrecall.serialization import decode_json_column
from ccrecall.session_tail import (
    find_pending_question,
    format_pending_block,
    load_tail_entries,
    transcript_for_uuid,
)
from ccrecall.summarizer import (
    build_context_summary_json,
    render_context_summary,
)

# Reject a clear-handoff written more than this many seconds ago (stale guard).
HANDOFF_STALE_SECONDS = 30

# Uncached topic fallback truncates the first user message to this many chars.
TOPIC_PREVIEW_MAX_CHARS = 120
# Most recent prior branches scanned when picking sessions to inject.
_CANDIDATE_LIMIT = 20


def _emit_empty() -> None:
    """Print the empty SessionStart response (inject no context)."""
    print(json.dumps({}))


def _emit_with_proactive(proactive_block: str) -> None:
    """Emit hook output containing only the proactive alert block (no session context).

    Falls back to _emit_empty() when there is no proactive block to inject.
    Hook stdout must never contain bare text — only the JSON envelope.
    """
    if not proactive_block:
        _emit_empty()
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": proactive_block,
                }
            }
        )
    )


def _proactive_alert_block(
    settings: dict,
    conn: sqlite3.Connection | None,
    db_available: bool,
    *,
    _marker_path: Path | None = None,
    _snooze_path: Path | None = None,
    _status_path: Path | None = None,
) -> str:
    """Build the proactive health-alert block for SessionStart injection.

    Evaluates both proactive alert classes:
    - Filesystem writability probe (active, cheap, no DB required)
    - Embedding-status sidecar read (passive, plain file read — never imports
      fastembed/onnxruntime/sqlite_vec — hot-path invariant)
    - DB write-lock probe (active, on the already-open connection, or conn=None
      when the connection itself failed — that failure becomes a fault)

    Passes active keys through the snooze ledger (fire / suppress / auto-clear)
    and builds ONE combined block for whatever fires.

    Defensive wrapper: follows the _pending_question_block precedent — any
    exception degrades to "" so the hook is never broken and context injection is
    unaffected.

    _marker_path / _snooze_path / _status_path: test injection points for sidecar
    paths; None means use the health.py function defaults (production paths).
    """
    try:
        # 1. Filesystem probe — no DB needed, unconditional.
        fs_result = probe_filesystem() if _marker_path is None else probe_filesystem(_marker_path)

        # 2. Embedding-status sidecar — plain file read only, no vec/fastembed load.
        embedding_status = read_embedding_status() if _status_path is None else read_embedding_status(_status_path)

        # 3. DB probe — only when the DB file exists; a missing DB is not a fault
        # (fresh install). conn=None here means the connection failed (dir/WAL
        # unwritable), which probe_db correctly classifies as a persist fault.
        db_probe = probe_db(conn) if db_available else None

        # 4. Compute active alert keys and human-readable reasons.
        active_keys: set[str] = set()
        fault_reason = ""
        embedding_reason = ""

        if not fs_result.ok:
            active_keys.add(ALERT_CANT_PERSIST)
            fault_reason = fs_result.reason

        if db_probe is not None and not db_probe.ok:
            active_keys.add(ALERT_CANT_PERSIST)
            # Prefer the FS reason (more actionable) when both probes fail.
            if not fault_reason:
                fault_reason = db_probe.reason

        if embedding_status is not None:
            active_keys.add(ALERT_EMBEDDINGS_FAILING)
            # Pass the raw reason code; build_alert_block translates it to prose
            # (the mapping lives in health.py beside the REASON_* constants).
            embedding_reason = embedding_status.get("reason", "")

        # 5. Evaluate snooze ledger: fire / suppress / auto-clear.
        # load_settings() always carries alert_snooze_hours from DEFAULT_SETTINGS;
        # fall back to the canonical default only for sparse (test) settings dicts.
        snooze_hours = float(settings.get("alert_snooze_hours", DEFAULT_SETTINGS["alert_snooze_hours"]))
        keys_to_fire = (
            evaluate_alerts(active_keys, snooze_hours)
            if _snooze_path is None
            else evaluate_alerts(active_keys, snooze_hours, _snooze_path)
        )

        # 6. Build one combined block.
        return build_alert_block(
            keys_to_fire,
            fault_reason=fault_reason,
            embedding_reason=embedding_reason,
        )

    except Exception:
        # Deliberately broad: this optional alert must never break the SessionStart
        # hook or drop the main context injection. Log best-effort (no-op unless
        # logging_enabled) so the failure isn't silently lost.
        logging.getLogger(LOGGER_NAME).exception("proactive alert block failed")
        return ""


def _pending_question_block(sessions: list[dict], cwd: str) -> str:
    """Markdown warning if the most recent prior session ended on an unanswered
    AskUserQuestion, else "". ``sessions`` is select_sessions output (most-recent
    first, current session already excluded), so sessions[0] is the prior session.
    Wrapped defensively — this must never break the SessionStart hook, so any
    failure degrades to no warning.
    """
    try:
        uuid = sessions[0].get("uuid")
        if not uuid:
            return ""
        path = transcript_for_uuid(uuid, cwd=cwd)
        if not path:
            return ""
        payload = find_pending_question(load_tail_entries(path))
        if not payload:
            return ""
        return format_pending_block(payload, for_injection=True) + "\n\n"
    except Exception:
        # Deliberately broad: this optional warning must never break the
        # SessionStart hook or drop the main context injection. Log best-effort
        # (no-op unless logging_enabled) so the failure isn't silently lost.
        logging.getLogger(LOGGER_NAME).exception("pending-question block failed")
        return ""


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


_CANDIDATE_QUERY = f"""
    SELECT s.id, s.uuid, b.started_at, b.ended_at, b.exchange_count,
           b.files_modified, b.commits, s.git_branch, b.id as branch_db_id,
           b.context_summary
    FROM sessions s
    JOIN branches b ON b.session_id = s.id AND b.is_active = 1
    WHERE s.project_id = ?
      AND s.uuid != ?
      AND s.parent_session_id IS NULL
    ORDER BY b.ended_at DESC
    LIMIT {_CANDIDATE_LIMIT}
"""

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


def _find_first_substantive(cursor, project_id: int, exclude_uuid: str) -> dict | None:
    """Find the most recent substantive session (>2 exchanges), excluding a given uuid."""
    cursor.execute(_CANDIDATE_QUERY, (project_id, exclude_uuid))
    for row in cursor.fetchall():
        entry = _row_to_entry(row)
        if entry["exchange_count"] > 2:
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
        data = json.loads(handoff_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        with contextlib.suppress(OSError):
            handoff_path.unlink()
        return None

    session_id = data.get("session_id")
    handoff_cwd = data.get("cwd")
    timestamp_str = data.get("timestamp")

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
    if cleared_from["exchange_count"] <= 2 and max_sessions > 1:
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
    cursor.execute(_CANDIDATE_QUERY, (project_id, current_session_id))
    candidates = cursor.fetchall()

    short_sessions = []  # exchange_count == 2
    substantive = None
    for row in candidates:
        entry = _row_to_entry(row)

        if entry["exchange_count"] <= 1:
            continue

        if entry["exchange_count"] == 2:
            short_sessions.append(entry)
            continue

        # First substantive session found — stop searching
        substantive = entry
        break

    # Build filtered list: substantive session always gets a slot,
    # remaining slots go to short sessions that are more recent
    if substantive:
        recent_shorts = short_sessions[: max_sessions - 1]
        filtered = [*recent_shorts, substantive]
    else:
        filtered = short_sessions[:max_sessions]

    if not filtered:
        return []

    _load_messages_for(cursor, filtered)
    return _finalize(filtered)


def _build_fallback_context(session: dict) -> str:
    """Fallback for sessions without a cached context_summary.

    Builds the structured summary through the canonical
    build_context_summary_json so exchange text is truncated identically to the
    cached path, then renders it. One builder means the two paths can't drift —
    a hand-rolled copy here previously emitted untruncated exchange text.
    """
    # tool_counts isn't carried on the session dict that reaches this fallback
    # path; build_context_summary_json defaults a missing key to {}.
    branch_row = {
        "files_modified": session.get("files_modified", []),
        "commits": session.get("commits", []),
        "git_branch": session.get("git_branch"),
        "started_at": session.get("started_at"),
        "ended_at": session.get("ended_at"),
    }
    if "exchange_count" in session:
        branch_row["exchange_count"] = session["exchange_count"]
    summary_json = build_context_summary_json(branch_row, session.get("messages", []))
    return render_context_summary(summary_json)


def _extract_topic(session: dict) -> str:
    """Extract topic string from a session entry.

    Prefers cached context_summary (always present for synced sessions).
    Falls back to first user message content for uncached branches.
    """
    cached = session.get("context_summary", "")
    if cached:
        for line in cached.splitlines():
            if "**Topic:**" in line:
                part = line.split("**Topic:**", 1)[1]
                return part.split(" | ")[0].strip()
    for msg in session.get("messages", []):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            return (content[:TOPIC_PREVIEW_MAX_CHARS] + "...") if len(content) > TOPIC_PREVIEW_MAX_CHARS else content
    return ""


def build_origin_block(source: str, sessions: list[dict]) -> str:
    """Build a structured Session Origin block that tells the new session where it came from."""
    if not sessions:
        return ""

    primary = sessions[0]
    started = format_time_full(primary.get("started_at", ""))
    ended = format_time_full(primary.get("ended_at", ""))
    branch = primary.get("git_branch", "unknown")
    exchanges = primary.get("exchange_count", 0)

    source_label = "clear (continuing same session)" if source == "clear" else "startup (new session)"

    uuid = primary.get("uuid", "")
    lines = [
        "## Session Origin",
        f"- Source: {source_label}",
        f"- Session: {uuid}",
        f"- Previous session started: {started}",
        f"- Branch: {branch}",
        f"- Exchanges: {exchanges}",
        f"- Last active: {ended}",
    ]

    if source == "clear":
        topic = _extract_topic(primary)
        if topic:
            lines.append(f"- Topic: {topic}")

    if len(sessions) > 1:
        lines.append(f"- +{len(sessions) - 1} supplementary session(s) included below")

    return "\n".join(lines)


def build_context(sessions: list[dict]) -> str:
    """Build markdown context from selected sessions.

    Uses cached context_summary when available, falls back to
    truncated last-3 exchanges for uncached branches.
    """
    if not sessions:
        return ""

    parts = []
    for session in sessions:
        cached = session.get("context_summary")
        if cached:
            parts.append(cached)
        else:
            parts.append(_build_fallback_context(session))

    return "\n\n---\n\n".join(parts)


def main():
    settings = load_settings()
    logger = setup_logging(settings)

    raw = sys.stdin.read()
    try:
        hook_input = HookInput.model_validate_json(raw) if raw else HookInput()
    except ValidationError:
        hook_input = HookInput()

    cwd = hook_input.cwd
    session_id = hook_input.session_id
    # Default only when absent (None), mirroring the old .get("source", "startup");
    # an explicit "" must stay "" so the source gate below still rejects it.
    source = hook_input.source if hook_input.source is not None else "startup"

    # Only SessionStart events get proactive alerts or context injection.
    if source not in ("startup", "clear"):
        _emit_empty()
        return

    # ── DB connection — opened early for the DB probe ──────────────────────────
    # We attempt the connection here so probe_db has a live conn to work with.
    # A connection failure (dir/WAL unwritable) leaves conn=None; probe_db(None)
    # correctly classifies that as a persist fault.
    # The db_path.exists() guard prevents creating a fresh DB on a first-run
    # install where the DB hasn't been initialised yet (not a fault condition).
    db_path = get_db_path(settings)
    db_available = db_path.exists()
    conn: sqlite3.Connection | None = None
    if db_available:
        try:
            conn = get_db_connection(settings)
        except Exception:
            # conn stays None; DB probe will report this as a persist fault.
            logger.debug("DB connection failed — DB probe will report fault")

    # ── Proactive alert evaluation ──────────────────────────────────────────────
    # Must run before ALL early-return gates so alerts fire even when sessions is
    # empty, the DB is inaccessible, or onboarding is incomplete.
    proactive_block = _proactive_alert_block(settings, conn, db_available)

    # ── Gate: onboarding incomplete ────────────────────────────────────────────
    config = load_config()
    if not config.get("onboarding_completed"):
        # The proactive write-failure alert (if active) still surfaces here:
        # it explains why onboarding config.json can't be saved. onboarding.py
        # fires as a separate hook with its own injection; if both would appear,
        # the write-failure alert wins in this hook (it's the cause of the blockage).
        if conn is not None:
            conn.close()
        _emit_with_proactive(proactive_block)
        return

    # ── Gate: context injection disabled ───────────────────────────────────────
    if not settings.get("auto_inject_context", True):
        logger.info("Context injection disabled by settings")
        if conn is not None:
            conn.close()
        _emit_with_proactive(proactive_block)
        return

    # ── Gate: must have cwd + session_id to inject session context ─────────────
    if not cwd or not session_id:
        if conn is not None:
            conn.close()
        _emit_with_proactive(proactive_block)
        return

    # ── Gate: DB must exist for context injection ───────────────────────────────
    if not db_available:
        _emit_with_proactive(proactive_block)
        return

    # ── Gate: DB connection must be open for context injection ──────────────────
    if conn is None:
        # Connection failed earlier; proactive alert already captures this fault.
        _emit_with_proactive(proactive_block)
        return

    # ── Context injection ───────────────────────────────────────────────────────
    try:
        project_key = get_project_key(cwd)
        max_sessions = settings.get("max_context_sessions", 2)
        sessions = select_sessions(
            conn,
            project_key,
            session_id,
            max_sessions,
            source=source,
            db_path=db_path,
            cwd=cwd,
        )

        if not sessions:
            _emit_with_proactive(proactive_block)
            return

        context = build_context(sessions)
        if not context:
            _emit_with_proactive(proactive_block)
            return

        logger.info("Injecting context from %s session(s) for project %s", len(sessions), project_key)

        # Top-of-context directive: placed first because the hook's inline
        # preview may be truncated by the harness, and because earlier tokens
        # receive more attention. Tells Claude how to read the rest of this
        # injection and when to reach for the persisted file or recall skill.
        directive = (
            "## How To Use This Context\n"
            "- Sessions below are ordered most-recent first, and within each session "
            "the most recent exchanges come first. Read top-down to get the freshest "
            "context before older context.\n"
            "- If this hook's output was truncated inline and a persisted file path "
            "is referenced, Read that file before answering any message that references "
            "prior work — the last exchanges of the previous session may live only there.\n"
            "- For anything beyond the sessions shown here, use the "
            "`recall-conversations` skill rather than guessing."
        )

        # Assemble: directive + proactive (if any) + origin + pending + context.
        # The directive is first (it tells Claude how to read the rest); the
        # proactive block immediately follows it, ahead of origin / pending /
        # prior-session content (highest-attention position for the alert).
        origin = build_origin_block(source, sessions)
        pending = _pending_question_block(sessions, cwd)
        if proactive_block:
            full_context = f"{directive}\n\n{proactive_block}\n\n{origin}\n\n{pending}{context}"
        else:
            full_context = f"{directive}\n\n{origin}\n\n{pending}{context}"

        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": full_context,
            }
        }
        print(json.dumps(output))

    except Exception as e:
        logger.error("Context injection error: %s", e)
        # Don't block session start on errors; proactive alert (if any) still surfaces.
        _emit_with_proactive(proactive_block)
        sys.exit(0)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
