import contextlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionTranscriptDiscovery:
    files: list[Path]
    had_unsafe_path: bool = False
    had_matching_unsafe_path: bool = False


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _resolved_path(path: Path) -> Path | None:
    with contextlib.suppress(OSError, RuntimeError):
        return path.resolve()
    return None


def _symlinked_project_contains_session_candidate(project_dir: Path, session_uuid: str) -> bool:
    direct = project_dir / f"{session_uuid}.jsonl"
    if direct.exists() or direct.is_symlink():
        return True

    direct_subagents = project_dir / "subagents"
    if direct_subagents.exists() or direct_subagents.is_symlink():
        with contextlib.suppress(OSError):
            for _path in direct_subagents.glob(f"*{session_uuid}*.jsonl"):
                return True

    state_dir = project_dir / "state"
    if not (state_dir.exists() or state_dir.is_symlink()):
        return False

    pending = [state_dir]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        resolved = _resolved_path(current)
        if resolved is None or resolved in visited:
            continue
        visited.add(resolved)
        with contextlib.suppress(OSError):
            for child in sorted(current.iterdir()):
                if child.name == "subagents":
                    with contextlib.suppress(OSError):
                        for _path in child.glob(f"*{session_uuid}*.jsonl"):
                            return True
                elif child.is_dir() and not child.is_symlink():
                    pending.append(child)
    return False


def _dir_contains_matching_session_transcript(path: Path, session_uuid: str) -> bool:
    pending = [path]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        with contextlib.suppress(OSError):
            for candidate in current.glob(f"*{session_uuid}*.jsonl"):
                if candidate.is_file() or candidate.is_symlink():
                    return True
            pending.extend(child for child in sorted(current.iterdir()) if child.is_dir() and not child.is_symlink())
    return False


def _unsafe_subagent_dirs_contain_session_candidate(project_dir: Path, projects_dir: Path, session_uuid: str) -> bool:
    direct_subagents = project_dir / "subagents"
    if direct_subagents.exists() or direct_subagents.is_symlink():
        if (
            direct_subagents.is_symlink()
            or not direct_subagents.is_dir()
            or not _is_under(direct_subagents, projects_dir)
        ) and _dir_contains_matching_session_transcript(direct_subagents, session_uuid):
            return True

    state_dir = project_dir / "state"
    if not (state_dir.exists() or state_dir.is_symlink()):
        return False
    if state_dir.is_symlink() or not state_dir.is_dir() or not _is_under(state_dir, projects_dir):
        return _dir_contains_matching_session_transcript(state_dir, session_uuid)

    pending = [state_dir]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        with contextlib.suppress(OSError):
            for child in sorted(current.iterdir()):
                if child.name == "subagents":
                    if (
                        child.is_symlink() or not child.is_dir() or not _is_under(child, projects_dir)
                    ) and _dir_contains_matching_session_transcript(child, session_uuid):
                        return True
                elif child.is_dir():
                    if child.is_symlink() or not _is_under(child, projects_dir):
                        if _dir_contains_matching_session_transcript(child, session_uuid):
                            return True
                    else:
                        pending.append(child)
    return False


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

    direct_subagents = project_dir / "subagents"
    if direct_subagents.exists() or direct_subagents.is_symlink():
        if (
            direct_subagents.is_symlink()
            or not direct_subagents.is_dir()
            or not _is_under(direct_subagents, projects_dir)
        ):
            had_unsafe_path = True
        else:
            candidates.append(direct_subagents)

    state_dir = project_dir / "state"
    if not (state_dir.exists() or state_dir.is_symlink()):
        return candidates, had_unsafe_path
    if state_dir.is_symlink() or not state_dir.is_dir() or not _is_under(state_dir, projects_dir):
        return candidates, True

    pending = [state_dir]
    visited: set[Path] = set()

    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        with contextlib.suppress(OSError):
            for child in sorted(current.iterdir()):
                if child.name == "subagents":
                    if child.is_symlink() or not child.is_dir() or not _is_under(child, projects_dir):
                        had_unsafe_path = True
                        continue
                    candidates.append(child)
                elif child.is_dir():
                    if child.is_symlink() or not _is_under(child, projects_dir):
                        had_unsafe_path = True
                    else:
                        pending.append(child)

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
