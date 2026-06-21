"""Write or update ~/.claude-memory/config.json.

Called by Claude during onboarding to persist user configuration choices.
Atomic write via tmp+replace to prevent partial writes.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path

from ccrecall.db import CONFIG_PATH, CURRENT_ONBOARDING_VERSION, DEFAULT_SETTINGS


def run(*, defaults: bool = False, auto_inject_context: bool | None = None) -> None:
    """Write or update ~/.claude-memory/config.json from onboarding choices."""
    # Build initial config from DEFAULT_SETTINGS (plus write_config-specific keys).
    # Skip merge when --defaults is set so it always writes fresh defaults.
    write_config_defaults = {
        "onboarding_completed": False,
        "onboarding_version": 0,
        "auto_inject_context": DEFAULT_SETTINGS["auto_inject_context"],
    }
    config = write_config_defaults.copy()
    if CONFIG_PATH.exists() and not defaults:
        # Malformed/unreadable existing config falls back to defaults; a real bug
        # (not OSError/ValueError) still surfaces.
        with contextlib.suppress(OSError, ValueError):
            existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                config.update(existing)

    # Apply explicit overrides (only when --defaults is not set)
    if not defaults and auto_inject_context is not None:
        config["auto_inject_context"] = auto_inject_context

    # Mark onboarding complete
    config["onboarding_completed"] = True
    config["onboarding_version"] = CURRENT_ONBOARDING_VERSION

    # Atomic write
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(config, indent=2) + "\n")
        Path(tmp_path).replace(CONFIG_PATH)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise

    print(f"Config saved to {CONFIG_PATH}")
