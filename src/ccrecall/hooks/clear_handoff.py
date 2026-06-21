"""SessionEnd hook (matcher: clear) — writes handoff file for SessionStart to link sessions."""

import contextlib
import json
import sys

from pydantic import ValidationError
from whenever import Instant

from ccrecall.db import get_db_path, load_settings
from ccrecall.models import HookInput


def main():
    raw = sys.stdin.read()
    try:
        # model_validate_json parses then validates, so this also covers invalid
        # or empty JSON (empty stdin -> "" -> ValidationError -> return).
        hook_input = HookInput.model_validate_json(raw)
    except ValidationError:
        return

    if hook_input.end_reason != "clear":
        return

    session_id = hook_input.session_id
    cwd = hook_input.cwd
    if not session_id or not cwd:
        return

    # Best-effort: a failed handoff write must not surface an error on session clear.
    with contextlib.suppress(Exception):
        settings = load_settings()
        db_path = get_db_path(settings)
        handoff_path = db_path.parent / "clear-handoff.json"
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


if __name__ == "__main__":
    main()
