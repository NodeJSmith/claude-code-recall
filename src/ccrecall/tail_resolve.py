"""Transcript path resolution and session selection for ``ccrecall tail``.

Locates transcript files on disk and picks the right one: by cwd-derived
project dir, by session-id substring, by worktree-aware search order, or by
branch-hinted fallback. No pending-question or rendering logic lives here —
see ``tail_pending.py`` and ``session_tail.py`` for those.
"""

import json
import logging
import sys
from collections import deque
from pathlib import Path

from whenever import Instant

from ccrecall.db import DEFAULT_PROJECTS_DIR
from ccrecall.formatting import split_worktree_path
from ccrecall.models import LOGGER_NAME

log = logging.getLogger(LOGGER_NAME)

# Lines of transcript tail scanned for the latest event timestamp when ordering
# sessions — enough to find a timestamp even if the trailing lines are a
# no-timestamp tool-result burst, without reading a multi-MB file in full.
_TIMESTAMP_TAIL_LINES = 20
_BRANCH_HEAD_LINES = 20


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
            log.debug("failed to parse transcript tail line: %s", path, exc_info=True)
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


def _build_search_dirs(  # pyright: ignore[reportUnusedFunction] -- called only from session_tail.run()
    provided_cwd: str, *, real_cwd: str | None = None
) -> tuple[list[Path], str | None]:
    """Build ordered list of transcript dirs to search (worktree-specific first).

    Returns ``(dirs, branch_hint)`` where *branch_hint* is the resolved worktree
    name (used as a branch filter for fallback dirs), or None when not in a worktree.

    When the process is running inside a worktree but --cwd was passed pointing at
    the repo root, the worktree dir is still searched first and a warning is emitted.
    Only activates worktree logic when provided_cwd relates to the same repo.

    When --cwd explicitly names a *different* worktree of the same repo, that
    worktree is searched first (the user asked for it), not the process's own.
    """
    real_cwd_normalized = (real_cwd or str(Path.cwd())).replace("\\", "/")
    real_parts = split_worktree_path(real_cwd_normalized)
    if not real_parts:
        return [transcript_dir(provided_cwd)], None

    repo_root, worktree_cwd = real_parts

    provided_parts = split_worktree_path(provided_cwd)
    provided_base = provided_parts[0] if provided_parts else provided_cwd.replace("\\", "/")

    if provided_base.rstrip("/") != repo_root.rstrip("/"):
        return [transcript_dir(provided_cwd)], None

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

    # Worktree dir name == branch for `claude --worktree <branch>`;
    # won't match slash-containing branches — falls back to newest.
    branch_hint = Path(primary).name
    return dirs, branch_hint


def _extract_branch(path: Path) -> str | None:
    """Read the first ~20 lines of a transcript to find its git branch.

    Cheap head-read used to filter fallback candidates by branch — avoids
    parsing the full file.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _BRANCH_HEAD_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("failed to parse branch extraction line: %s", path, exc_info=True)
                    continue
                branch = entry.get("gitBranch")
                if branch:
                    return branch
    except OSError:
        log.warning("failed to read transcript for branch extraction: %s", path, exc_info=True)
        return None
    return None


def _pick_branch_match(sessions: list[Path], branch_hint: str | None) -> Path | None:
    """From a recency-sorted list, prefer the newest session matching *branch_hint*.

    Falls back to the overall newest if no branch match is found (or no hint).
    """
    if not sessions:
        return None
    if not branch_hint:
        return sessions[0]
    for path in sessions:
        if _extract_branch(path) == branch_hint:
            return path
    return sessions[0]


def _resolve_across_dirs(  # pyright: ignore[reportUnusedFunction] -- called only from session_tail.run()
    dirs: list[Path], selector: str | None, *, branch_hint: str | None = None
) -> Path | None:
    """Search multiple transcript dirs for a target session.

    The first dir is the primary (contains the current live session when no
    selector is given), so resolve_target skips the newest file there.
    Fallback dirs don't contain the current session, so their newest is fair
    game — but when a *branch_hint* is provided, the fallback prefers a
    session on the same branch over the globally newest.
    """
    for i, pdir in enumerate(dirs):
        if selector:
            target = resolve_target(pdir, selector)
        elif i == 0:
            target = resolve_target(pdir, None)
        else:
            sessions = list_transcripts(pdir)
            target = _pick_branch_match(sessions, branch_hint)
        if target is not None:
            return target
    return None
