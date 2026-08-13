"""Content-free clear-session handoff writer shared by SessionEnd entry points."""

import json

from whenever import Instant

from ccrecall.config import CLEAR_HANDOFF_FILENAME, get_db_path, load_settings
from ccrecall.models import HookInput


def write_clear_handoff(hook_input: HookInput, settings: dict | None = None) -> None:
    """Write the clear handoff before any SessionEnd finalization work."""
    if hook_input.end_reason != "clear" or not hook_input.session_id or not hook_input.cwd:
        return
    db_path = get_db_path(settings or load_settings())
    (db_path.parent / CLEAR_HANDOFF_FILENAME).write_text(
        json.dumps(
            {"session_id": hook_input.session_id, "cwd": hook_input.cwd, "timestamp": Instant.now().format_iso()},
        ),
        encoding="utf-8",
    )
