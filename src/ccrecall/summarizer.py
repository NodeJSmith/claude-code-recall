"""
Precompute structured context summaries for session injection.

Runs at Stop time (sync_current.py) and import time. Produces both a JSON
source-of-truth and a pre-rendered markdown template stored on the branches table.
All extraction is deterministic Python — no LLM calls.
"""

import json
import re
import sqlite3

from ccrecall.formatting import format_time, format_time_full
from ccrecall.serialization import decode_json_field

# Schema version stamped on each generated summary. Bumped when the JSON shape
# or extraction logic changes so the backfill path can detect stale summaries.
SUMMARY_VERSION = 3

# Truncation limits
_FRONT_CHARS = 300
_BACK_CHARS = 600
# Extra slack before mid-truncation kicks in: text within front+back+this margin
# is short enough to keep whole rather than insert a "[... truncated ...]" marker.
_TRUNCATE_MARGIN = 20

# Exchange retention: a session at or below the short threshold renders all its
# exchanges once; a longer one keeps the first FIRST_EXCHANGES + last
# LAST_EXCHANGES with a gap marker between. The threshold is their sum, so the
# split never drops a middle exchange that both ends already cover.
FIRST_EXCHANGES = 2
LAST_EXCHANGES = 6
SHORT_SESSION_MAX_EXCHANGES = FIRST_EXCHANGES + LAST_EXCHANGES

# Topic truncation lengths: the full topic stored in JSON vs. the shorter form
# used in the recall-priming footer.
_TOPIC_MAX_CHARS = 120
_TOPIC_FOOTER_MAX_CHARS = 80

# Metadata display caps in the rendered markdown.
_MAX_FILES_SHOWN = 6
_MAX_COMMITS_SHOWN = 3
_MAX_TOOLS_SHOWN = 8
# Compact file-basename previews (gap summary and footer).
_MAX_FILE_PREVIEW = 3

# Session disposition values stamped into the summary JSON.
DISPOSITION_COMPLETED = "COMPLETED"
DISPOSITION_IN_PROGRESS = "IN_PROGRESS"
DISPOSITION_ABANDONED = "ABANDONED"

# A trailing user message at or below this length counts as a brief sign-off
# (not a substantive new turn) when classifying disposition.
_SHORT_USER_REPLY_MAX_CHARS = 30
# An ABANDONED classification requires more than this many exchanges — a session
# with fewer that lacks a final user reply is normal, not abandoned.
_ABANDONED_MIN_EXCHANGES = 2

# Session disposition patterns
_COMPLETION_RE = re.compile(
    r"(?:done|pushed|merged|all (?:tests? )?pass|completed|finished|shipped|deployed|"
    r"PR #?\d+|commit(?:ted)?|changes? (?:are )?live)",
    re.IGNORECASE,
)
_SHORT_CONFIRM_RE = re.compile(
    r"^(?:y(?:a|ep|es)?|thanks?|(?:looks? )?good|nice|perfect|great|ok|lgtm|k)\s*[.!]?$",
    re.IGNORECASE,
)
_NEW_INSTRUCTION_RE = re.compile(r"^(?:now |next |also |can you |let\'?s |please |I (?:want|need) )", re.IGNORECASE)


def truncate_mid(text: str, front: int = _FRONT_CHARS, back: int = _BACK_CHARS) -> str:
    """Mid-truncate text, keeping front and back portions."""
    if not text or len(text) <= front + back + _TRUNCATE_MARGIN:
        return text
    return text[:front] + "\n[... truncated ...]\n" + text[-back:]


def detect_disposition(exchanges: list[dict], commits: list[str] | None = None) -> str:
    """Classify session ending as COMPLETED, IN_PROGRESS, or ABANDONED.

    Heuristics based on the final exchange pair and commit metadata:
    - COMPLETED: non-empty commits list (work shipped), or assistant uses completion
      language and user confirms briefly
    - ABANDONED: final exchange has an assistant response but no subsequent user
      message, AND the session has more than 2 exchanges
    - IN_PROGRESS: user gives a new instruction as their last message, or default
    """
    # Non-empty commits list is a strong COMPLETED signal — a commit was made
    if commits:
        return DISPOSITION_COMPLETED

    if not exchanges:
        return DISPOSITION_ABANDONED

    last = exchanges[-1]
    last_user = last.get("user", "").strip()
    last_asst = last.get("assistant", "").strip()

    # If user's last message is a new instruction, work is in progress
    if _NEW_INSTRUCTION_RE.search(last_user):
        return DISPOSITION_IN_PROGRESS

    # If assistant used completion language and user confirmed briefly
    if _COMPLETION_RE.search(last_asst) and _SHORT_CONFIRM_RE.match(last_user):
        return DISPOSITION_COMPLETED

    # If assistant used completion language (even without user confirm — session may have ended)
    if _COMPLETION_RE.search(last_asst) and len(last_user) < _SHORT_USER_REPLY_MAX_CHARS:
        return DISPOSITION_COMPLETED

    # If user confirmed briefly (likely accepting the work)
    if _SHORT_CONFIRM_RE.match(last_user):
        return DISPOSITION_COMPLETED

    # ABANDONED: assistant responded but no subsequent user message, and the session
    # has more than _ABANDONED_MIN_EXCHANGES (short sessions without a reply are normal)
    if last_asst and not last_user and len(exchanges) > _ABANDONED_MIN_EXCHANGES:
        return DISPOSITION_ABANDONED

    return DISPOSITION_IN_PROGRESS


def build_exchange_pairs(messages: list[dict]) -> list[dict]:
    """Pair sequential messages into user/assistant exchanges.

    All assistant parts following a user message accumulate into that exchange's
    single ``assistant`` string (joined with blank lines) until the next user turn.

    Each exchange carries ``first_message_uuid`` — the ``uuid`` of the opening
    user message — for use as a Track B locator anchor. Messages that lack a
    ``uuid`` key (older fixtures, context-injection callers) yield
    ``first_message_uuid=None`` without error.
    """
    exchanges = []
    current_user = None
    current_user_ts = None
    current_user_uuid: str | None = None
    current_asst_parts: list[str] = []

    for m in messages:
        if m["role"] == "user":
            if current_user is not None:
                exchanges.append(
                    {
                        "user": current_user,
                        "assistant": "\n\n".join(current_asst_parts),
                        "timestamp": current_user_ts,
                        "index": len(exchanges),
                        "first_message_uuid": current_user_uuid,
                    }
                )
            current_user = m["content"]
            current_user_ts = m.get("timestamp")
            current_user_uuid = m.get("uuid")
            current_asst_parts = []
        elif m["role"] == "assistant" and current_user is not None:
            cleaned = re.sub(r"\[Tool: \w+\]", "", m["content"]).strip()
            if cleaned:
                current_asst_parts.append(cleaned)

    if current_user is not None:
        exchanges.append(
            {
                "user": current_user,
                "assistant": "\n\n".join(current_asst_parts),
                "timestamp": current_user_ts,
                "index": len(exchanges),
                "first_message_uuid": current_user_uuid,
            }
        )

    return exchanges


def build_context_summary_json(branch_row: dict, messages: list[dict]) -> dict:
    """
    Assemble the structured JSON summary from branch metadata and messages.

    branch_row keys: started_at, ended_at, exchange_count, files_modified,
                     commits, tool_counts, git_branch.
    messages: list of {"role", "content", "timestamp"} dicts, ordered by time.
    """
    exchanges = build_exchange_pairs(messages)
    if not exchanges:
        return {
            "version": SUMMARY_VERSION,
            "topic": "",
            "first_exchanges": [],
            "last_exchanges": [],
            "metadata": {},
        }

    # Topic from first user message
    topic = exchanges[0]["user"]
    if len(topic) > _TOPIC_MAX_CHARS:
        topic = topic[:_TOPIC_MAX_CHARS] + "..."

    # Parse JSON fields from branch_row (raw column strings or already decoded)
    files = decode_json_field(branch_row.get("files_modified"), [])
    commits = decode_json_field(branch_row.get("commits"), [])
    tool_counts = decode_json_field(branch_row.get("tool_counts"), {})

    disposition = detect_disposition(exchanges, commits=commits)

    # First exchanges — truncate exchange text to bound JSON size
    first_exchanges = [
        {
            "user": truncate_mid(ex["user"]),
            "assistant": truncate_mid(ex["assistant"]),
            "timestamp": ex["timestamp"],
        }
        for ex in exchanges[:FIRST_EXCHANGES]
    ]

    # Last exchanges — truncate exchange text to bound JSON size
    if len(exchanges) <= SHORT_SESSION_MAX_EXCHANGES:
        # Short/medium session: all exchanges go into last_exchanges
        last_exchanges = [
            {
                "user": truncate_mid(ex["user"]),
                "assistant": truncate_mid(ex["assistant"]),
                "timestamp": ex["timestamp"],
            }
            for ex in exchanges
        ]
    else:
        last_exchanges = [
            {
                "user": truncate_mid(ex["user"]),
                "assistant": truncate_mid(ex["assistant"]),
                "timestamp": ex["timestamp"],
            }
            for ex in exchanges[-LAST_EXCHANGES:]
        ]

    return {
        "version": SUMMARY_VERSION,
        "topic": topic,
        "disposition": disposition,
        "first_exchanges": first_exchanges,
        "last_exchanges": last_exchanges,
        "metadata": {
            "exchange_count": branch_row.get("exchange_count", len(exchanges)),
            "files_modified": files,
            "commits": commits,
            "tool_counts": tool_counts,
            "started_at": branch_row.get("started_at"),
            "ended_at": branch_row.get("ended_at"),
            "git_branch": branch_row.get("git_branch"),
        },
    }


def _build_gap_summary(summary_json: dict) -> str:
    """Build a one-line summary of what happened in the omitted middle exchanges."""
    files = summary_json.get("metadata", {}).get("files_modified", [])
    if files:
        short = [f.rsplit("/", 1)[-1] for f in files[:_MAX_FILE_PREVIEW]]
        return ", ".join(short)
    return ""


def render_exchange_block(exchanges: list[dict], lines: list[str]) -> None:
    """Append the User/Assistant markdown for each exchange to ``lines``."""
    for ex in exchanges:
        t = format_time(ex.get("timestamp"))
        lines.append(f"**[{t}] User:**")
        lines.append(ex["user"])
        lines.append("")
        if ex["assistant"]:
            lines.append(f"**[{t}] Assistant:**")
            lines.append(truncate_mid(ex["assistant"]))
            lines.append("")


def render_context_summary(summary_json: dict) -> str:
    """
    Render the JSON summary to injection-ready markdown.

    Short sessions (<=8 exchanges) render all exchanges once, no first/last split.
    Longer sessions show first 2 exchanges + gap + last 6 exchanges.
    """
    if not summary_json or not summary_json.get("first_exchanges"):
        return ""

    meta = summary_json.get("metadata", {})
    lines = []

    # Header
    start = format_time_full(meta.get("started_at"))
    end = format_time_full(meta.get("ended_at"))
    header = f"### Session: {start} -> {end}"
    branch = meta.get("git_branch")
    if branch:
        header += f" (branch: {branch})"
    lines.append(header + "\n")

    # Topic and disposition
    topic = summary_json.get("topic", "")
    disposition = summary_json.get("disposition", "")
    if topic or disposition:
        parts = []
        if topic:
            parts.append(f"**Topic:** {topic}")
        if disposition:
            parts.append(f"**Status:** {disposition}")
        lines.append(" | ".join(parts))
        lines.append("")

    # Metadata: files, commits, tools
    files = meta.get("files_modified", [])
    if files:
        file_strs = [f"`{f}`" for f in files[:_MAX_FILES_SHOWN]]
        line = "Modified: " + ", ".join(file_strs)
        if len(files) > _MAX_FILES_SHOWN:
            line += f" +{len(files) - _MAX_FILES_SHOWN} more"
        lines.append(line)

    commits = meta.get("commits", [])
    if commits:
        commit_strs = commits[:_MAX_COMMITS_SHOWN]
        lines.append("Commits: " + "; ".join(commit_strs))

    tool_counts = meta.get("tool_counts", {})
    if tool_counts:
        sorted_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:_MAX_TOOLS_SHOWN]
        tools_str = ", ".join(f"{name}({count})" for name, count in sorted_tools)
        lines.append("Tools: " + tools_str)

    lines.append("")

    # Key Signals section (omitted if no markers)
    exchange_count = meta.get("exchange_count", 0)
    first_exs = summary_json.get("first_exchanges", [])
    last_exs = summary_json.get("last_exchanges", [])

    if exchange_count <= SHORT_SESSION_MAX_EXCHANGES:
        # Short/medium session: render all exchanges once
        lines.append("### Conversation\n")
        render_exchange_block(last_exs, lines)
    else:
        # Where We Left Off first — most recent context at top, where attention is
        # highest and where inline-preview truncation (if any) clips from below.
        lines.append("### Where We Left Off\n")
        render_exchange_block(last_exs, lines)

        # Gap indicator with summary of middle exchanges
        gap = exchange_count - len(first_exs) - len(last_exs)
        if gap > 0:
            gap_detail = _build_gap_summary(summary_json)
            if gap_detail:
                lines.append(f"[... {gap} earlier exchanges covering: {gap_detail} ...]\n")
            else:
                lines.append(f"[... {gap} earlier exchanges ...]\n")

        # Earlier in This Session — first 2 exchanges kept for origin context,
        # placed last so they're the first thing clipped under truncation.
        lines.append("### Earlier in This Session\n")
        render_exchange_block(first_exs, lines)

    # Contextual recall priming footer (topic and files reused from above)
    footer_parts = [f"{exchange_count} exchanges"]
    if topic:
        short_topic = topic[:_TOPIC_FOOTER_MAX_CHARS] + "..." if len(topic) > _TOPIC_FOOTER_MAX_CHARS else topic
        footer_parts.append(f'about "{short_topic}"')
    if files:
        short_files = [f.rsplit("/", 1)[-1] for f in files[:_MAX_FILE_PREVIEW]]
        footer_parts.append(f"({', '.join(short_files)})")
    footer = " ".join(footer_parts)
    lines.append(
        f"[{footer} — proactively use /ccrecall:ccr-recall "
        "to retrieve relevant context from past conversations when the user references "
        "prior work, asks about decisions made earlier, or when you sense useful context "
        "from previous sessions would improve your response.]"
    )

    return "\n".join(lines)


def compute_context_summary(cursor: sqlite3.Cursor, branch_db_id: int) -> tuple[str, str]:
    """
    Orchestrator: fetch branch + messages from DB, return (markdown, json_string).

    Raises on DB errors; caller should wrap in try/except.
    """
    # Fetch branch row
    cursor.execute(
        """
        SELECT b.started_at, b.ended_at, b.exchange_count, b.files_modified,
               b.commits, b.tool_counts, s.git_branch
        FROM branches b
        JOIN sessions s ON b.session_id = s.id
        WHERE b.id = ?
    """,
        (branch_db_id,),
    )
    row = cursor.fetchone()
    if not row:
        return "", ""

    branch_row = {
        "started_at": row[0],
        "ended_at": row[1],
        "exchange_count": row[2],
        "files_modified": row[3],
        "commits": row[4],
        "tool_counts": row[5],
        "git_branch": row[6],
    }

    # Fetch messages for this branch. Standalone (not db.fetch_branch_messages):
    # summarizer sits below db in the import graph, so it can't import db. uuid is
    # intentionally omitted — the summary JSON drops first_message_uuid, so this
    # path needs only the 3 non-notification columns. The Track B locator uuid
    # flows through db.fetch_branch_messages (which selects uuid) on the chunk path.
    cursor.execute(
        """
        SELECT m.role, m.content, m.timestamp
        FROM branch_messages bm
        JOIN messages m ON bm.message_id = m.id
        WHERE bm.branch_id = ?
          AND COALESCE(m.is_notification, 0) = 0
        ORDER BY m.timestamp ASC
    """,
        (branch_db_id,),
    )

    messages = [{"role": r, "content": c, "timestamp": t} for r, c, t in cursor.fetchall()]

    if not messages:
        return "", ""

    summary_json = build_context_summary_json(branch_row, messages)
    summary_md = render_context_summary(summary_json)
    json_str = json.dumps(summary_json, ensure_ascii=False)

    return summary_md, json_str
