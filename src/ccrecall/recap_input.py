"""Canonical, DB-only input projection for Session Recaps."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass

RECAP_CONTRACT_VERSION = 2
RECAP_INPUT_CONTRACT_VERSION = 1
# Frozen by design/specs/010-session-recaps/eligibility-audit.md.
ELIGIBILITY_POLICY_VERSION = 1
CONTENT_DEPENDENT_BLOCK_REASONS = ("budget_exceeded", "unusable_output")


@dataclass(frozen=True)
class RecapInput:
    """The packet object and bytes captured from one SQLite snapshot."""

    projection: dict
    packet: bytes
    input_hash: str


def _decoded_json(value: str | None):
    """Return imported JSON as a canonical value; absent or malformed is null."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def canonical_json(value: dict) -> bytes:
    """Serialize the exact packet/hash representation."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_recap_input(cursor: sqlite3.Cursor, branch_id: int) -> RecapInput:
    """Copy one active branch's normalized recap packet from SQLite only."""
    branch = cursor.execute(
        """
        SELECT b.id, b.leaf_uuid, b.started_at, b.ended_at, b.exchange_count,
               b.commits, b.tool_counts, b.context_summary,
                 b.context_summary_json, s.uuid, s.git_branch,
               p.key, p.name
        FROM branches b
        JOIN sessions s ON s.id = b.session_id
        LEFT JOIN projects p ON p.id = s.project_id
        WHERE b.id = ? AND b.is_active = 1
        """,
        (branch_id,),
    ).fetchone()
    if branch is None:
        raise ValueError(f"no active branch {branch_id}")

    messages = cursor.execute(
        """
        SELECT bm.position, m.role, m.timestamp, m.origin, m.uuid, m.parent_uuid,
               m.content, m.tool_content
        FROM branch_messages bm
        JOIN messages m ON m.id = bm.message_id
        JOIN branches b ON b.id = bm.branch_id
        WHERE bm.branch_id = ?
          AND m.session_id = b.session_id
          AND COALESCE(m.is_notification, 0) = 0
        ORDER BY bm.position ASC
        """,
        (branch_id,),
    ).fetchall()
    projection = {
        "input_contract_version": RECAP_INPUT_CONTRACT_VERSION,
        "recap_contract_version": RECAP_CONTRACT_VERSION,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "ordered_messages": [
            {
                "position": position,
                "role": role,
                "timestamp": timestamp,
                "origin": origin,
                "uuid": uuid,
                "parent_uuid": parent_uuid,
                "content": content,
                "tool_content": tool_content,
            }
            for position, role, timestamp, origin, uuid, parent_uuid, content, tool_content in messages
        ],
        "deterministic_summary": {
            "text": branch[7],
            "data": _decoded_json(branch[8]),
        },
        "metadata": {
            "branch": {
                "leaf_uuid": branch[1],
                "started_at": branch[2],
                "ended_at": branch[3],
                "exchange_count": branch[4],
                "commits": _decoded_json(branch[5]),
                "tool_counts": _decoded_json(branch[6]),
            },
            "session": {"uuid": branch[9], "git_branch": branch[10]},
            "project": {"key": branch[11], "name": branch[12]},
        },
    }
    packet = canonical_json(projection)
    return RecapInput(projection, packet, hashlib.sha256(packet).hexdigest())


def refresh_recap_input(cursor: sqlite3.Cursor, branch_id: int) -> RecapInput:
    """Persist current input identity and requeue terminal content-dependent jobs."""
    # The ordinary import code remains usable against an old, unmigrated DB.
    # Recap persistence begins only once T02's atomic schema is present.
    branch_columns = {row[1] for row in cursor.execute("PRAGMA table_info(branches)")}
    link_columns = {row[1] for row in cursor.execute("PRAGMA table_info(branch_messages)")}
    if "recap_input_hash" not in branch_columns or "position" not in link_columns:
        return RecapInput({}, b"", "")
    recap_input = load_recap_input(cursor, branch_id)
    previous = cursor.execute(
        """
        SELECT session_id, recap_input_hash, recap_input_contract_version,
               recap_eligibility_policy_version
        FROM branches WHERE id = ?
        """,
        (branch_id,),
    ).fetchone()
    if previous is None:
        raise RuntimeError(f"active branch {branch_id} disappeared while refreshing recap input")
    session_id, old_hash, old_contract, old_policy = previous
    changed = (old_hash, old_contract, old_policy) != (
        recap_input.input_hash,
        RECAP_INPUT_CONTRACT_VERSION,
        ELIGIBILITY_POLICY_VERSION,
    )
    cursor.execute(
        """
        UPDATE branches
        SET recap_input_hash = ?, recap_input_contract_version = ?, recap_eligibility_policy_version = ?
        WHERE id = ?
        """,
        (recap_input.input_hash, RECAP_INPUT_CONTRACT_VERSION, ELIGIBILITY_POLICY_VERSION, branch_id),
    )
    if changed:
        cursor.execute(
            "UPDATE session_recap_jobs SET requested_input_hash = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
            (recap_input.input_hash, session_id),
        )
        cursor.execute(
            """
            UPDATE session_recap_jobs
            SET state = 'pending', reason = NULL,
                lease_expires_at = NULL, active_attempt_id = NULL, next_eligible_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE session_id = ?
              AND (state IN ('current', 'excluded')
                    OR (state = 'blocked' AND reason IN (?, ?)))
            """,
            (session_id, *CONTENT_DEPENDENT_BLOCK_REASONS),
        )
    return recap_input
