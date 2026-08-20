"""
Shared project upsert logic for sync and import pipelines.

Handles project key normalization, path derivation, and the INSERT/UPDATE cascade
into the projects table. Two path strategies are supported:

  - cwd strategy (sync path): direct cwd string from session metadata
  - JSONL-probe strategy (import path): probe the first JSONL in project_dir to
    extract cwd metadata when no direct cwd is available
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
from ccrecall.parsing import extract_session_metadata, parse_all_with_uuids
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
    worktree suffixes are stripped automatically. The project path is derived by one
    of three strategies, in order: a provided ``cwd`` (sync path) is used directly
    after normalization; otherwise a provided ``project_dir`` (import path) is probed
    for cwd metadata in its first JSONL; if neither yields a path, fall back to lossy
    hyphen reconstruction from the key.

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
    if not raw_path:
        raw_path = parse_project_key(normalized_key)

    project_path = normalize_cwd(raw_path)
    project_name = extract_project_name(project_path)

    cursor.execute("SELECT id, path FROM projects WHERE key = ?", (normalized_key,))
    existing = cursor.fetchone()

    if existing:
        project_id = existing[0]
        if project_path != existing[1]:
            cursor.execute(
                "UPDATE projects SET path = ?, name = ? WHERE id = ?",
                (project_path, project_name, project_id),
            )
    else:
        cursor.execute(
            """
            INSERT INTO projects (path, key, name)
            VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET key = excluded.key, name = excluded.name
            """,
            (project_path, normalized_key, project_name),
        )
        cursor.execute("SELECT id FROM projects WHERE key = ?", (normalized_key,))
        project_id = cursor.fetchone()[0]

    return project_id, used_lossy_fallback


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
    """Probe the first JSONL file in project_dir for cwd metadata.

    Returns the cwd string if found, or None if no JSONL exists, has no cwd, or
    the probe fails outright (falls through to upsert_project's lossy
    hyphen-reconstruction fallback either way).
    """
    projects_dir = project_dir.parent
    project_discovery = discover_project_transcript_files(project_dir, projects_dir)
    if not project_discovery.files:
        return None
    jsonl_file = project_discovery.files[0]
    try:
        entries = list(parse_all_with_uuids(jsonl_file))
        meta = extract_session_metadata(entries)
        return meta.get("cwd")
    except Exception:
        # A genuine parse/read failure (vs. simply no cwd in this transcript) is
        # worth surfacing — it means the derived project path/name may be degraded.
        log.warning(
            "project cwd probe failed; falling back to lossy key reconstruction",
            exc_info=True,
            extra={"jsonl_file": str(jsonl_file)},
        )
        return None
