"""
Shared project upsert logic for sync and import pipelines.

Handles project key normalization, path derivation, and the INSERT/UPDATE cascade
into the projects table. Two path strategies are supported:

  - cwd strategy (sync path): direct cwd string from session metadata
  - JSONL-probe strategy (import path): probe the newest top-level session
    transcript in project_dir for cwd metadata when no direct cwd is available
"""

import logging
import sqlite3
from pathlib import Path

from ccrecall.formatting import (
    extract_project_name,
    get_project_key,
    normalize_cwd,
    normalize_project_key,
    parse_project_key,
)
from ccrecall.models import LOGGER_NAME
from ccrecall.parsing import parse_all_with_uuids
from ccrecall.transcript_sources import discover_project_transcript_files

log = logging.getLogger(LOGGER_NAME)


def upsert_project(
    cursor: sqlite3.Cursor,
    project_key: str,
    cwd: str | None = None,
    project_dir: Path | None = None,
) -> tuple[int, bool]:
    """Upsert a project row and return ``(projects.id, used_lossy_fallback)``.

    ``project_key`` is the encoded project directory name (e.g. ``-home-user-repo``);
    worktree suffixes are stripped automatically. The normalized key is the
    project's identity; ``path`` is metadata. The path is derived by one of three
    strategies, in order: a provided ``cwd`` (sync path) is used directly after
    normalization; otherwise a provided ``project_dir`` (import path) is probed for
    the cwd of its newest top-level session transcript; if neither yields a path,
    fall back to lossy hyphen reconstruction from the key.

    An accurate path (cwd or probe) always replaces the stored one, which is how a
    moved repo heals. A lossy path is written only when inserting a new row, never
    over an existing one.

    The second return value indicates whether the lossy fallback was used (True) or
    an accurate path was available from cwd/probe (False). Callers need this to decide
    whether conservative key-suffix exclusion matching is appropriate.
    """
    normalized_key = normalize_project_key(project_key)

    raw_path: str | None = None
    if cwd is not None:
        raw_path = cwd
    elif project_dir is not None:
        raw_path = _probe_project_dir(project_dir)

    used_lossy_fallback = not raw_path
    project_path = normalize_cwd(raw_path or parse_project_key(normalized_key))
    project_name = extract_project_name(project_path)

    cursor.execute("SELECT id, path FROM projects WHERE key = ?", (normalized_key,))
    existing = cursor.fetchone()

    if existing:
        project_id, stored_path = existing
        if not used_lossy_fallback and project_path != stored_path:
            cursor.execute(
                "UPDATE projects SET path = ?, name = ? WHERE id = ?",
                (project_path, project_name, project_id),
            )
            log.info(
                "project path changed",
                extra={"project_key": normalized_key, "old_path": stored_path, "new_path": project_path},
            )
        return project_id, used_lossy_fallback

    # DO NOTHING rather than DO UPDATE: a racer that inserted this key first keeps
    # its row. If its path was lossy and ours accurate, the key's next write heals it.
    cursor.execute(
        "INSERT INTO projects (path, key, name) VALUES (?, ?, ?) ON CONFLICT(key) DO NOTHING",
        (project_path, normalized_key, project_name),
    )
    cursor.execute("SELECT id FROM projects WHERE key = ?", (normalized_key,))
    return cursor.fetchone()[0], used_lossy_fallback


def key_could_match_excluded(key: str, exclude_projects: list[str]) -> bool:
    """Check whether a normalized project key could encode any excluded project name.

    On the lossy fallback path (no cwd metadata), hyphens in directory names are
    indistinguishable from path separators. This checks conservatively: if the key's
    suffix could represent any excluded name, return True.

    The encoding (get_project_key) replaces ``/``, ``:``, ``.`` with ``-``. So a
    project named ``secret-client`` at ``/home/u/src/secret-client`` produces key
    ``-home-u-src-secret-client``. We check whether the key ends with
    ``-<encoded_name>`` for each excluded name.
    """
    for name in exclude_projects:
        encoded = get_project_key(name)
        if key == encoded or key.endswith("-" + encoded):
            return True
    return False


def _probe_project_dir(project_dir: Path) -> str | None:
    """Return the cwd of the newest top-level session transcript in project_dir that has one.

    Subagent transcripts (nested ``subagents/`` dirs and legacy top-level
    ``agent-*.jsonl``) are skipped: their cwd can be a ``cd``'d subdirectory, which
    would change the project's name and with it the ``exclude_projects`` match.
    Candidates are walked newest mtime first (filename breaks ties); a file that
    fails to read logs a WARNING and the walk moves to the next-older one.

    Returns None if no candidate yields a cwd (upsert_project then falls back to
    lossy hyphen reconstruction).
    """
    project_discovery = discover_project_transcript_files(project_dir, project_dir.parent)
    # Same "." skip as import_project's file loop; "agent-" is the legacy
    # top-level subagent transcript name (see _project_file_order there).
    candidates: list[tuple[float, str, Path]] = []
    for path in project_discovery.files:
        if path.parent != project_dir or path.name.startswith((".", "agent-")):
            continue
        try:
            candidates.append((path.stat().st_mtime, path.name, path))
        except OSError:
            # Vanished between discovery and here (e.g. deleted mid-import).
            log.warning("project cwd probe skipped a transcript it could not stat", extra={"jsonl_file": str(path)})
    candidates.sort(reverse=True)
    for _mtime, _name, jsonl_file in candidates:
        try:
            cwd = _first_cwd(jsonl_file)
        except Exception:
            # A genuine parse/read failure (vs. simply no cwd in this transcript) is
            # worth surfacing — it means the derived project path/name may be degraded.
            log.warning(
                "project cwd probe failed for one transcript; trying the next-older one",
                exc_info=True,
                extra={"jsonl_file": str(jsonl_file)},
            )
            continue
        if cwd:
            return cwd
    return None


def _first_cwd(jsonl_file: Path) -> str | None:
    """Stream jsonl_file and return the first entry's cwd, without reading the rest.

    Deliberately not extract_session_metadata: that needs every entry materialized,
    and a probe that runs on every import must stop at the first cwd.
    """
    entries = parse_all_with_uuids(jsonl_file)
    try:
        for entry in entries:
            if entry.get("cwd"):
                return entry["cwd"]
    finally:
        entries.close()
    return None
