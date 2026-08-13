"""Deterministic, DB-only eligibility policy for Session Recaps."""

import json
import re
import sqlite3
from dataclasses import dataclass

from whenever import Instant

from ccrecall.recap_contract import ELIGIBILITY_POLICY_VERSION
from ccrecall.summarizer import SUMMARY_VERSION

MIN_EXCHANGES = 2
SELECTED_PROSE_CHARS = 600
EVIDENCE_PROSE_CHARS = 240
EVIDENCE_TOOL_ACTIONS = 3
EVIDENCE_ELAPSED_SECONDS = 120

MISSING_ACTIVE_BRANCH = "missing_active_branch"
MISSING_CURRENT_SUMMARY = "missing_current_summary"
NO_ELIGIBLE_MESSAGES = "no_eligible_messages"
BELOW_MIN_EXCHANGES = "below_min_exchanges"
ELIGIBLE_SUBSTANTIVE_PROSE = "eligible_substantive_prose"
ELIGIBLE_WORK_EVIDENCE = "eligible_work_evidence"
BELOW_MEANINGFUL_THRESHOLD = "below_meaningful_threshold"

_TOOL_MARKER_RE = re.compile(r"^\[([^:\]]+)(?::.*)?\]$")
_HARNESS_PREFIXES = (
    "[request interrupted",
    "first, silently read",
    "re-read ./claude.md",
    "re-read claude.md",
    "<system-reminder>",
    "<local-command-",
    "base directory for this skill:",
    "<command-message>",
)


@dataclass(frozen=True)
class EligibilityMeasures:
    """Audited normalized measures used to explain one policy decision."""

    exchange_count: int
    substantive_prose_chars: int
    nonrepetitive_tool_actions: int
    file_count: int
    commit_count: int
    elapsed_seconds: float


@dataclass(frozen=True)
class EligibilityInput:
    """One branch's DB-only state, copied before policy evaluation."""

    branch_id: int | None
    active: bool
    summary_current: bool
    started_at: str | None
    ended_at: str | None
    files_modified: object
    commits: object
    messages: tuple[dict, ...]


@dataclass(frozen=True)
class EligibilityDecision:
    """Versioned eligibility result shared by selection and status callers."""

    eligible: bool
    reason: str
    measures: EligibilityMeasures
    policy_version: int = ELIGIBILITY_POLICY_VERSION


def _decoded_list(value: object) -> list:
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _nonempty_prose(content: object) -> str:
    if not isinstance(content, str):
        return ""
    text = content.strip()
    if text.lower().startswith(_HARNESS_PREFIXES):
        return ""
    return text


def _elapsed_seconds(started_at: str | None, ended_at: str | None) -> float:
    if not started_at or not ended_at:
        return 0
    try:
        return max(0, (Instant.parse_iso(ended_at) - Instant.parse_iso(started_at)).total("seconds"))
    except ValueError:
        return 0


def measure_eligibility(input: EligibilityInput) -> EligibilityMeasures:
    """Calculate the frozen audit measures from normalized imported state."""
    exchange_count = 0
    prose_chars = 0
    tool_actions = 0
    last_tool: str | None = None

    for message in input.messages:
        role = message.get("role")
        prose = _nonempty_prose(message.get("content"))
        prose_chars += len(prose)
        if role == "user":
            exchange_count += 1
            last_tool = None
            continue
        if role != "assistant":
            last_tool = None
            continue
        if prose:
            last_tool = None
        tool_content = message.get("tool_content")
        if not isinstance(tool_content, str):
            continue
        for line in tool_content.splitlines():
            match = _TOOL_MARKER_RE.match(line.strip())
            if match is None:
                last_tool = None
                continue
            tool_name = match.group(1)
            if tool_name != last_tool:
                tool_actions += 1
            last_tool = tool_name

    files = {entry for entry in _decoded_list(input.files_modified) if isinstance(entry, str) and entry}
    commits = [entry for entry in _decoded_list(input.commits) if entry]
    return EligibilityMeasures(
        exchange_count=exchange_count,
        substantive_prose_chars=prose_chars,
        nonrepetitive_tool_actions=tool_actions,
        file_count=len(files),
        commit_count=len(commits),
        elapsed_seconds=_elapsed_seconds(input.started_at, input.ended_at),
    )


def evaluate_eligibility(input: EligibilityInput) -> EligibilityDecision:
    """Apply policy v1 without inferring completion, quality, or satisfaction."""
    measures = measure_eligibility(input)
    if not input.active:
        return EligibilityDecision(False, MISSING_ACTIVE_BRANCH, measures)
    if not input.summary_current:
        return EligibilityDecision(False, MISSING_CURRENT_SUMMARY, measures)
    if not input.messages:
        return EligibilityDecision(False, NO_ELIGIBLE_MESSAGES, measures)
    if measures.exchange_count < MIN_EXCHANGES:
        return EligibilityDecision(False, BELOW_MIN_EXCHANGES, measures)
    if measures.substantive_prose_chars >= SELECTED_PROSE_CHARS:
        return EligibilityDecision(True, ELIGIBLE_SUBSTANTIVE_PROSE, measures)
    has_work_evidence = (
        measures.nonrepetitive_tool_actions >= EVIDENCE_TOOL_ACTIONS
        or measures.file_count >= 1
        or measures.commit_count >= 1
    )
    if (
        measures.substantive_prose_chars >= EVIDENCE_PROSE_CHARS
        and measures.elapsed_seconds >= EVIDENCE_ELAPSED_SECONDS
        and has_work_evidence
    ):
        return EligibilityDecision(True, ELIGIBLE_WORK_EVIDENCE, measures)
    return EligibilityDecision(False, BELOW_MEANINGFUL_THRESHOLD, measures)


def load_eligibility_input(cursor: sqlite3.Cursor, branch_id: int) -> EligibilityInput:
    """Load one branch's normalized policy input without reading transcript files."""
    branch = cursor.execute(
        """
        SELECT id, is_active, summary_version, context_summary, context_summary_json,
               started_at, ended_at, files_modified, commits
        FROM branches WHERE id = ?
        """,
        (branch_id,),
    ).fetchone()
    if branch is None:
        return EligibilityInput(None, False, False, None, None, None, None, ())
    messages = cursor.execute(
        """
        SELECT m.role, m.content, m.tool_content
        FROM branch_messages bm
        JOIN messages m ON m.id = bm.message_id
        WHERE bm.branch_id = ?
          AND m.session_id = (SELECT session_id FROM branches WHERE id = ?)
          AND COALESCE(m.is_notification, 0) = 0
        ORDER BY bm.position ASC
        """,
        (branch_id, branch_id),
    ).fetchall()
    return _eligibility_input_from_rows(branch, messages)


def _eligibility_input_from_rows(branch: tuple, messages: list[tuple]) -> EligibilityInput:
    """Build policy input from one branch row and its ordered normalized messages."""
    summary_current = (
        branch[2] == SUMMARY_VERSION
        and isinstance(branch[3], str)
        and bool(branch[3].strip())
        and isinstance(branch[4], str)
        and bool(branch[4].strip())
    )
    return EligibilityInput(
        branch_id=branch[0],
        active=branch[1] == 1,
        summary_current=summary_current,
        started_at=branch[5],
        ended_at=branch[6],
        files_modified=branch[7],
        commits=branch[8],
        messages=tuple(
            {"role": role, "content": content, "tool_content": tool_content} for role, content, tool_content in messages
        ),
    )


def evaluate_branch(cursor: sqlite3.Cursor, branch_id: int) -> EligibilityDecision:
    """Load and evaluate one branch through the common DB/policy boundary."""
    return evaluate_eligibility(load_eligibility_input(cursor, branch_id))


def iter_evaluate_branches(
    cursor: sqlite3.Cursor,
    *,
    active_only: bool = False,
    batch_size: int = 100,
):
    """Yield shared policy decisions in bounded keyset batches."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    last_branch_id = 0
    active_clause = "AND is_active = 1" if active_only else ""
    while True:
        branches = cursor.execute(
            "SELECT id, is_active, summary_version, context_summary, context_summary_json, "
            "started_at, ended_at, files_modified, commits FROM branches "
            f"WHERE id > ? {active_clause} ORDER BY id LIMIT ?",
            (last_branch_id, batch_size),
        ).fetchall()
        if not branches:
            return
        branch_ids = [branch[0] for branch in branches]
        placeholders = ", ".join("?" for _ in branch_ids)
        messages_by_branch = {branch_id: [] for branch_id in branch_ids}
        for branch_id, role, content, tool_content in cursor.execute(
            "SELECT bm.branch_id, m.role, m.content, m.tool_content FROM branch_messages bm "
            "JOIN messages m ON m.id = bm.message_id JOIN branches b ON b.id = bm.branch_id "
            f"WHERE bm.branch_id IN ({placeholders}) AND m.session_id = b.session_id "
            "AND COALESCE(m.is_notification, 0) = 0 ORDER BY bm.branch_id, bm.position",
            branch_ids,
        ):
            messages_by_branch[branch_id].append((role, content, tool_content))
        for branch in branches:
            branch_id = branch[0]
            yield branch_id, evaluate_eligibility(_eligibility_input_from_rows(branch, messages_by_branch[branch_id]))
        last_branch_id = branch_ids[-1]


def evaluate_branches(cursor: sqlite3.Cursor, branch_ids: list[int] | None = None) -> dict[int, EligibilityDecision]:
    """Evaluate selected branches, or all branches when no selection is supplied."""
    if branch_ids is None:
        return dict(iter_evaluate_branches(cursor))
    return {branch_id: evaluate_branch(cursor, branch_id) for branch_id in branch_ids}
