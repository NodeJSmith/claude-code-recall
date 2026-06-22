"""Incremental sync for the current session only (Stop-hook helper — fast and lightweight).

Reads session_id from stdin (or --input-file) and only syncs that session file.
Detects conversation branches (from rewind) and stores each branch separately.

v3 schema: messages stored once per session, branches as a separate index.
"""

import contextlib
import json
import re
import sys
from pathlib import Path

from pydantic import ValidationError

from ccrecall.db import (
    DEFAULT_PROJECTS_DIR,
    get_db_connection,
    load_settings,
    setup_logging,
)
from ccrecall.formatting import extract_project_name, normalize_cwd
from ccrecall.models import HookInput
from ccrecall.session_ops import sync_session

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def validate_session_id(session_id: str) -> bool:
    """Validate that session_id is a proper UUID to prevent path traversal."""
    return bool(session_id and _UUID_RE.match(session_id))


def _is_under(path: Path, base: Path) -> bool:
    """Check whether path resolves to a location under base (symlink-escape guard)."""
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def get_session_file(projects_dir: Path, session_id: str) -> Path | None:
    """Find the JSONL file for a session ID. Validates path stays under projects_dir."""
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue

        # Check main session files
        session_file = project_dir / f"{session_id}.jsonl"
        if session_file.exists():
            # Verify resolved path is still under projects_dir (symlink escape prevention)
            if _is_under(session_file, projects_dir):
                return session_file
            continue

        # Check subagent files
        for subdir in project_dir.iterdir():
            if subdir.is_dir():
                subagents_dir = subdir / "subagents"
                if subagents_dir.exists():
                    for f in subagents_dir.glob(f"*{session_id}*.jsonl"):
                        if _is_under(f, projects_dir):
                            return f

    return None


def run(input_file: Path | None = None) -> None:
    """Sync only the current session into the memory DB (Stop-hook helper)."""
    settings = load_settings()
    logger = setup_logging(settings)

    # Read hook input from file or stdin
    if input_file:
        try:
            raw = input_file.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        finally:
            # Clean up temp file
            with contextlib.suppress(OSError):
                input_file.unlink()
    else:
        raw = sys.stdin.read()

    try:
        hook_input = HookInput.model_validate_json(raw) if raw else HookInput()
    except ValidationError:
        hook_input = HookInput()

    session_id = hook_input.session_id

    if not session_id or not validate_session_id(session_id):
        # No session ID or invalid format — exit silently
        print(json.dumps({"continue": True}))
        return

    # Honor exclude_projects for the live session too — import applies it on the
    # batch path, and without this an excluded project's current session would
    # still sync on Stop. Match by the current cwd's project name. This uses the
    # same formula as the import path (extract_project_name(normalize_cwd(...))),
    # just on the live cwd instead of each session's recorded cwd — identical in
    # the normal case (they're the same cwd). Fail open when cwd is absent: a
    # Stop hook shouldn't block, and cwd is effectively always present.
    exclude_projects = settings["exclude_projects"]
    if exclude_projects and hook_input.cwd:
        project_name = extract_project_name(normalize_cwd(hook_input.cwd))
        if project_name in exclude_projects:
            logger.info("Skipping sync — project %r is excluded", project_name)
            print(json.dumps({"continue": True}))
            return

    session_file = get_session_file(DEFAULT_PROJECTS_DIR, session_id)

    if not session_file:
        print(json.dumps({"continue": True}))
        return

    try:
        conn = get_db_connection(settings, load_vec=True)
        project_dir = session_file.parent

        # Handle subagent paths
        if project_dir.name == "subagents":
            project_dir = project_dir.parent.parent

        # Sync path: write import_log with NULL file_hash as a "synced" marker
        new_messages = sync_session(
            conn,
            session_file,
            project_dir,
            write_import_log=True,
            file_hash=None,
        )
        conn.commit()
        conn.close()

        if new_messages > 0:
            logger.info("Synced %s new message(s) from session %s", new_messages, session_id[:8])

        # Output for hook (continue = True means don't block)
        output = {"continue": True}
        if new_messages > 0:
            output["suppressOutput"] = True  # Don't show in transcript

        print(json.dumps(output))

    except Exception as e:
        logger.error("Sync error: %s", e)
        # Don't block Claude on sync errors
        print(json.dumps({"continue": True}))
        sys.exit(0)
