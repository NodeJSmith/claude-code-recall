"""Pending-question detection and formatting for prior-session resume.

The one thing on-disk artifacts can never tell you is whether the prior session
stopped on a decision the user never made — an AskUserQuestion that was rejected,
interrupted, or simply left open. Reading git/task state and assuming "done" is
how a session ships work the user was still deciding about. This module scans
raw transcript entries to recover exactly that signal, and renders it for
either the CLI (plain text) or the SessionStart hook (markdown).

Self-contained by design: imports only from ``ccrecall.content`` and stdlib, so
``session_tail.py`` can import from here without creating a cycle.
"""

from ccrecall.content import (
    extract_text_content,
    is_task_notification,
    is_teammate_message,
    is_tool_result,
)

# Harness-injected user content that isn't a typed instruction. command/channel
# wrappers, task-notifications, and <local-command-caveat> blocks are already
# handled by extract_text_content / is_task_notification / the "<local-command-"
# prefix below; these are the remainder.
_NOISE_PREFIXES = (
    "<system-reminder>",
    "<local-command-",
    "base directory for this skill:",
)
_TEXT_CLIP = 600

# Clip lengths for pending-question option descriptions.
_INJECTION_OPTION_CLIP = 160
_CLI_OPTION_CLIP = 140


def clip(text: str, limit: int = _TEXT_CLIP) -> str:
    """Collapse whitespace to one line and truncate — for compact tail display;
    this deliberately flattens code blocks and lists."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " […]"


def _is_main_chain(entry: dict) -> bool:
    return not entry.get("isSidechain", False)


def typed_instruction(entry: dict) -> str | None:
    """Return the user's typed text, or None if this 'user' entry isn't a real instruction.

    Filters tool-result echoes, task-notifications, teammate messages, and
    harness-injected noise (interrupt markers, system reminders, skill bodies)
    so the recovered "last instruction" is what the user actually typed.
    """
    if entry.get("type") != "user":
        return None
    # `.get("message", {})` only supplies the {} default when the key is missing;
    # a present-but-null "message" (#171) returns None and crashes the .get below.
    content = (entry.get("message") or {}).get("content")
    if is_tool_result(content) or is_task_notification(content) or is_teammate_message(content):
        return None
    text, _, _, _, _ = extract_text_content(content)
    if not text:
        return None
    low = text.lstrip().lower()
    if "request interrupted" in low or low.startswith(_NOISE_PREFIXES):
        return None
    return text


def _is_command_wrapper(entry: dict) -> bool:
    """True if this main-chain user entry is a slash-command invocation.

    A slash-command turn's raw content is entirely <command-name>/<command-args>
    markup, which extract_text_content strips to empty text — so
    typed_instruction returns None for it and the pending-question detector
    below would otherwise treat the user as never having moved on. Checked
    against the raw (pre-strip) content deliberately, so this stays scoped to
    tail_pending.py rather than teaching content.py a new concept.

    Real command-wrapper tag order varies (``<command-message>`` can precede
    ``<command-name>``), so this can't anchor on where the tag sits — it applies
    the same noise-prefix filter as typed_instruction instead. Without it, a
    noise entry (e.g. a <system-reminder> whose body quotes or documents the
    "<command-name>" tag) would false-positive as a real command invocation and
    hide a genuinely-unresolved question.
    """
    if entry.get("type") != "user":
        return False
    content = (entry.get("message") or {}).get("content")
    if is_tool_result(content) or is_task_notification(content) or is_teammate_message(content):
        return False
    if not isinstance(content, str):
        return False
    if content.lstrip().lower().startswith(_NOISE_PREFIXES):
        return False
    return "<command-name>" in content


def find_pending_question(entries: list[dict]) -> dict | None:
    """The last main-chain AskUserQuestion with no genuine answer, or None.

    Returns the tool_use ``input`` payload (``{"questions": [...]}``) when the
    prior session ended on a decision the user never resolved.
    """
    # Map every tool_use_id to the is_error flag on its tool_result block.
    # Answered: tool_result with no is_error key. Rejected: is_error=true.
    # No tool_result at all: the session ended before the harness delivered one.
    # We scan all entries (sidechain included) — a tool_result resolves its id
    # wherever it lives — but only main-chain questions are considered below.
    result_is_error: dict[str, bool] = {}
    for entry in entries:
        if entry.get("type") != "user":
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    result_is_error[tool_use_id] = bool(block.get("is_error"))

    last = None
    last_entry_idx = -1
    for i, entry in enumerate(entries):
        if not _is_main_chain(entry) or entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                last = (block.get("id"), block.get("input", {}))
                last_entry_idx = i

    if not last:
        return None
    tool_id, payload = last
    if not isinstance(tool_id, str):
        return payload
    if result_is_error.get(tool_id) is False:
        return None
    # No result or is_error=true — only pending if the user didn't move on.
    # Moving on via a slash command counts too: _is_command_wrapper covers the
    # entries typed_instruction alone can't see.
    tail = entries[last_entry_idx + 1 :]
    if any((typed_instruction(e) or _is_command_wrapper(e)) for e in tail if _is_main_chain(e)):
        return None
    return payload


def format_pending_block(payload: dict, *, for_injection: bool = False) -> str:
    """Render a pending-question payload for the CLI (plain) or the hook (markdown)."""
    lines: list[str] = []
    if for_injection:
        lines.append("## ⚠ Unresolved Decision From Prior Session")
        lines.append(
            "The previous session stopped at an AskUserQuestion the user never answered "
            "(rejected, interrupted, or left open — not resolved). Surface it and let the "
            "user decide; do not act on the work it gates or answer it yourself."
        )
        for q in payload.get("questions", []):
            lines.append(f"- **Q:** {q.get('question', '')}")
            lines.extend(
                f"  - {opt.get('label', '')}: {clip(opt.get('description', ''), _INJECTION_OPTION_CLIP)}"
                for opt in q.get("options", [])
            )
    else:
        lines.append("⚠ PENDING QUESTION — prior session stopped at an UNANSWERED AskUserQuestion.")
        lines.append("  Surface this to the user. Do NOT answer it or act on it yourself.")
        for q in payload.get("questions", []):
            lines.append(f"  Q: {q.get('question', '')}")
            for i, opt in enumerate(q.get("options", []), 1):
                desc = clip(opt.get("description", ""), _CLI_OPTION_CLIP)
                lines.append(f"     {i}. {opt.get('label', '')} — {desc}")
    return "\n".join(lines)
