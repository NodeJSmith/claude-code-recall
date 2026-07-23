"""Message content extraction and tool detection utilities."""

import json
import re

# Commit messages are stored truncated — they're for at-a-glance session context,
# not full reconstruction.
MAX_COMMIT_MESSAGE_LEN = 100

# Tool content markers are searchable text extracted from tool_use input fields —
# capped so a single oversized field or tool_use block can't dominate a message row.
TOOL_FIELD_CAP = 200
TOOL_CONTENT_CAP = 300


def extract_tool_strings(value) -> list[str]:
    """Recursively collect string values out of a tool_use input value.

    Handles the shapes Claude Code's tool inputs actually take: plain strings,
    lists (e.g. AskUserQuestion's ``questions``), and nested dicts within those
    lists (e.g. each question's ``options``). Anything else (bool, int, None,
    ...) contributes nothing rather than raising.
    """
    if isinstance(value, str):
        return [value[:TOOL_FIELD_CAP]]
    if isinstance(value, list):
        strings: list[str] = []
        for item in value:
            strings.extend(extract_tool_strings(item))
        return strings
    if isinstance(value, dict):
        strings = []
        for nested in value.values():
            strings.extend(extract_tool_strings(nested))
        return strings
    return []


def build_tool_use_marker(item: dict) -> str:
    """Build a searchable '[ToolName: joined field values]' marker for a tool_use block.

    Generic field-join extraction — no per-tool dispatch table. This deliberately
    covers new tool types Claude Code may ship without code changes. Never raises:
    malformed input (missing keys, wrong types, None where a list is expected)
    falls back to the tool name alone, guaranteed by the isinstance guard below
    and by extract_tool_strings's own exhaustive type handling.
    """
    name = item.get("name", "")
    inp = item.get("input", {})
    if not isinstance(inp, dict):
        return f"[{name}]"
    strings: list[str] = []
    for value in inp.values():
        strings.extend(extract_tool_strings(value))
    joined = " ".join(s.replace("\n", " ") for s in strings)[:TOOL_CONTENT_CAP]
    return f"[{name}: {joined}]" if joined else f"[{name}]"


def extract_text_content(content) -> tuple[str, bool, bool, str | None, str]:
    """
    Extract text from message content.
    Returns: (text, has_tool_use, has_thinking, tool_summary_json, tool_content)

    tool_summary_json is a JSON string like '{"Bash":3,"Read":2}' or None.
    tool_content is a newline-joined string of '[ToolName: ...]' markers, one per
    tool_use block, or "" when there are none. Tool use markers are NOT
    materialized into `text` — they live in this separate field.
    """
    has_tool_use = False
    has_thinking = False
    tool_counts: dict[str, int] = {}

    if isinstance(content, str):
        # Clean up command artifacts
        text = re.sub(r"<command-name>.*?</command-name>", "", content, flags=re.DOTALL)
        text = re.sub(r"<command-message>.*?</command-message>", "", text, flags=re.DOTALL)
        text = re.sub(r"<command-args>.*?</command-args>", "", text, flags=re.DOTALL)
        text = re.sub(
            r"<local-command-stdout>.*?</local-command-stdout>",
            "",
            text,
            flags=re.DOTALL,
        )
        text = re.sub(r"<channel\b[^>]*>\n?([\s\S]*?)\n?</channel>", r"\1", text, flags=re.DOTALL)
        return text.strip(), False, False, None, ""

    if isinstance(content, list):
        texts = []
        tool_markers = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type == "text":
                    texts.append(item.get("text", ""))
                elif item_type == "tool_use":
                    has_tool_use = True
                    tool_name = item.get("name", "")
                    if tool_name:
                        tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
                    tool_markers.append(build_tool_use_marker(item))
                elif item_type == "thinking":
                    has_thinking = True
        tool_summary = json.dumps(tool_counts) if tool_counts else None
        tool_content = "\n".join(tool_markers)
        return "\n".join(texts).strip(), has_tool_use, has_thinking, tool_summary, tool_content

    return "", False, False, None, ""


def parse_origin(entry: dict) -> str | None:
    """Extract clean platform name from origin.server (e.g. 'telegram' from 'plugin:telegram:telegram')."""
    origin = entry.get("origin")
    if not origin or not isinstance(origin, dict):
        return None
    server = origin.get("server") or ""
    if not server:
        return None
    # Pattern: "plugin:telegram:telegram" -> "telegram"
    parts = server.split(":")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return None


def extract_plain_text(content) -> str | None:
    """Join text blocks (or a bare string) into stripped plain text; None if neither shape."""
    if isinstance(content, list):
        texts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        return "\n".join(texts).strip()
    if isinstance(content, str):
        return content.strip()
    return None


def is_task_notification(content) -> bool:
    """Check if content is a task-notification message (subagent result)."""
    text = extract_plain_text(content)
    return text is not None and text.startswith("<task-notification>")


def is_teammate_message(content) -> bool:
    """Detect teammate coordination messages (team reports, idle notifications, shutdown)."""
    text = extract_plain_text(content)
    return text is not None and text.startswith("<teammate-message")


def is_tool_result(content) -> bool:
    """Check if content is a tool result (not a real user message)."""
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "tool_result":
            return True
    return False


def extract_files_modified(content) -> list[str]:
    """Extract file paths from Edit/Write/MultiEdit tool uses."""
    files = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                name = item.get("name", "")
                inp = item.get("input", {})
                if name in ("Edit", "Write", "MultiEdit") and "file_path" in inp:
                    files.append(inp["file_path"])
    return files


def extract_commits(content) -> list[str]:
    """Extract git commit messages from Bash tool uses."""
    commits = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                if item.get("name") == "Bash":
                    cmd = item.get("input", {}).get("command", "")
                    if "git commit" in cmd:
                        match = re.search(r'-m\s+["\']([^"\']+)["\']', cmd)
                        if match:
                            commits.append(match.group(1)[:MAX_COMMIT_MESSAGE_LEN])
    return commits
