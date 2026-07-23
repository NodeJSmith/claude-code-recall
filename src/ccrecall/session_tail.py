"""Recover a prior session's tail for fast resume.

Powers two things:
  - the ``ccrecall tail`` CLI (invoked by the ccr-resume skill), and
  - the SessionStart context injection's "unresolved decision" warning
    (``context_rendering.py``).

The one thing on-disk artifacts can never tell you is whether the prior session
stopped on a decision the user never made — an AskUserQuestion that was rejected,
interrupted, or simply left open. Reading git/task state and assuming "done" is
how a session ships work the user was still deciding about. This module reads the
raw transcript JSONL to recover exactly that signal.

Why a raw-cwd slug instead of ``get_project_key``: Claude Code writes each
session's transcript into ``~/.claude/projects/<slug>/`` where the slug is the
RAW cwd (``/`` ``.`` ``:`` → ``-``), INCLUDING any ``/.claude/worktrees/<name>``
segment. ``get_project_key`` deliberately normalizes that suffix away so worktree
sessions share a DB project key with the base repo — correct for the DB, wrong
for locating files. So we encode the raw cwd here.
"""

import json
import sys
from collections import deque
from pathlib import Path

from whenever import Instant

from ccrecall.content import (
    extract_text_content,
    is_task_notification,
    is_teammate_message,
    is_tool_result,
)
from ccrecall.db import DEFAULT_PROJECTS_DIR
from ccrecall.errors import emit_error_return
from ccrecall.formatting import split_worktree_path
from ccrecall.parsing import (
    extract_session_metadata,
    extract_session_uuid,
    parse_all_with_uuids,
    parse_lines_with_uuids,
)

# Lines of transcript tail scanned for the latest event timestamp when ordering
# sessions — enough to find a timestamp even if the trailing lines are a
# no-timestamp tool-result burst, without reading a multi-MB file in full.
_TIMESTAMP_TAIL_LINES = 20

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
# Lines of transcript tail the SessionStart hook parses — enough to catch the
# trailing AskUserQuestion + its result without reading a multi-MB file in full.
_HOOK_TAIL_LINES = 400
DEFAULT_TAIL_EVENTS = 8  # CLI -n default

# Clip lengths for pending-question option descriptions and the session preview.
_INJECTION_OPTION_CLIP = 160
_CLI_OPTION_CLIP = 140
_PREVIEW_CLIP = 90
_TOOL_CLIP = 80


def transcript_dir(cwd: str, projects_dir: Path = DEFAULT_PROJECTS_DIR) -> Path:
    """Directory holding this cwd's transcripts (raw slug — see module docstring)."""
    slug = cwd.replace("\\", "/").replace("/", "-").replace(":", "-").replace(".", "-")
    return projects_dir / slug


def transcript_for_uuid(uuid: str, cwd: str | None = None, projects_dir: Path = DEFAULT_PROJECTS_DIR) -> Path | None:
    """Locate a session's transcript file by its session id (filename stem).

    Tries the cwd's project dir first (the common case), then falls back to a
    global glob since session ids are unique.
    """
    if cwd:
        direct = transcript_dir(cwd, projects_dir) / f"{uuid}.jsonl"
        if direct.is_file():
            return direct
    matches = sorted(projects_dir.glob(f"*/{uuid}.jsonl"))
    return matches[0] if matches else None


def load_entries(path: Path) -> list[dict]:
    return list(parse_all_with_uuids(path))


def load_tail_entries(path: Path, tail_lines: int = _HOOK_TAIL_LINES) -> list[dict]:
    """Parse only the last ``tail_lines`` lines into entries with uuids.

    Sufficient for pending-question detection (a session that stalls on a question
    stalls at its end) and bounds the SessionStart hook, which would otherwise
    parse multi-MB transcripts in full on every startup. Not for the CLI tail
    view, which needs a possibly-early last instruction — use load_entries there.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = deque(fh, maxlen=tail_lines)
    return list(parse_lines_with_uuids(lines))


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
    content = entry.get("message", {}).get("content")
    if is_tool_result(content) or is_task_notification(content) or is_teammate_message(content):
        return None
    text, _, _, _, _ = extract_text_content(content)
    if not text:
        return None
    low = text.lstrip().lower()
    if "request interrupted" in low or low.startswith(_NOISE_PREFIXES):
        return None
    return text


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
        content = entry.get("message", {}).get("content")
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
        content = entry.get("message", {}).get("content")
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
    tail = entries[last_entry_idx + 1 :]
    if any(typed_instruction(e) for e in tail if _is_main_chain(e)):
        return None
    return payload


def last_typed_instruction(entries: list[dict]) -> str | None:
    for entry in reversed(entries):
        text = typed_instruction(entry)
        if text:
            return text
    return None


def last_assistant_text(entries: list[dict]) -> str | None:
    for entry in reversed(entries):
        if entry.get("type") == "assistant":
            text, _, _, _, _ = extract_text_content(entry.get("message", {}).get("content"))
            if text:
                return text
    return None


def _brief_path(path: str) -> str:
    """Last two path components — enough to identify the file without noise."""
    parts = Path(path).parts
    if len(parts) <= 2:
        return path
    return "…/" + "/".join(parts[-2:])


def _tool_event(block: dict) -> tuple[str, str]:
    """Extract (lowercase_tag, brief_summary) from a tool_use block."""
    name = block.get("name", "?")
    inp = block.get("input", {})
    tag = name.lower()

    if name == "Bash":
        return tag, clip(inp.get("command", ""), _TOOL_CLIP)
    if name == "Read":
        return tag, _brief_path(inp.get("file_path", ""))
    if name in ("Edit", "Write", "MultiEdit"):
        return tag, _brief_path(inp.get("file_path", ""))
    if name == "Agent":
        desc = inp.get("description", "")
        return tag, desc or clip(inp.get("prompt", ""), _TOOL_CLIP)
    if name == "Skill":
        return tag, inp.get("skill", "")
    if name in ("Grep", "Glob"):
        return tag, clip(inp.get("pattern", ""), _TOOL_CLIP)
    if name == "AskUserQuestion":
        qs = inp.get("questions", [])
        if qs:
            return "ask", clip(qs[0].get("question", ""), _TOOL_CLIP)
        return "ask", ""
    return tag, ""


def build_tail(entries: list[dict], k: int) -> list[tuple[str, str]]:
    """Last ``k`` main-chain events as (tag, body). One assistant entry can yield
    several events (its text plus each tool_use); ``k`` bounds the output, not input.

    Tool events use the lowercase tool name as the tag (``bash``, ``read``, ``edit``,
    ``agent``, etc.) with a brief summary from the tool's input as the body.
    """
    if k <= 0:
        return []
    events: list[tuple[str, str]] = []
    for entry in entries:
        if not _is_main_chain(entry):
            continue
        kind = entry.get("type")
        content = entry.get("message", {}).get("content")
        if kind == "user":
            text = typed_instruction(entry)
            if text:
                events.append(("user", clip(text)))
        elif kind == "assistant":
            text, _, _, _, _ = extract_text_content(content)
            if text:
                events.append(("asst", clip(text)))
            if isinstance(content, list):
                events.extend(
                    _tool_event(block)
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "tool_use"
                )
    return events[-k:]


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


def _last_event_timestamp(path: Path) -> str:
    """Latest ``timestamp`` value among a transcript's last lines, ISO 8601 string.

    ISO 8601 strings sort correctly by plain string comparison, so callers can
    order transcripts with a plain key function. Falls back to the file's mtime
    (also rendered as an ISO string) when no line in the tail window parses as
    JSON with a usable timestamp — e.g. a truncated or corrupt transcript.
    """
    latest: str | None = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        tail_lines = deque(fh, maxlen=_TIMESTAMP_TAIL_LINES)
    for line in tail_lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = entry.get("timestamp")
        if ts and (latest is None or ts > latest):
            latest = ts
    if latest is not None:
        return latest
    return Instant.from_timestamp(path.stat().st_mtime).format_iso(unit="millisecond")


def list_transcripts(pdir: Path) -> list[Path]:
    if not pdir.is_dir():
        return []
    files = [p for p in pdir.glob("*.jsonl") if p.is_file()]
    files.sort(key=_last_event_timestamp, reverse=True)
    return files


def resolve_target(pdir: Path, selector: str | None) -> Path | None:
    """Pick the transcript to show.

    With a selector, match by session-id substring. Without one, assume this runs
    inside the live session (as the ccr-resume skill does): the newest file by
    last-event timestamp is the current session, so the prior session is the
    second-newest. Invoked outside an active session this is off by one — pass
    a selector there.
    """
    sessions = list_transcripts(pdir)
    if not sessions:
        return None
    if selector:
        for p in sessions:
            if selector in p.stem:
                return p
        return None
    return sessions[1] if len(sessions) >= 2 else None


def resolve_target_global(selector: str, projects_dir: Path = DEFAULT_PROJECTS_DIR) -> Path | None:
    """Search all project dirs for a transcript matching *selector* by substring.

    Called as a fallback when resolve_target finds no match in the local project.
    Returns the newest match (by last-event timestamp) when multiple projects
    contain a matching session id.
    """
    if not projects_dir.is_dir():
        return None
    matches: list[Path] = [
        p
        for project_dir in projects_dir.iterdir()
        if project_dir.is_dir()
        for p in project_dir.glob("*.jsonl")
        if p.is_file() and selector in p.stem
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    matches.sort(key=_last_event_timestamp, reverse=True)
    return matches[0]


def first_typed_preview(path: Path) -> str:
    for entry in load_entries(path):
        text = typed_instruction(entry)
        if text:
            return clip(text, _PREVIEW_CLIP)
    return "(no user message)"


def _emit_header(entries: list[dict], path: Path) -> str:
    """Print the session header block and return the session id."""
    meta = extract_session_metadata(entries)
    sid = str(next((e.get("sessionId") for e in entries if e.get("sessionId")), extract_session_uuid(path)))

    print(f"RESUME — prior session {sid[:8]}")
    print(f"  transcript:  {path}")
    print(f"  last active: {meta.get('ended_at') or 'unknown'}")
    print(f"  branch:      {meta.get('git_branch') or 'unknown'}")
    print()
    return sid


def emit(path: Path, k: int, full: bool = False) -> int:
    entries = load_entries(path)
    if not entries:
        return emit_error_return(
            f"transcript is empty: {path}",
            code="empty_transcript",
            exit_code=1,
            remediation="The session file exists but contains no parseable entries.",
        )

    _emit_header(entries, path)

    pending = find_pending_question(entries)
    if pending:
        print(format_pending_block(pending))
        print()

    if full:
        return _emit_full(entries, pending)

    instr = last_typed_instruction(entries)
    if instr:
        print("LAST USER INSTRUCTION:")
        print(f"  {clip(instr)}")
        print()

    tail = build_tail(entries, k)
    if tail:
        print(f"TAIL (last {len(tail)} events):")
        for tag, body in tail:
            print(f"  [{tag}] {body}")
        print()

    if not pending:
        last = last_assistant_text(entries)
        if last:
            print("LAST ASSISTANT MESSAGE (excerpt):")
            print(f"  {clip(last)}")
    return 0


def _emit_full(entries: list[dict], pending: dict | None) -> int:
    """Print the full untruncated last instruction and assistant message."""
    instr = last_typed_instruction(entries)
    if instr:
        print("LAST USER INSTRUCTION:")
        print(instr)
        print()

    if not pending:
        last = last_assistant_text(entries)
        if last:
            print("LAST ASSISTANT MESSAGE:")
            print(last)
    return 0


def _build_search_dirs(provided_cwd: str, *, real_cwd: str | None = None) -> list[Path]:
    """Build ordered list of transcript dirs to search (worktree-specific first).

    When the process is running inside a worktree but --cwd was passed pointing at
    the repo root, the worktree dir is still searched first and a warning is emitted.
    Only activates worktree logic when provided_cwd relates to the same repo.

    When --cwd explicitly names a *different* worktree of the same repo, that
    worktree is searched first (the user asked for it), not the process's own.
    """
    real_cwd_normalized = (real_cwd or str(Path.cwd())).replace("\\", "/")
    real_parts = split_worktree_path(real_cwd_normalized)
    if not real_parts:
        return [transcript_dir(provided_cwd)]

    repo_root, worktree_cwd = real_parts

    provided_parts = split_worktree_path(provided_cwd)
    provided_base = provided_parts[0] if provided_parts else provided_cwd.replace("\\", "/")

    if provided_base.rstrip("/") != repo_root.rstrip("/"):
        return [transcript_dir(provided_cwd)]

    if provided_parts and provided_parts[1].rstrip("/") != worktree_cwd.rstrip("/"):
        primary = provided_parts[1]
    elif provided_base.rstrip("/") == repo_root.rstrip("/") and provided_cwd.replace("\\", "/").rstrip(
        "/"
    ) != worktree_cwd.rstrip("/"):
        print(
            f"note: running in worktree — checking {worktree_cwd} before repo root",
            file=sys.stderr,
        )
        primary = worktree_cwd
    else:
        primary = worktree_cwd

    dirs = [transcript_dir(primary)]
    root_dir = transcript_dir(repo_root)
    if root_dir not in dirs:
        dirs.append(root_dir)

    return dirs


def _resolve_across_dirs(dirs: list[Path], selector: str | None) -> Path | None:
    """Search multiple transcript dirs for a target session.

    The first dir is the primary (contains the current live session when no
    selector is given), so resolve_target skips the newest file there.
    Fallback dirs don't contain the current session, so their newest is fair game.
    """
    for i, pdir in enumerate(dirs):
        if selector:
            target = resolve_target(pdir, selector)
        elif i == 0:
            target = resolve_target(pdir, None)
        else:
            sessions = list_transcripts(pdir)
            target = sessions[0] if sessions else None
        if target is not None:
            return target
    return None


def run(
    selector: str | None = None,
    *,
    list_sessions: bool = False,
    cwd: str | None = None,
    n: int = DEFAULT_TAIL_EVENTS,
    full: bool = False,
) -> int:
    """Print the tail of a prior session's transcript for fast resume."""
    if cwd is None:
        cwd = str(Path.cwd())
    if n < 1:
        return emit_error_return(
            "-n must be >= 1",
            code="invalid_arg",
            exit_code=2,
            remediation="Pass a positive integer: ccrecall tail -n 8",
        )

    search_dirs = _build_search_dirs(cwd)
    valid_dirs = [d for d in search_dirs if d.is_dir()]

    if not valid_dirs:
        pdir = search_dirs[0]
        return emit_error_return(
            f"no project dir for {cwd}",
            code="no_project_dir",
            exit_code=2,
            remediation=f"Expected {pdir}. Use --cwd to specify a different project path.",
        )

    if list_sessions:
        for pdir in valid_dirs:
            sessions = list_transcripts(pdir)
            if sessions:
                print(f"Sessions in {pdir.name} (newest first; newest is the current session):")
                for i, p in enumerate(sessions):
                    marker = "  <- current" if i == 0 else ""
                    print(f"  {p.stem[:8]}  {first_typed_preview(p)}{marker}")
                return 0
        return emit_error_return(
            "no sessions found",
            code="no_sessions",
            exit_code=2,
            remediation="Run a Claude Code session in this project first, then retry.",
        )

    target = _resolve_across_dirs(valid_dirs, selector)

    if target is None and selector:
        target = resolve_target_global(selector)

    if target is None:
        if selector:
            return emit_error_return(
                f"no session matching '{selector}'",
                code="no_match",
                exit_code=2,
                remediation=(
                    "Run ccrecall tail --list to see available sessions, or ccrecall recent to search by project."
                ),
            )
        return emit_error_return(
            "no prior session found (only the current one exists)",
            code="no_prior_session",
            exit_code=2,
            remediation=(
                "This is the first session in this project."
                " Use ccrecall search -q '<topic>' to find sessions in other projects."
            ),
        )

    return emit(target, n, full=full)
