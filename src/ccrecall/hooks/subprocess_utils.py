"""Lightweight helpers for detached hook subprocesses."""

import subprocess
import sys
from typing import Any


def detached_popen_kwargs() -> dict[str, Any]:
    """Return the cross-platform kwargs used for detached hook subprocesses."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return kwargs
