"""Dependency-light primitives for safe provider process cleanup."""

import os
import sys
from pathlib import Path


def posix_process_groups_supported() -> bool:
    """Whether this host can prove and clean up provider process groups."""
    return (
        sys.platform == "linux"
        and Path("/proc/self/stat").is_file()
        and hasattr(os, "killpg")
        and hasattr(os, "getpgid")
    )


def process_start_identity(pid: int) -> str | None:
    """Return Linux process start ticks, or ``None`` when identity is uncertain."""
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()[19]
    except (FileNotFoundError, IndexError, OSError):
        return None


def process_group_absent(group_id: int) -> bool:
    """Prove a Linux process group has no non-zombie members; errors fail closed."""
    try:
        for stat_path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = stat_path.read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
            except FileNotFoundError:
                # An unrelated process exited between the glob and the read. It
                # cannot be a live member, and failing closed on it would report
                # every group as present whenever the machine is busy.
                continue
            if fields[0] != "Z" and int(fields[2]) == group_id:
                return False
    except (IndexError, OSError, ValueError):
        return False
    return True
