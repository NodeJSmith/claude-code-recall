"""SessionEnd hook (matcher: clear) — writes handoff file for SessionStart to link sessions."""

import json
import sys

from pydantic import ValidationError
from whenever import Instant

from ccrecall.db import CLEAR_HANDOFF_FILENAME, get_db_path, load_settings, log_hook_exception
from ccrecall.models import HookInput


def main():
    try:
        raw = sys.stdin.read()
        try:
            hook_input = HookInput.model_validate_json(raw)
        except ValidationError:
            print(json.dumps({}))
            return

        if hook_input.end_reason != "clear":
            print(json.dumps({}))
            return

        session_id = hook_input.session_id
        cwd = hook_input.cwd
        if not session_id or not cwd:
            print(json.dumps({}))
            return

        settings = load_settings()
        db_path = get_db_path(settings)
        handoff_path = db_path.parent / CLEAR_HANDOFF_FILENAME
        handoff_path.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "cwd": cwd,
                    "timestamp": Instant.now().format_iso(),
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        log_hook_exception("clear-handoff")

    print(json.dumps({}))


if __name__ == "__main__":
    main()
