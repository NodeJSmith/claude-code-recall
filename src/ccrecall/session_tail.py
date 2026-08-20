"""Recover a prior session's tail for fast resume.

Powers the ``ccrecall tail`` CLI and the SessionStart "unresolved decision"
warning (``context_rendering.py``). Path resolution lives in
``tail_resolve.py``; pending-question detection in ``tail_pending.py``. This
module owns tail rendering and the CLI orchestrator.
"""

from collections import deque
from pathlib import Path

from ccrecall.content import extract_text_content
from ccrecall.errors import emit_error_return
from ccrecall.parsing import (
    extract_session_metadata,
    extract_session_uuid,
    parse_all_with_uuids,
    parse_lines_with_uuids,
)
from ccrecall.tail_pending import _is_main_chain, clip, find_pending_question, format_pending_block, typed_instruction

# Explicit re-export ("as" with the same name) of private (leading-underscore)
# helpers from tail_resolve.py. _build_search_dirs and _resolve_across_dirs
# have no in-module caller in tail_resolve.py — they're called only from this
# file's run(). _extract_branch, _last_event_timestamp, and _pick_branch_match
# do have in-module callers in tail_resolve.py but are re-exported here solely
# because test_session_tail.py imports them from ccrecall.session_tail rather
# than ccrecall.tail_resolve — not used directly in this file. Either way, the
# "as X" form tells ruff (F401) and pyright (reportUnusedFunction) the
# cross-module use is intentional.
from ccrecall.tail_resolve import _build_search_dirs as _build_search_dirs
from ccrecall.tail_resolve import _extract_branch as _extract_branch
from ccrecall.tail_resolve import _last_event_timestamp as _last_event_timestamp
from ccrecall.tail_resolve import _pick_branch_match as _pick_branch_match
from ccrecall.tail_resolve import _resolve_across_dirs as _resolve_across_dirs
from ccrecall.tail_resolve import list_transcripts, resolve_target_global
from ccrecall.tail_resolve import resolve_target as resolve_target
from ccrecall.tail_resolve import transcript_dir as transcript_dir

# Lines of transcript tail the SessionStart hook parses — enough to catch the
# trailing AskUserQuestion + its result without reading a multi-MB file in full.
_HOOK_TAIL_LINES = 400
DEFAULT_TAIL_EVENTS = 8  # CLI -n default

_PREVIEW_CLIP = 90
_TOOL_CLIP = 80


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
    return list(parse_lines_with_uuids(lines, source=str(path)))


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

    search_dirs, branch_hint = _build_search_dirs(cwd)
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

    target = _resolve_across_dirs(valid_dirs, selector, branch_hint=branch_hint if not selector else None)

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
