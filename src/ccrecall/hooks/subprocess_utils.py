"""Lightweight helpers for detached hook subprocesses."""

import contextlib
import ctypes
import gc
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


def try_load_libc() -> ctypes.CDLL | None:
    """Load glibc for malloc_trim (Linux only); None anywhere else or on failure."""
    if sys.platform != "linux":
        return None
    with contextlib.suppress(OSError):
        return ctypes.CDLL("libc.so.6")
    return None


def reclaim_memory(libc: ctypes.CDLL | None) -> None:
    """Free Python objects and hand freed glibc arena pages back to the OS.

    Without malloc_trim, a long-running process keeps every arena it ever grew,
    so its RSS floor ratchets up. Call between batches so peaks don't accumulate.
    """
    gc.collect()
    if libc is not None:
        with contextlib.suppress(Exception):
            libc.malloc_trim(0)
