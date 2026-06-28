"""Write or update ~/.ccrecall/config.json.

Called by Claude during onboarding to persist user configuration choices.
Atomic write via tmp+replace to prevent partial writes.
"""

import contextlib
import json

from ccrecall.db import CONFIG_PATH, CURRENT_ONBOARDING_VERSION, DEFAULT_SETTINGS, atomic_write_json


def run(*, defaults: bool = False, auto_inject_context: bool | None = None) -> None:
    """Write or update ~/.ccrecall/config.json from onboarding choices."""
    # Build initial config from DEFAULT_SETTINGS (plus write_config-specific keys).
    # Skip merge when --defaults is set so it always writes fresh defaults.
    initial_config = {
        "onboarding_completed": False,
        "onboarding_version": 0,
        "auto_inject_context": DEFAULT_SETTINGS["auto_inject_context"],
    }
    config = initial_config.copy()
    if CONFIG_PATH.exists() and not defaults:
        # Malformed existing config falls back to defaults; an unexpected bug still surfaces.
        with contextlib.suppress(OSError, ValueError):
            existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                config.update(existing)

    # Apply explicit overrides (only when --defaults is not set)
    if not defaults and auto_inject_context is not None:
        config["auto_inject_context"] = auto_inject_context

    config["onboarding_completed"] = True
    config["onboarding_version"] = CURRENT_ONBOARDING_VERSION

    atomic_write_json(CONFIG_PATH, config)
    print(f"Config saved to {CONFIG_PATH}")
