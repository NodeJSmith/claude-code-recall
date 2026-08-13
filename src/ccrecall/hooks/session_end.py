"""Lightweight SessionEnd coordinator for durable recap finalization intent."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError
from whenever import Instant

from ccrecall.config import PID_FILE_MODE, get_db_path, load_settings, log_hook_exception
from ccrecall.formatting import extract_project_name, normalize_cwd
from ccrecall.hooks.durability import fsync_directory, journal_lock
from ccrecall.hooks.handoff import write_clear_handoff
from ccrecall.hooks.subprocess_utils import detached_popen_kwargs
from ccrecall.models import HookInput
from ccrecall.process_cleanup import posix_process_groups_supported
from ccrecall.recap_intent import record_intent

JOURNAL_VERSION = 1
JOURNAL_PREFIX = "recap-finalize-"


def _valid_session_id(value: str | None) -> bool:
    # The hook value is used in a filename, so accept UUID-shaped IDs only.
    parts = value.split("-") if value else []
    return [len(part) for part in parts] == [8, 4, 4, 4, 12] and all(
        char in "0123456789abcdefABCDEF" for part in parts for char in part
    )


def journal_path(db_path: Path, session_uuid: str) -> Path:
    return db_path.parent / f"{JOURNAL_PREFIX}{session_uuid}.json"


def write_fallback_journal(db_path: Path, session_uuid: str, requested_at: str) -> None:
    """Atomically coalesce one content-free replay marker per session."""
    path = journal_path(db_path, session_uuid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with journal_lock(path):
        prior: dict = {}
        with contextlib.suppress(OSError, json.JSONDecodeError):
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prior = loaded
        payload = {
            "version": JOURNAL_VERSION,
            "session_uuid": session_uuid,
            "requested_at": max(str(prior.get("requested_at", "")), requested_at),
            "trigger": "session_end",
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, PID_FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
                handle.flush()
                os.fsync(handle.fileno())
            Path(temporary).replace(path)
            fsync_directory(path.parent)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise


def _spawn_drainer() -> None:
    subprocess.Popen(
        ["ccrecall-drain-session-recaps"],  # noqa: S607 - installed internal console entry point
        **detached_popen_kwargs(),
    )


def main() -> None:
    try:
        raw = sys.stdin.read()
        try:
            hook_input = HookInput.model_validate_json(raw)
        except ValidationError:
            return
        settings = load_settings()
        write_clear_handoff(hook_input, settings)
        session_uuid = hook_input.session_id
        if (
            not settings.get("llm_summaries_enabled", False)
            or session_uuid is None
            or not _valid_session_id(session_uuid)
        ):
            return
        # Recording intent for an excluded project is what later authorizes
        # importing it and sending it to a provider. Fail open when cwd is
        # absent, matching the Stop hook; sync_session_for_finalization is the
        # backstop that refuses the import itself.
        exclude_projects = settings.get("exclude_projects") or []
        if exclude_projects and hook_input.cwd:
            if extract_project_name(normalize_cwd(hook_input.cwd)) in exclude_projects:
                return
        now = Instant.now().format_iso()
        platform_supported = posix_process_groups_supported()
        intent = record_intent(settings, session_uuid, now, platform_supported=platform_supported)
        if intent in {"unknown", "unavailable"}:
            write_fallback_journal(get_db_path(settings), session_uuid, now)
        if platform_supported:
            with contextlib.suppress(OSError):
                _spawn_drainer()
    except Exception:
        log_hook_exception("session-end")
    finally:
        print(json.dumps({}))


if __name__ == "__main__":
    main()
