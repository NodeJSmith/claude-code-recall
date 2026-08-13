"""Dependency-light ownership and durability helpers for hook markers."""

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

# fcntl is POSIX-only. SessionEnd imports this module to write its clear-session
# handoff, which has nothing to do with recaps, so an unconditional import here
# would take the whole hook down on Windows before that handoff is written.
if sys.platform != "win32":
    import fcntl


def journal_lock_path(marker: Path) -> Path:
    """Return the owner-only advisory lock path for one fallback marker."""
    return marker.with_suffix(".lock")


@contextlib.contextmanager
def journal_lock(marker: Path) -> Iterator[None]:
    """Serialize all read, replace, and removal operations for one marker."""
    with journal_lock_path(marker).open("a+") as lock:
        if sys.platform == "win32":
            # Nothing contends for the marker here: the drainer that would replay
            # it refuses to start without POSIX process groups, so the SessionEnd
            # writer is the only toucher.
            yield
            return
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def fsync_directory(path: Path) -> None:
    """Persist a directory entry change or raise so durable callers can report it."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
