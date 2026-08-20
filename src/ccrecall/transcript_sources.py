import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from ccrecall.models import LOGGER_NAME

log = logging.getLogger(LOGGER_NAME)

_SUBAGENTS_DIRNAME = "subagents"
_STATE_DIRNAME = "state"


@dataclass(frozen=True)
class SessionTranscriptDiscovery:
    files: list[Path]
    had_unsafe_path: bool = False
    had_matching_unsafe_path: bool = False


class _ChildAction(Enum):
    """Per-caller policy for a non-`subagents`-named child dir met during the state/ walk."""

    RECURSE = auto()
    STOP = auto()
    SKIP = auto()


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _resolved_path(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        log.warning("failed to resolve path during subagents walk", extra={"path": str(path)})
        return None


def _process_subagents_walk_child(
    child: Path,
    *,
    on_subagents_dir: Callable[[Path], bool],
    non_subagents_policy: Callable[[Path], _ChildAction],
    pending: list[Path],
) -> bool:
    """Process one child dir during `_walk_subagents_dirs`. Returns True to stop the whole walk.

    Split out of the loop body (rather than an inline try/except) so the try/except
    isn't itself inside a `for` loop — avoids ruff PERF203. Note this is a narrower
    failure-isolation scope than the original `contextlib.suppress(OSError)`, which
    wrapped the whole per-directory loop: an `OSError` on one child used to abort
    every remaining sibling in that directory listing, whereas now only the failing
    child is skipped and the rest of the listing is still walked. Deliberate — a
    single transient stat failure no longer silently drops discovery of unrelated
    siblings — but distinct from "same isolation," so call it out explicitly.
    """
    try:
        if child.name == _SUBAGENTS_DIRNAME:
            return on_subagents_dir(child)
        if child.is_dir():
            action = non_subagents_policy(child)
            if action is _ChildAction.RECURSE:
                pending.append(child)
            elif action is _ChildAction.STOP:
                return True
    except OSError:
        log.warning(
            "failed to process child during subagents walk, skipping",
            extra={"path": str(child)},
        )
    return False


def _walk_subagents_dirs(
    state_dir: Path,
    *,
    on_subagents_dir: Callable[[Path], bool],
    non_subagents_policy: Callable[[Path], _ChildAction],
    dedupe_by_resolved_path: bool,
) -> bool:
    """Walk the `state/` subtree (stack-based, LIFO order), dispatching to caller-supplied policy.

    For every child directory literally named "subagents" (at any depth),
    calls `on_subagents_dir(child)`. If it returns True, the whole walk stops
    and this function returns True. A `subagents`-named child is never itself
    recursed into, whatever the callback returns.

    For every other child directory, calls `non_subagents_policy(child)` to
    decide what happens: RECURSE (queue it for further walking), STOP (stop
    the whole walk, return True — used when a caller performs its own
    fallback search inside the child and finds a match), or SKIP (drop that
    branch, no recursion).

    `dedupe_by_resolved_path` selects which of the two existing (and
    deliberately preserved) visited-path dedup strategies this caller uses —
    see the transcript_sources.py tree-walk section of
    design/specs/013-backfill-transcript-dedup/design.md.
    """
    pending = [state_dir]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        if dedupe_by_resolved_path:
            resolved = _resolved_path(current)
            if resolved is None or resolved in visited:
                continue
            visited.add(resolved)
        else:
            if current in visited:
                continue
            visited.add(current)
        try:
            children = sorted(current.iterdir())
        except OSError:
            log.warning(
                "failed to list directory during subagents walk, skipping subtree",
                extra={"dir": str(current)},
            )
            continue
        for child in children:
            if _process_subagents_walk_child(
                child,
                on_subagents_dir=on_subagents_dir,
                non_subagents_policy=non_subagents_policy,
                pending=pending,
            ):
                return True
    return False


def _symlinked_project_contains_session_candidate(project_dir: Path, session_uuid: str) -> bool:
    direct = project_dir / f"{session_uuid}.jsonl"
    if direct.exists() or direct.is_symlink():
        return True

    direct_subagents = project_dir / _SUBAGENTS_DIRNAME
    if direct_subagents.exists() or direct_subagents.is_symlink():
        try:
            for _path in direct_subagents.glob(f"*{session_uuid}*.jsonl"):
                return True
        except OSError:
            log.warning(
                "failed to glob symlinked subagents dir for session candidate",
                extra={"dir": str(direct_subagents)},
            )

    state_dir = project_dir / _STATE_DIRNAME
    if not (state_dir.exists() or state_dir.is_symlink()):
        return False

    def on_subagents_dir(child: Path) -> bool:
        try:
            for _path in child.glob(f"*{session_uuid}*.jsonl"):
                return True
        except OSError:
            log.warning(
                "failed to glob subagents dir for session candidate",
                extra={"dir": str(child)},
            )
        return False

    def non_subagents_policy(child: Path) -> _ChildAction:
        return _ChildAction.SKIP if child.is_symlink() else _ChildAction.RECURSE

    return _walk_subagents_dirs(
        state_dir,
        on_subagents_dir=on_subagents_dir,
        non_subagents_policy=non_subagents_policy,
        dedupe_by_resolved_path=True,
    )


def _dir_contains_matching_session_transcript(path: Path, session_uuid: str) -> bool:
    pending = [path]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        try:
            for candidate in current.glob(f"*{session_uuid}*.jsonl"):
                if candidate.is_file() or candidate.is_symlink():
                    return True
            pending.extend(child for child in sorted(current.iterdir()) if child.is_dir() and not child.is_symlink())
        except OSError:
            log.warning(
                "failed to search directory for matching session transcript",
                extra={"dir": str(current)},
            )
    return False


def _unsafe_subagent_dirs_contain_session_candidate(project_dir: Path, projects_dir: Path, session_uuid: str) -> bool:
    direct_subagents = project_dir / _SUBAGENTS_DIRNAME
    if direct_subagents.exists() or direct_subagents.is_symlink():
        if (
            direct_subagents.is_symlink()
            or not direct_subagents.is_dir()
            or not _is_under(direct_subagents, projects_dir)
        ) and _dir_contains_matching_session_transcript(direct_subagents, session_uuid):
            return True

    state_dir = project_dir / _STATE_DIRNAME
    if not (state_dir.exists() or state_dir.is_symlink()):
        return False
    if state_dir.is_symlink() or not state_dir.is_dir() or not _is_under(state_dir, projects_dir):
        return _dir_contains_matching_session_transcript(state_dir, session_uuid)

    def on_subagents_dir(child: Path) -> bool:
        return (
            child.is_symlink() or not child.is_dir() or not _is_under(child, projects_dir)
        ) and _dir_contains_matching_session_transcript(child, session_uuid)

    def non_subagents_policy(child: Path) -> _ChildAction:
        if child.is_symlink() or not _is_under(child, projects_dir):
            if _dir_contains_matching_session_transcript(child, session_uuid):
                return _ChildAction.STOP
            return _ChildAction.SKIP
        return _ChildAction.RECURSE

    return _walk_subagents_dirs(
        state_dir,
        on_subagents_dir=on_subagents_dir,
        non_subagents_policy=non_subagents_policy,
        dedupe_by_resolved_path=False,
    )


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _is_safe_transcript_file(path: Path, projects_dir: Path) -> bool:
    return path.exists() and path.is_file() and not path.is_symlink() and _is_under(path, projects_dir)


def is_safe_project_dir(project_dir: Path, projects_dir: Path) -> bool:
    return (
        project_dir.exists()
        and project_dir.is_dir()
        and not project_dir.is_symlink()
        and _is_under(project_dir, projects_dir)
    )


def _discover_safe_project_transcript_files(project_dir: Path, projects_dir: Path) -> SessionTranscriptDiscovery:
    found: list[Path] = []
    had_unsafe_path = False
    for path in sorted(project_dir.glob("*.jsonl")):
        if _is_safe_transcript_file(path, projects_dir):
            found.append(path)
        else:
            had_unsafe_path = True

    subagents_dirs, had_unsafe_subagent_dir = _candidate_subagent_dirs(project_dir, projects_dir)
    had_unsafe_path = had_unsafe_path or had_unsafe_subagent_dir
    for subagents_dir in subagents_dirs:
        if not (subagents_dir.exists() or subagents_dir.is_symlink()):
            continue
        if subagents_dir.is_symlink() or not subagents_dir.is_dir() or not _is_under(subagents_dir, projects_dir):
            had_unsafe_path = True
            continue
        for path in sorted(subagents_dir.glob("*.jsonl")):
            if _is_safe_transcript_file(path, projects_dir):
                found.append(path)
            else:
                had_unsafe_path = True

    return SessionTranscriptDiscovery(files=_dedupe_paths(found), had_unsafe_path=had_unsafe_path)


def discover_project_transcript_files(project_dir: Path, projects_dir: Path) -> SessionTranscriptDiscovery:
    if not (project_dir.exists() or project_dir.is_symlink()):
        return SessionTranscriptDiscovery(files=[])
    if not is_safe_project_dir(project_dir, projects_dir):
        return SessionTranscriptDiscovery(files=[], had_unsafe_path=True)
    return _discover_safe_project_transcript_files(project_dir, projects_dir)


def _candidate_subagent_dirs(project_dir: Path, projects_dir: Path) -> tuple[list[Path], bool]:
    candidates: list[Path] = []
    had_unsafe_path = False

    direct_subagents = project_dir / _SUBAGENTS_DIRNAME
    if direct_subagents.exists() or direct_subagents.is_symlink():
        if (
            direct_subagents.is_symlink()
            or not direct_subagents.is_dir()
            or not _is_under(direct_subagents, projects_dir)
        ):
            had_unsafe_path = True
        else:
            candidates.append(direct_subagents)

    state_dir = project_dir / _STATE_DIRNAME
    if not (state_dir.exists() or state_dir.is_symlink()):
        return candidates, had_unsafe_path
    if state_dir.is_symlink() or not state_dir.is_dir() or not _is_under(state_dir, projects_dir):
        return candidates, True

    def on_subagents_dir(child: Path) -> bool:
        nonlocal had_unsafe_path
        if child.is_symlink() or not child.is_dir() or not _is_under(child, projects_dir):
            had_unsafe_path = True
        else:
            candidates.append(child)
        return False

    def non_subagents_policy(child: Path) -> _ChildAction:
        nonlocal had_unsafe_path
        if child.is_symlink() or not _is_under(child, projects_dir):
            had_unsafe_path = True
            return _ChildAction.SKIP
        return _ChildAction.RECURSE

    _walk_subagents_dirs(
        state_dir,
        on_subagents_dir=on_subagents_dir,
        non_subagents_policy=non_subagents_policy,
        dedupe_by_resolved_path=False,
    )

    return _dedupe_paths(candidates), had_unsafe_path


def discover_session_transcript_files(projects_dir: Path, session_uuid: str) -> SessionTranscriptDiscovery:
    direct_found: list[Path] = []
    subagent_found: list[Path] = []
    had_unsafe_path = False
    had_matching_unsafe_path = False
    if not projects_dir.exists():
        return SessionTranscriptDiscovery(files=[])
    for project_dir in sorted(projects_dir.iterdir()):
        if project_dir.is_symlink():
            had_unsafe_path = True
            had_matching_unsafe_path = had_matching_unsafe_path or _symlinked_project_contains_session_candidate(
                project_dir, session_uuid
            )
            continue
        if not project_dir.is_dir():
            continue
        direct = project_dir / f"{session_uuid}.jsonl"
        if direct.exists() or direct.is_symlink():
            if _is_safe_transcript_file(direct, projects_dir):
                direct_found.append(direct)
            else:
                had_unsafe_path = True
                had_matching_unsafe_path = True
        had_matching_unsafe_path = had_matching_unsafe_path or _unsafe_subagent_dirs_contain_session_candidate(
            project_dir, projects_dir, session_uuid
        )
        subagents_dirs, had_unsafe_subagent_dir = _candidate_subagent_dirs(project_dir, projects_dir)
        had_unsafe_path = had_unsafe_path or had_unsafe_subagent_dir
        for subagents_dir in subagents_dirs:
            if not (subagents_dir.exists() or subagents_dir.is_symlink()):
                continue
            if subagents_dir.is_symlink() or not subagents_dir.is_dir() or not _is_under(subagents_dir, projects_dir):
                had_unsafe_path = True
                continue
            for path in sorted(subagents_dir.glob(f"*{session_uuid}*.jsonl")):
                if _is_safe_transcript_file(path, projects_dir):
                    subagent_found.append(path)
                else:
                    had_unsafe_path = True
                    had_matching_unsafe_path = True
    return SessionTranscriptDiscovery(
        files=_dedupe_paths(direct_found + subagent_found),
        had_unsafe_path=had_unsafe_path,
        had_matching_unsafe_path=had_matching_unsafe_path,
    )


def discover_importable_transcript_files(projects_dir: Path) -> SessionTranscriptDiscovery:
    found: list[Path] = []
    had_unsafe_path = False
    if not projects_dir.exists():
        return SessionTranscriptDiscovery(files=[])
    for project_dir in sorted(projects_dir.iterdir()):
        if project_dir.is_symlink():
            had_unsafe_path = True
            continue
        if not project_dir.is_dir():
            continue
        project_discovery = _discover_safe_project_transcript_files(project_dir, projects_dir)
        found.extend(project_discovery.files)
        had_unsafe_path = had_unsafe_path or project_discovery.had_unsafe_path
    return SessionTranscriptDiscovery(files=_dedupe_paths(found), had_unsafe_path=had_unsafe_path)
