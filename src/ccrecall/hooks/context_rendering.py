"""Context rendering for the SessionStart context hook.

Turns selected sessions (see session_selection.py) into the markdown blocks
injected into the new session: the Session Origin block, the pending-question
warning, and the per-session context body (cached summary or fallback render).
"""

import logging

from ccrecall.formatting import format_time_full
from ccrecall.models import LOGGER_NAME
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

# Uncached topic fallback truncates the first user message to this many chars.
TOPIC_PREVIEW_MAX_CHARS = 120


def pending_question_block(sessions: list[dict], cwd: str) -> str:
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
