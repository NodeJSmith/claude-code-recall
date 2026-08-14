"""Content-free clear-session handoff writer shared by SessionEnd entry points."""

import json
import os
import tempfile
from pathlib import Path

from whenever import Instant

from ccrecall.config import CLEAR_HANDOFF_FILENAME, get_db_path, load_settings
from ccrecall.models import HookInput


def write_clear_handoff(hook_input: HookInput, settings: dict | None = None) -> None:
    """Write the clear handoff before any SessionEnd finalization work."""
    if hook_input.end_reason != "clear" or not hook_input.session_id or not hook_input.cwd:
        return
    db_path = get_db_path(settings or load_settings())
    target = db_path.parent / CLEAR_HANDOFF_FILENAME
    # write_text truncates first, so a SessionStart reading concurrently can see
    # half a document. Stage and rename instead, the way the journal marker does.
    # The mkdir matters on a first run: without it this raises, and SessionEnd's
    # broad except would swallow the handoff with no visible failure.
    target.parent.mkdir(parents=True, exist_ok=True)
    # A fixed staging name is shared by every process: two sessions clearing at
    # once truncate each other's payload, and whichever renames second may find
    # its own staged file already gone. mkstemp gives each writer its own, the
    # way write_fallback_journal does.
    #
    # Deliberately without that function's fsync and fsync_directory: this handoff
    # is best-effort and the reader discards it after HANDOFF_STALE_SECONDS anyway,
    # so losing it to a power cut costs one session's continuity hint. The recap
    # journal fsyncs because it is durable intent — losing it drops a recap the
    # user asked for. Catching BaseException rather than Exception so an interrupt
    # mid-write still takes the staged file with it.
    handle, staged_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(
                {"session_id": hook_input.session_id, "cwd": hook_input.cwd, "timestamp": Instant.now().format_iso()},
                stream,
            )
        staged.replace(target)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
