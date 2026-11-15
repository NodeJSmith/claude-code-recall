"""Session recaps: which recap a session shows, which sessions need a new one, and generating it.

A session can have two recaps. Claude Code writes its own (``away_summary``,
stored on ``branches`` by sync and import) when the user steps away; ccrecall
generates one with ``ccrecall backfill recaps`` and stores it in ``recaps``,
whose only writer is this module. ``pick_recap`` is the one rule for which of
the two a session shows and whether it's current; both the read surface
(``show-recaps``) and backfill selection call it.

The branch's *last message* is the last row the generation input includes: a
user or assistant row that isn't a notification and has non-empty content
(``INPUT_ROW_FILTER``). A recap is current when it covers that message.

The read side (``read_recaps``, ``format_line``) lives here too, so the pick
rule and the last-message definition have one home.
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import NamedTuple

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    ResultError,
    ResultMessage,
    TextBlock,
    query,
)
from whenever import Instant, TimeDelta

from ccrecall import run_history
from ccrecall.config import RECAP_CALL_DIR
from ccrecall.dates import parse_instant
from ccrecall.db import escape_like
from ccrecall.db_base import commit_single_write
from ccrecall.models import LOGGER_NAME
from ccrecall.project_ops import is_excluded_project

log = logging.getLogger(LOGGER_NAME)

SOURCE_CLAUDE_CODE = "claude_code"
SOURCE_GENERATED = "generated"

DEFAULT_DAYS = 7
DEFAULT_IDLE_MINUTES = 30
# Calls per run. Bounds a run (and the shared Claude quota it spends) without a time budget;
# newest-first selection lets successive scheduled runs work through a backlog.
DEFAULT_LIMIT = 20
MIN_USER_TURNS = 3
COOLDOWN = TimeDelta(hours=24)
MAX_CONSECUTIVE_FAILURES = 3
# branches.ended_at prefilters selection by string comparison; this slack keeps a
# format difference at the window's edge (".123Z" sorts before "Z") from cutting a session.
PREFILTER_SLACK = TimeDelta(hours=24)

INPUT_CAP_CHARS = 400_000
OUTPUT_CAP_CHARS = 400
CALL_TIMEOUT_SECONDS = 300
OMITTED_MARKER = "[... middle of the session omitted ...]"

RECAP_MODEL = "claude-sonnet-5-5"
SYSTEM_PROMPT = (
    "You are the assistant from the Claude Code session in the transcript below, "
    "talking to the same user. Answer only with the recap text."
)
# Claude Code's own recap prompt, verbatim.
RECAP_PROMPT = (
    "The user stepped away and is coming back. Recap in under 40 words, 1-2 plain sentences, "
    "no markdown. Lead with the overall goal and current task, then the one next action. "
    "Skip root-cause narrative, fix internals, secondary to-dos, and em-dash tangents."
)

# Rows the generation input includes; the newest one is the branch's last message.
INPUT_ROW_FILTER = "COALESCE(m.is_notification, 0) = 0 AND TRIM(m.content) != ''"

# HTTP statuses and SDK assistant errors that fail every call, not just this session's.
# A 402 (payment required) is an account problem, like a lapsed login. Any 5xx is an
# outage (see classify_failure), which a session's own retry can't get past either.
RUN_LEVEL_STATUSES = {
    401: run_history.REASON_CLAUDE_AUTH,
    402: run_history.REASON_CLAUDE_AUTH,
    403: run_history.REASON_CLAUDE_AUTH,
    429: run_history.REASON_RATE_LIMITED,
}
RUN_LEVEL_ASSISTANT_ERRORS = {
    "authentication_failed": run_history.REASON_CLAUDE_AUTH,
    "billing_error": run_history.REASON_CLAUDE_AUTH,
    "rate_limit": run_history.REASON_RATE_LIMITED,
    "server_error": run_history.REASON_API_UNAVAILABLE,
}


@dataclass(frozen=True)
class Recap:
    text: str
    source: str
    as_of: str
    current: bool


@dataclass(frozen=True)
class RecapInput:
    prompt: str
    as_of: str


@dataclass
class CallState:
    """What one call's stream has carried so far; kept when the stream raises partway."""

    text: str = ""
    assistant_error: str | None = None
    result_error: str | None = None
    api_status: int | None = None


@dataclass(frozen=True)
class Candidate:
    session_id: int
    session_uuid: str
    branch_id: int
    last_message_at: Instant


class RecapCallError(Exception):
    """A generation call that produced no recap.

    ``run_reason`` is set for a failure that would hit every session (not logged
    in, rate limited, Claude Code not starting): the run stops and nothing
    cools down. It is None for a failure specific to this session.
    """

    def __init__(self, message: str, run_reason: str | None = None) -> None:
        super().__init__(message)
        self.run_reason = run_reason


def pick_recap(
    *,
    harness_text: str | None,
    harness_at: str | None,
    generated_text: str | None,
    generated_as_of: str | None,
    last_message_at: str | None,
) -> Recap | None:
    """The recap a session shows: the one covering the newer point, Claude Code's on a tie.

    A recap's ``as_of`` is the time of the last message it covers; Claude Code's
    is its own timestamp. It is current when that is no earlier than the
    branch's last message, so a branch whose last message is missing or
    unreadable never has a current recap.
    """
    harness = recap_option(SOURCE_CLAUDE_CODE, harness_text, harness_at)
    generated = recap_option(SOURCE_GENERATED, generated_text, generated_as_of)
    # Claude Code's wins unless the generated one covers a strictly later point.
    newer_generated = generated is not None and (harness is None or generated.instant > harness.instant)
    winner = generated if newer_generated else harness
    if winner is None:
        return None
    last = parse_instant(last_message_at)
    current = last is not None and winner.instant >= last
    return Recap(text=winner.text, source=winner.source, as_of=winner.as_of, current=current)


class RecapOption(NamedTuple):
    source: str
    text: str
    as_of: str
    instant: Instant


def recap_option(source: str, text: str | None, as_of: str | None) -> RecapOption | None:
    """A stored recap the pick rule can weigh, or None when its text or time is missing."""
    instant = parse_instant(as_of)
    if not text or as_of is None or instant is None:
        return None
    return RecapOption(source, text, as_of, instant)


def select_candidates(
    conn: sqlite3.Connection,
    *,
    now: Instant,
    days: int,
    idle_minutes: int,
    limit: int | None,
    exclude_projects: list[str],
) -> list[Candidate]:
    """Sessions that need a generated recap, newest last message first.

    A session qualifies when it has at least ``MIN_USER_TURNS`` user turns, its
    last message is within ``days``, it has no timestamped entry of any kind (tool
    calls and subagent reports included) in the last ``idle_minutes``, its project
    isn't excluded, the recap it would show isn't current, and its last attempt
    didn't fail within ``COOLDOWN``.
    """
    window_start = now - TimeDelta(hours=24 * days)
    idle_cutoff = now - TimeDelta(minutes=idle_minutes)
    # ended_at counts every entry, so it is never earlier than the last message.
    prefilter = (window_start - PREFILTER_SLACK).format_iso()
    rows = conn.execute(
        f"""
        SELECT s.id, s.uuid, b.id, b.ended_at, b.away_summary, b.away_summary_at, p.name, p.key,
               r.text, r.as_of, r.error, r.attempted_at, agg.last_at, agg.user_turns
        FROM branches b
        JOIN sessions s ON s.id = b.session_id
        LEFT JOIN projects p ON p.id = s.project_id
        LEFT JOIN recaps r ON r.session_id = s.id
        JOIN (
            SELECT bm.branch_id,
                   MAX(m.timestamp) AS last_at,
                   SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END) AS user_turns
            FROM branch_messages bm
            JOIN messages m ON m.id = bm.message_id
            WHERE {INPUT_ROW_FILTER}
              AND bm.branch_id IN (SELECT id FROM branches WHERE is_active = 1 AND ended_at >= ?)
            GROUP BY bm.branch_id
        ) agg ON agg.branch_id = b.id
        WHERE b.is_active = 1
        """,
        (prefilter,),
    ).fetchall()

    candidates: list[Candidate] = []
    for (
        session_id,
        session_uuid,
        branch_id,
        ended_at,
        harness_text,
        harness_at,
        project_name,
        project_key,
        generated_text,
        generated_as_of,
        error,
        attempted_at,
        last_at,
        user_turns,
    ) in rows:
        last = parse_instant(last_at)
        if last is None or last < window_start or user_turns < MIN_USER_TURNS:
            continue
        # Idleness counts every entry: a session busy with tool calls or subagents writes
        # no prose row for a while, but hasn't stopped. The max covers a missing ended_at.
        active_until = max(last, parse_instant(ended_at) or last)
        if active_until > idle_cutoff:
            continue
        # Stored rows don't record whether import fell back to the lossy key, so check it too.
        if is_excluded_project(project_name, project_key, exclude_projects, lossy=True):
            continue
        attempted = parse_instant(attempted_at)
        if error is not None and attempted is not None and attempted > now - COOLDOWN:
            continue
        recap = pick_recap(
            harness_text=harness_text,
            harness_at=harness_at,
            generated_text=generated_text,
            generated_as_of=generated_as_of,
            last_message_at=last_at,
        )
        if recap is not None and recap.current:
            continue
        candidates.append(Candidate(session_id, session_uuid, branch_id, last))

    candidates.sort(key=lambda c: c.last_message_at, reverse=True)
    return candidates if limit is None else candidates[:limit]


def build_input(conn: sqlite3.Connection, branch_id: int) -> RecapInput | None:
    """The branch's prose as one prompt, and the time of the last message it covers.

    Both come from one read, so a message synced during the call is never
    counted as covered. None when the branch has no input rows.
    """
    rows = conn.execute(
        f"""
        SELECT m.role, m.content, m.timestamp
        FROM branch_messages bm
        JOIN messages m ON m.id = bm.message_id
        WHERE bm.branch_id = ? AND {INPUT_ROW_FILTER}
        ORDER BY m.timestamp ASC, m.id ASC
        """,
        (branch_id,),
    ).fetchall()
    if not rows:
        return None
    blocks = [(role, f"[{role}]\n{content.strip()}") for role, content, _timestamp in rows]
    body = "\n\n".join(cap_blocks(blocks, INPUT_CAP_CHARS))
    prompt = f"<transcript>\n{body}\n</transcript>\n\n{RECAP_PROMPT}"
    return RecapInput(prompt=prompt, as_of=rows[-1][2])


def cap_blocks(blocks: list[tuple[str, str]], cap: int) -> list[str]:
    """Rendered rows joined by blank lines, kept within ``cap`` characters.

    Over the cap, keeps the first user row, then the most recent rows that fit,
    with ``OMITTED_MARKER`` where the middle was dropped. The newest row is always
    kept, its end truncated to fit if it alone is too long, since the recap is
    recorded as covering it.
    """
    texts = [text for _role, text in blocks]
    if sum(len(text) for text in texts) + 2 * (len(texts) - 1) <= cap:
        return texts
    first_user = next((i for i, (role, _text) in enumerate(blocks) if role == "user"), 0)
    head = texts[first_user][: cap // 2]
    budget = cap - len(head) - len(OMITTED_MARKER) - 4
    tail: list[str] = []
    for text in reversed(texts[first_user + 1 :]):
        cost = len(text) + 2
        if cost > budget:
            if not tail and budget > 2:
                tail.append(text[-(budget - 2) :])
            break
        tail.append(text)
        budget -= cost
    tail.reverse()
    return [head, OMITTED_MARKER, *tail]


def call_options() -> ClaudeAgentOptions:
    """An isolated one-turn call: no tools, settings, MCP servers, or transcript left behind."""
    return ClaudeAgentOptions(
        model=RECAP_MODEL,
        system_prompt=SYSTEM_PROMPT,
        max_turns=1,
        tools=[],
        setting_sources=[],
        strict_mcp_config=True,
        cwd=str(RECAP_CALL_DIR),
        extra_args={"no-session-persistence": None},
    )


def classify_failure(exc: BaseException | None, assistant_error: str | None, api_status: int | None) -> str | None:
    """The run-level reason for a failed call, or None when it is this session's alone."""
    if isinstance(exc, CLINotFoundError | CLIConnectionError):
        return run_history.REASON_CLAUDE_UNAVAILABLE
    if assistant_error in RUN_LEVEL_ASSISTANT_ERRORS:
        return RUN_LEVEL_ASSISTANT_ERRORS[assistant_error]
    if api_status is not None:
        if api_status in RUN_LEVEL_STATUSES:
            return RUN_LEVEL_STATUSES[api_status]
        if api_status >= 500:
            return run_history.REASON_API_UNAVAILABLE
    return None


async def consume(stream: AsyncIterator[object], state: CallState) -> None:
    """Read a call's whole stream into ``state``: its text and any error it reported.

    Reads to the end rather than stopping at the result: abandoning the SDK's
    generator mid-stream leaves it to be finalized at loop shutdown, which raises.
    """
    async for message in stream:
        if isinstance(message, AssistantMessage):
            state.text += "".join(block.text for block in message.content if isinstance(block, TextBlock))
            state.assistant_error = message.error or state.assistant_error
        elif isinstance(message, ResultMessage) and message.is_error:
            state.result_error = message.result or message.subtype
            state.api_status = message.api_error_status


async def generate_async(prompt: str) -> str:
    """Make one call and return its recap text, or raise ``RecapCallError``."""
    result = CallState()
    try:
        await asyncio.wait_for(consume(query(prompt=prompt, options=call_options()), result), CALL_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise RecapCallError(f"timed out (limit {CALL_TIMEOUT_SECONDS}s)") from exc
    except Exception as exc:
        # The SDK raises ResultError after an error result, carrying its HTTP status.
        status = exc.api_error_status if isinstance(exc, ResultError) else None
        detail = (exc.result if isinstance(exc, ResultError) else None) or str(exc)
        reason = classify_failure(exc, result.assistant_error, status or result.api_status)
        raise RecapCallError(f"{type(exc).__name__}: {detail}", reason) from exc

    if result.result_error is not None or result.assistant_error is not None:
        raise RecapCallError(
            f"Claude returned an error: {result.result_error or result.assistant_error}",
            classify_failure(None, result.assistant_error, result.api_status),
        )
    text = result.text.strip()[:OUTPUT_CAP_CHARS].strip()
    if not text:
        raise RecapCallError("Claude returned an empty recap")
    return text


def generate(prompt: str) -> str:
    """Make one call synchronously, first creating the directory the call runs in."""
    RECAP_CALL_DIR.mkdir(parents=True, exist_ok=True)
    return asyncio.run(generate_async(prompt))


def write_success(conn: sqlite3.Connection, session_id: int, text: str, as_of: str, attempted_at: Instant) -> None:
    """Store a generated recap and clear any earlier error."""
    commit_single_write(
        conn,
        """
        INSERT INTO recaps (session_id, text, as_of, error, attempted_at) VALUES (?, ?, ?, NULL, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            text = excluded.text, as_of = excluded.as_of, error = NULL, attempted_at = excluded.attempted_at
        """,
        (session_id, text, as_of, attempted_at.format_iso()),
    )


def write_failure(conn: sqlite3.Connection, session_id: int, error: str, attempted_at: Instant) -> None:
    """Record a failed attempt, leaving any earlier recap in place."""
    commit_single_write(
        conn,
        """
        INSERT INTO recaps (session_id, error, attempted_at) VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET error = excluded.error, attempted_at = excluded.attempted_at
        """,
        (session_id, error, attempted_at.format_iso()),
    )


@dataclass(frozen=True)
class SessionRecap:
    """One row of ``show-recaps`` output."""

    session_id: str
    recap: Recap | None

    def to_json(self) -> dict:
        recap = self.recap
        return {
            "session_id": self.session_id,
            "text": recap.text if recap else None,
            "source": recap.source if recap else None,
            "current": recap.current if recap else False,
            "as_of": recap.as_of if recap else None,
        }


def read_recaps(conn: sqlite3.Connection, ids: list[str]) -> list[SessionRecap]:
    """Each id's best recap, in the order given; a prefix matching several sessions yields each.

    Reads only the database: it never imports or calls Claude. An id that matches
    no session yields a row with no recap.
    """
    results: list[SessionRecap] = []
    for session_id in ids:
        rows = conn.execute(
            f"""
            SELECT s.uuid, b.away_summary, b.away_summary_at, r.text, r.as_of,
                   (SELECT MAX(m.timestamp) FROM branch_messages bm JOIN messages m ON m.id = bm.message_id
                    WHERE bm.branch_id = b.id AND {INPUT_ROW_FILTER})
            FROM sessions s
            LEFT JOIN branches b ON b.session_id = s.id AND b.is_active = 1
            LEFT JOIN recaps r ON r.session_id = s.id
            WHERE s.uuid LIKE ? ESCAPE '\\'
            ORDER BY s.uuid
            """,
            (escape_like(session_id) + "%",),
        ).fetchall()
        if not rows:
            results.append(SessionRecap(session_id, None))
            continue
        for session_uuid, harness_text, harness_at, generated_text, generated_as_of, last_at in rows:
            recap = pick_recap(
                harness_text=harness_text,
                harness_at=harness_at,
                generated_text=generated_text,
                generated_as_of=generated_as_of,
                last_message_at=last_at,
            )
            results.append(SessionRecap(session_uuid, recap))
    return results


def format_line(row: SessionRecap) -> str:
    """One line of ``show-recaps`` markdown output."""
    recap = row.recap
    if recap is None:
        return f"{row.session_id[:8]}  (no recap)"
    state = "current" if recap.current else "out of date"
    return f"{row.session_id[:8]}  {recap.text}  ({recap.source}, {state})"
