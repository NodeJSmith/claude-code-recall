"""SessionEnd hook (matcher: clear) — writes handoff file for SessionStart to link sessions."""

import json
import logging
import sys

from pydantic import ValidationError
from whenever import Instant

from ccrecall.config import (
    CLEAR_HANDOFF_FILENAME,
    atomic_write_json,
    get_db_path,
    load_settings,
    log_hook_exception,
    setup_logging,
)
from ccrecall.models import LOGGER_NAME, HookInput


def main():
    settings = None
    try:
        settings = load_settings()
        logger = setup_logging(settings, process_name="clear-handoff")
    except Exception:
        # Logging setup is best-effort: a bad log path must not cost the user
        # their handoff write below. Fall back to the bare named logger — with
        # no handler attached, .warning()/.exception() calls can't raise
        # (stdlib routes WARNING+ through logging.lastResort to stderr).
        log_hook_exception("clear-handoff")
        logger = logging.getLogger(LOGGER_NAME)

    try:
        raw = sys.stdin.read()
        try:
            hook_input = HookInput.model_validate_json(raw)
        except ValidationError:
            logger.warning(
                "malformed hook input on stdin, dropping",
                extra={"raw_len": len(raw)},
            )
            return

        if hook_input.end_reason != "clear":
            return

        session_id = hook_input.session_id
        cwd = hook_input.cwd
        if not session_id or not cwd:
            return

        db_path = get_db_path(settings)
        handoff_path = db_path.parent / CLEAR_HANDOFF_FILENAME
        # write_text truncates before it writes, so a SessionStart reading
        # concurrently can see half a document, and two sessions clearing at once
        # can interleave. atomic_write_json stages through a per-writer temp file
        # in the same directory and renames, and creates the runtime dir — which
        # matters on a first run, where the missing directory would otherwise
        # raise into the broad except below and lose the handoff silently.
        atomic_write_json(
            handoff_path,
            {
                "session_id": session_id,
                "cwd": cwd,
                "timestamp": Instant.now().format_iso(),
            },
        )
    except Exception:
        log_hook_exception("clear-handoff")
    finally:
        print(json.dumps({}))


if __name__ == "__main__":
    main()
