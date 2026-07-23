"""
JSONL parsing, branch detection, and metadata extraction.
"""

import json
import sqlite3
from collections.abc import Generator, Iterable
from pathlib import Path

from ccrecall.content import (
    extract_commits,
    extract_files_modified,
    is_task_notification,
    is_teammate_message,
    is_tool_result,
)
from ccrecall.models import TranscriptEntry, is_valid


def is_valid_entry(obj: object) -> bool:
    """Validate a raw transcript entry at the ingest boundary.

    Rejects (and logs) lines whose ``message``/``content`` aren't the shapes the
    import path dereferences, so a malformed entry is skipped here instead of
    crashing compute_branch_metadata downstream.
    """
    return is_valid(TranscriptEntry, obj, "transcript entry")


def extract_session_uuid(filepath: Path) -> str:
    """Session UUID from a transcript filename.

    The stem minus an optional ``agent-`` prefix — subagent transcripts are named
    ``agent-<uuid>.jsonl`` and resolve to the same session UUID as the parent.
    """
    stem = filepath.stem
    return stem.removeprefix("agent-")


def parse_jsonl_file(filepath: Path) -> Generator[dict, None, None]:
    """Parse JSONL file, yielding user/assistant entries for import."""
    with open(filepath, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not is_valid_entry(entry):
                continue
            if entry.get("isMeta") and not entry.get("origin"):
                continue
            if entry.get("type") in ("user", "assistant"):
                yield entry


def parse_lines_with_uuids(lines: Iterable[str]) -> Generator[dict, None, None]:
    """Yield parsed JSONL entries that carry a uuid, skipping blanks and bad JSON.

    Shared by the full-file reader and session_tail's tail-bounded reader.
    """
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not is_valid_entry(entry):
            continue
        if entry.get("uuid"):
            yield entry


def parse_all_with_uuids(filepath: Path) -> Generator[dict, None, None]:
    """
    Parse JSONL file yielding ALL entries with UUIDs.
    Used for building the parentUuid chain to find branches.
    """
    with open(filepath, encoding="utf-8", errors="replace") as f:
        yield from parse_lines_with_uuids(f)


def extract_session_metadata(entries: list[dict]) -> dict:
    """Extract session metadata from entries."""
    metadata = {
        "started_at": None,
        "ended_at": None,
        "git_branch": None,
        "cwd": None,
    }

    for entry in entries:
        ts = entry.get("timestamp")
        if ts:
            if metadata["started_at"] is None or ts < metadata["started_at"]:
                metadata["started_at"] = ts
            if metadata["ended_at"] is None or ts > metadata["ended_at"]:
                metadata["ended_at"] = ts

        if not metadata["git_branch"]:
            metadata["git_branch"] = entry.get("gitBranch")
        if not metadata["cwd"]:
            metadata["cwd"] = entry.get("cwd")

    return metadata


def find_all_branches(all_entries: list[dict]) -> list[dict]:
    """
    Find the active conversation branch.

    Returns a single-element list containing the active branch:
      - leaf_uuid: UUID of the last message in this branch
      - uuids: set of all UUIDs on this branch path
      - is_active: always True

    Algorithm: trace from the latest message back to the root via parentUuid.
    """
    uuid_to_entry: dict[str, dict] = {}
    uuid_to_parent: dict[str, str | None] = {}

    for entry in all_entries:
        uuid = entry.get("uuid")
        if not uuid:
            continue
        uuid_to_entry[uuid] = entry
        uuid_to_parent[uuid] = entry.get("parentUuid")

    if not uuid_to_entry:
        return []

    # Find active branch (latest -> root)
    latest = max(uuid_to_entry.values(), key=lambda e: e.get("timestamp") or "")
    active_uuids: set[str] = set()
    current: str | None = latest["uuid"]
    while current:
        active_uuids.add(current)
        current = uuid_to_parent.get(current)

    branches: list[dict] = [
        {
            "leaf_uuid": latest["uuid"],
            "uuids": active_uuids,
            "is_active": True,
        }
    ]

    return branches


def compute_branch_metadata(
    entries: list[dict],
) -> tuple[int, list[str], list[str], dict[str, int]]:
    """
    Compute metadata for a branch's entries in one pass.
    Returns: (exchange_count, files_modified, commits, tool_counts)
    """
    exchange_count = 0
    all_files = []
    all_commits = []
    tool_counts: dict[str, int] = {}
    has_user = False

    for entry in entries:
        entry_type = entry.get("type")
        if entry_type not in ("user", "assistant"):
            continue

        message = entry.get("message", {})
        content = message.get("content", "")

        if entry_type == "user" and is_tool_result(content):
            continue

        if entry_type == "user" and (is_task_notification(content) or is_teammate_message(content)):
            continue

        if entry_type == "user":
            if has_user:
                exchange_count += 1
            has_user = True

        if entry_type == "assistant":
            all_files.extend(extract_files_modified(content))
            all_commits.extend(extract_commits(content))
            # Count tool usage from all assistant entries (including tool-only ones)
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        tool_name = item.get("name", "")
                        if tool_name:
                            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1

    if has_user:
        exchange_count += 1

    # Deduplicate files preserving order
    seen_files = {}
    unique_files = []
    for f in all_files:
        if f not in seen_files:
            seen_files[f] = True
            unique_files.append(f)

    return exchange_count, unique_files, all_commits, tool_counts


def aggregate_branch_content(cursor: sqlite3.Cursor, branch_db_id: int) -> tuple[str, str]:
    """Concatenate a branch's message content and tool content, in timestamp order,
    excluding notifications.

    Returns (msg_text, tool_text) — prose and tool markers kept separate so the
    caller can place tool content in its own aggregated-content section.
    """
    cursor.execute(
        """
        SELECT m.content, m.tool_content FROM branch_messages bm
        JOIN messages m ON bm.message_id = m.id
        WHERE bm.branch_id = ? AND COALESCE(m.is_notification, 0) = 0
        ORDER BY m.timestamp ASC
    """,
        (branch_db_id,),
    )
    rows = cursor.fetchall()
    msg_text = "\n".join(row[0] for row in rows)
    tool_texts = [row[1] for row in rows if row[1]]
    return msg_text, "\n".join(tool_texts)


def build_aggregated_content(
    cursor: sqlite3.Cursor,
    branch_db_id: int,
    files: list[str] | None,
    commits: list[str] | None,
) -> str:
    """Build aggregated FTS content for a branch using SET semantics.

    Concatenates message text (excluding notifications), deduplicated full file
    paths, commit text, and tool content.  Shared by the live sync and import
    paths to ensure format consistency.
    """
    msg_text, tool_text = aggregate_branch_content(cursor, branch_db_id)
    parts = [msg_text]
    if files:
        deduped_paths = list(dict.fromkeys(files))
        parts.append("\n__files__\n" + "\n".join(deduped_paths))
    if commits:
        parts.append("\n__commits__\n" + "\n".join(commits))
    if tool_text:
        parts.append("\n__tools__\n" + tool_text)
    return "".join(parts)
