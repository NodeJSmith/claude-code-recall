"""Dependency-light ownership and durability helpers for hook markers."""

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path


def journal_lock_path(marker: Path) -> Path:
    """Return the owner-only advisory lock path for one fallback marker."""
    return marker.with_suffix(".lock")


@contextlib.contextmanager
def journal_lock(marker: Path) -> Iterator[None]:
    """Serialize all read, replace, and removal operations for one marker."""
    with journal_lock_path(marker).open("a+") as lock:
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
