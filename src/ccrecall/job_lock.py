"""Single-instance job locks: a kernel-held ``flock`` plus a record of who holds it.

Each background job kind has one lock file in the runtime directory,
``.lock-<key>``. It is created once and not deleted in normal operation:
unlinking a file someone holds a lock on would let the next job lock a fresh
inode while the old holder keeps running. The one exception is a lock file this
user can't open at all (left by another user, say), which ``open_lock_file``
replaces. A job runs only while it holds an exclusive, nonblocking ``flock`` on
that file, and writes a JSON record into it (its PID, the key, when it started,
and its scope). A clean exit empties the file before closing it.

So a reader learns everything from the file and the kernel, never from a PID:

  - lock held: the job is running (the record says which run, if it parses);
  - lock free with a record left in it: the holder died without cleaning up (killed);
  - lock free and the file empty or missing: nothing is running.

``spawn`` takes the lock in the parent and hands the open descriptor to the
child, naming it in ``CCRECALL_JOB_LOCK_FD``. The lock belongs to the open file
description, so it passes to the child with no gap, and the child's ``acquire``
adopts it instead of locking again.

A record's scope says what the run covers; ``import_scope`` and
``backfill_scope`` build it, and ``repairs_gaps`` / ``drains_backlog`` read it
for ``doctor``. Each scope names the database the run works on, so a run against
another database (``--db``) never reads as working on this one.

Imports no vector stack (fastembed, numpy, onnxruntime, sqlite-vec), only
``config``, ``models`` and ``whenever``, so the hook hot path can import it.
"""

import contextlib
import fcntl
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whenever import Instant

from ccrecall import config
from ccrecall.models import LOGGER_NAME

log = logging.getLogger(LOGGER_NAME)

# Names the descriptor a spawning parent handed its child (see spawn/adopt).
LOCK_FD_ENV = "CCRECALL_JOB_LOCK_FD"


@dataclass(frozen=True)
class JobState:
    """What a job's lock file says about its latest run."""

    running: bool  # False: the lock is free but a record was left, so the run was killed
    pid: int | None  # None for a spawn the child hasn't adopted yet, or an unreadable record
    started_at: Instant | None  # None only for a running job whose record doesn't parse
    scope: dict | None  # None when the record doesn't parse, so the run's scope is unknown


class JobLock:
    """A held job lock. ``release`` (or leaving the ``with`` block) empties the record and drops the lock."""

    def __init__(self, key: str, fd: int) -> None:
        self.key = key
        self.fd: int | None = fd

    def release(self) -> None:
        """Empty the record, then close the descriptor, which drops the lock. Idempotent."""
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            os.ftruncate(fd, 0)
        except OSError:
            # The lock still drops at close; a record left behind reads as a killed run.
            log.warning("could not empty the job lock record", extra={"key": self.key})
        finally:
            os.close(fd)

    def __enter__(self) -> "JobLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def lock_path(key: str) -> Path:
    return config.RUNTIME_DIR / f".lock-{key}"


def acquire(key: str, scope: dict | None = None) -> JobLock | None:
    """Become the single running job for ``key``, or return None when another holds it (skip, don't queue).

    Adopts the lock a spawning parent handed over (``CCRECALL_JOB_LOCK_FD``) when
    it names this key's lock file; otherwise takes the lock itself. Either way
    the record is rewritten with this process's PID and ``scope``. The caller
    must release the returned lock, including on error paths.
    """
    fd = adopt(key)
    if fd is None:
        fd = lock_exclusive(key)
        if fd is None:
            return None
    write_record(fd, key, os.getpid(), scope)
    return JobLock(key, fd)


def spawn(argv: list[str], key: str, scope: dict | None = None, **popen_kwargs: Any) -> bool:
    """Start ``argv`` as ``key``'s job, holding its lock across the handover. False when the job is already running.

    The parent takes the lock, records the spawn (no PID yet), and passes the
    descriptor to the child, which adopts it in ``acquire``. A spawn that fails
    empties the record so it doesn't read as a killed run.
    """
    fd = lock_exclusive(key)
    if fd is None:
        return False
    try:
        write_record(fd, key, None, scope)
        subprocess.Popen(  # noqa: S603 — spawns a trusted internal command, not untrusted input
            argv,
            pass_fds=(fd,),
            env={**os.environ, LOCK_FD_ENV: str(fd)},
            **popen_kwargs,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
        raise
    finally:
        os.close(fd)
    return True


def probe(key: str) -> JobState | None:
    """Read ``key``'s lock file without taking part in it. Never raises.

    Returns None when no job is running and no readable record was left: the file
    is missing or empty, its lock is free and its record doesn't parse, or the
    file can't be opened or read. Otherwise returns the job's state: running
    while another process holds the lock, killed when the lock is free but a
    record was left. Briefly holds a shared lock to ask the kernel whether a job holds it, so a job starting in
    that instant skips and its next trigger runs it.
    """
    path = lock_path(key)
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("job lock file can't be opened; treating the job as not running", extra={"path": str(path)})
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            held = True
        else:
            held = False
        # Under our shared lock a free file can't change; a held one may be mid-write.
        content = read_all(fd)
    except OSError:
        log.warning("job lock file can't be read; treating the job as not running", extra={"path": str(path)})
        return None
    finally:
        os.close(fd)  # also drops the shared lock

    record = parse_record(content)
    if record is None:
        return JobState(running=True, pid=None, started_at=None, scope=None) if held else None
    pid, started_at, scope = record
    return JobState(running=held, pid=pid, started_at=started_at, scope=scope)


def import_scope(*, repair_gaps: bool, db: Path) -> dict:
    """The scope an import records: whether it repairs gaps, and which database it works on."""
    return {"repair_gaps": repair_gaps, "db": str(resolve_db(db))}


def backfill_scope(*, days: int | None, limit: int | None, db: Path) -> dict:
    """The scope an embedding backfill records: its --days and --limit, and which database it works on."""
    return {"days": days, "limit": limit, "db": str(resolve_db(db))}


def scope_on(state: JobState | None, db: Path) -> dict | None:
    """The scope of ``state`` when it's a running job whose record says it works on ``db``, else None.

    None for a job that isn't running, whose record doesn't parse, or whose
    scope names no database or another one.
    """
    if state is None or not state.running or state.scope is None:
        return None
    recorded = state.scope.get("db")
    if not isinstance(recorded, str) or resolve_db(Path(recorded)) != resolve_db(db):
        return None
    return state.scope


def repairs_gaps(state: JobState | None, db: Path) -> bool:
    """Whether ``state`` is a running import repairing gaps in ``db``."""
    scope = scope_on(state, db)
    return scope is not None and scope.get("repair_gaps") is True


def drains_backlog(state: JobState | None, db: Path) -> bool:
    """Whether ``state`` is a running backfill working ``db``'s whole backlog: no --days and no --limit."""
    scope = scope_on(state, db)
    return scope is not None and scope.get("days") is None and scope.get("limit") is None


def resolve_db(db: Path) -> Path:
    """``db`` as an absolute path with symlinks resolved, so two spellings of one database compare equal."""
    try:
        return db.expanduser().resolve()
    except (OSError, RuntimeError):
        return db.expanduser().absolute()


def lock_exclusive(key: str) -> int | None:
    """Open ``key``'s lock file and take its exclusive lock; None when another process holds it."""
    fd = open_lock_file(key)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    # Upgrades from the PID-marker guard leave a `.pid-<key>` file nothing reads any more.
    with contextlib.suppress(OSError):
        (config.RUNTIME_DIR / f".pid-{key}").unlink(missing_ok=True)
    return fd


def open_lock_file(key: str) -> int:
    """Open (creating if needed) ``key``'s lock file for writing, recreating one this user can't open."""
    path = lock_path(key)
    config.ensure_parent_dir(path)
    flags = os.O_RDWR | os.O_CREAT
    try:
        return os.open(path, flags, config.PRIVATE_FILE_MODE)
    except PermissionError:
        # The runtime directory is the user's own, so a file in it they can't
        # open (left by another user, say) can be replaced.
        log.warning("job lock file can't be opened; recreating it", extra={"path": str(path)})
        path.unlink(missing_ok=True)
        return os.open(path, flags, config.PRIVATE_FILE_MODE)


def adopt(key: str) -> int | None:
    """Take over the descriptor a spawning parent passed for ``key``'s lock, or None when there isn't one.

    The variable is consumed only when this adopts it or when it doesn't name an
    open descriptor. One open on another file (another key's lock, which this
    process may acquire next) stays in the environment for that key's acquire:
    consuming it here would leave that descriptor holding its lock, so the later
    acquire would conflict with its own process and skip.
    """
    raw = os.environ.get(LOCK_FD_ENV)
    if raw is None:
        return None
    try:
        fd = int(raw)
        held = os.fstat(fd)
    except (ValueError, OSError):
        os.environ.pop(LOCK_FD_ENV, None)
        log.warning("handed-over job lock isn't usable; taking the lock afresh", extra={"key": key, "fd": raw})
        return None
    try:
        expected = lock_path(key).stat()
    except OSError:
        return None  # this key's lock file doesn't exist, so the handover can't be for it
    if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
        return None
    os.environ.pop(LOCK_FD_ENV, None)
    # pass_fds made it inheritable; keep it out of this process's own children.
    os.set_inheritable(fd, False)
    return fd


def write_record(fd: int, key: str, pid: int | None, scope: dict | None) -> None:
    """Replace the record in a held lock file. Best-effort: the lock is what guards the job."""
    record = {"pid": pid, "key": key, "started_at": Instant.now().format_iso(), "scope": scope or {}}
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(record).encode(), 0)
    except (OSError, TypeError, ValueError):
        log.warning("could not write the job lock record", extra={"key": key})


def read_all(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 4096):
        chunks.append(chunk)
    return b"".join(chunks)


def parse_record(content: bytes) -> tuple[int | None, Instant, dict] | None:
    """``(pid, started_at, scope)`` from a record, or None when it's empty or doesn't parse."""
    try:
        data = json.loads(content)
        started_at = Instant.parse_iso(data["started_at"])
    except (ValueError, TypeError, KeyError, RecursionError):
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        pid = None
    scope = data.get("scope")
    return pid, started_at, scope if isinstance(scope, dict) else {}
