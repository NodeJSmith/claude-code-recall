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
)
from ccrecall.models import LOGGER_NAME, HookInput

log = logging.getLogger(LOGGER_NAME)


def main():
    try:
        raw = sys.stdin.read()
        try:
            hook_input = HookInput.model_validate_json(raw)
        except ValidationError:
            log.warning("clear-handoff: malformed hook input on stdin, dropping")
            return

        if hook_input.end_reason != "clear":
            return

        session_id = hook_input.session_id
        cwd = hook_input.cwd
        if not session_id or not cwd:
            return

        settings = load_settings()
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
