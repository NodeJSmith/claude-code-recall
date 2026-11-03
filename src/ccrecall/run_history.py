"""A record of background runs, and the sessions repair can't fix.

Two small JSON sidecars live beside the database they describe, so runs against
one database never appear in another's history:

  - ``<db>.runs.json``: per run kind, the newest finished run (``last``) and the
    newest successful one (``last_ok``). Written by import, repair, sync and the
    embedding backfill when a run finishes.
  - ``<db>.unrepairable.json``: sessions repair tried and couldn't fix, each with
    the source fingerprint it was tested against. Written only by repair, and
    present only while non-empty.

A run that never finished leaves no record; it is read from the PID marker it
left behind (``read_marker``): a marker naming a live ccrecall process is a
running job, and one naming a dead process is a killed one.

Recording is best-effort: a failure is logged and never fails the run. This
module imports no vector or embedding modules, so the hooks can record on their
own hot paths.
"""

import contextlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from whenever import Instant

from ccrecall.config import (
    PID_KEY_BACKFILL_EMBEDDINGS,
    PID_KEY_IMPORT,
    PID_KEY_SYNC,
    atomic_write_json,
    pid_file_path,
    read_marker_holder,
)
from ccrecall.db_base import is_database_locked
from ccrecall.health import locked_sidecar
from ccrecall.models import LOGGER_NAME

log = logging.getLogger(LOGGER_NAME)

KIND_IMPORT = "import"
KIND_REPAIR = "repair"
KIND_SYNC = "sync"
# Embedding backfill only: `backfill tool-content` and `backfill summaries` record nothing.
KIND_BACKFILL = "backfill"
RUN_KINDS = (KIND_IMPORT, KIND_REPAIR, KIND_SYNC, KIND_BACKFILL)

# Kinds with a PID marker of their own. Repair runs inside `import --repair-gaps`,
# so a killed repair reads as a killed import.
KIND_PID_KEYS = {
    KIND_IMPORT: PID_KEY_IMPORT,
    KIND_SYNC: PID_KEY_SYNC,
    KIND_BACKFILL: PID_KEY_BACKFILL_EMBEDDINGS,
}

OUTCOME_OK = "ok"
OUTCOME_PARTIAL = "partial"  # finished, with some failures
OUTCOME_FAILED = "failed"

REASON_DATABASE_BUSY = "database_busy"
REASON_VECTOR_UNAVAILABLE = "vector_unavailable"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_ERROR = "error"


def runs_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".runs.json")


def unrepairable_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".unrepairable.json")


@dataclass
class RunRecorder:
    """What one run will record when it finishes. The run sets its outcome before it returns."""

    kind: str
    started_at: Instant
    # Only an unscoped run may advance `last_ok`: a scoped backfill (--days,
    # --limit) worked part of the backlog, so it can't prove the rest is done.
    advances_ok: bool = True
    outcome: str = OUTCOME_OK
    reason: str | None = None

    def finish(self, outcome: str, reason: str | None = None) -> None:
        self.outcome = outcome
        self.reason = reason


def failure_reason(exc: BaseException) -> str:
    """``database_busy`` for a lock that outlasted the busy timeout, else ``error``."""
    if isinstance(exc, sqlite3.OperationalError) and is_database_locked(exc):
        return REASON_DATABASE_BUSY
    return REASON_ERROR


@contextlib.contextmanager
def record_run(db_path: Path, kind: str, *, advances_ok: bool = True) -> Iterator[RunRecorder]:
    """Record the run in the body when it finishes.

    An exception records ``failed`` (reason ``database_busy`` for a lock that
    outlasted the timeout, else the reason the body set, else ``error``) and
    re-raises. A Ctrl-C records nothing, so a deliberate interruption never reads
    as a failure. ``SystemExit`` isn't an interruption: it records whatever outcome
    the body set.
    """
    recorder = RunRecorder(kind=kind, started_at=Instant.now(), advances_ok=advances_ok)
    try:
        yield recorder
    except KeyboardInterrupt:
        raise
    except SystemExit:
        write_record(db_path, recorder)
        raise
    except Exception as exc:
        reason = failure_reason(exc)
        if reason == REASON_ERROR and recorder.reason:
            reason = recorder.reason
        recorder.finish(OUTCOME_FAILED, reason)
        write_record(db_path, recorder)
        raise
    else:
        write_record(db_path, recorder)


def write_record(db_path: Path, recorder: RunRecorder) -> None:
    """Merge one finished run into ``<db>.runs.json``. Best-effort: never raises."""
    entry = {
        "outcome": recorder.outcome,
        "reason": recorder.reason,
        "started_at": recorder.started_at.format_iso(),
        "finished_at": Instant.now().format_iso(),
    }
    path = runs_path(db_path)
    try:
        # Read, merge and write all inside the lock: a read outside it could
        # overwrite another kind's fresher record.
        with locked_sidecar(path):
            runs = read_json_object(path)
            previous = runs.get(recorder.kind)
            last_ok = previous.get("last_ok") if isinstance(previous, dict) else None
            if recorder.outcome == OUTCOME_OK and recorder.advances_ok:
                last_ok = entry
            atomic_write_json(path, {**runs, recorder.kind: {"last": entry, "last_ok": last_ok}})
    except Exception:
        log.exception("could not record the run", extra={"kind": recorder.kind, "path": str(path)})


def read_runs(db_path: Path) -> dict[str, dict]:
    """Return ``{kind: {"last": record | None, "last_ok": record | None}}`` for every recorded kind."""
    runs = read_json_object(runs_path(db_path))
    return {kind: value for kind, value in runs.items() if isinstance(value, dict)}


def read_json_object(path: Path) -> dict:
    """Read a JSON object, treating a missing, unreadable or malformed file as empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("unreadable run-history sidecar; treating it as empty", extra={"path": str(path)})
        return {}
    return data if isinstance(data, dict) else {}


def read_unfixable(db_path: Path) -> dict[str, str]:
    """Return ``{session_uuid: source_fingerprint}`` for the sessions repair couldn't fix."""
    sessions = read_json_object(unrepairable_path(db_path)).get("sessions")
    if not isinstance(sessions, dict):
        return {}
    return {uuid: fp for uuid, fp in sessions.items() if isinstance(uuid, str) and isinstance(fp, str)}


def write_unfixable(db_path: Path, sessions: dict[str, str], *, replace: bool) -> None:
    """Store sessions repair couldn't fix. Best-effort: never raises.

    A repair that completed with no failures passes ``replace=True``: the file
    then holds exactly these sessions, or is removed when there are none. Any
    other repair only adds.
    """
    path = unrepairable_path(db_path)
    try:
        with locked_sidecar(path):
            merged = sessions if replace else {**read_unfixable(db_path), **sessions}
            if merged:
                atomic_write_json(path, {"sessions": merged})
            else:
                path.unlink(missing_ok=True)
    except Exception:
        log.exception("could not record unfixable sessions", extra={"path": str(path)})


@dataclass(frozen=True)
class MarkerState:
    """A run that hasn't finished, read from its PID marker."""

    kind: str
    running: bool  # False: the marker names a dead process, so the run was killed
    pid: int
    # The marker is written when the run (or its spawn) starts and never touched
    # again, so its modified time is when the run started, never when it died.
    started_at: Instant


def read_marker(kind: str) -> MarkerState | None:
    """Return the unfinished run of ``kind``, or None when there's no readable marker.

    An empty or corrupt marker (``_spawn_background`` creates it empty and writes
    the PID after the spawn returns) reads as no marker.
    """
    path = pid_file_path(KIND_PID_KEYS[kind])
    try:
        started_at = Instant.from_timestamp(path.stat().st_mtime)
        pid = read_marker_holder(path)
    except ProcessLookupError:
        try:
            pid = int(path.read_text().strip())
        except (ValueError, OSError):
            return None
        return MarkerState(kind=kind, running=False, pid=pid, started_at=started_at)
    except (ValueError, OverflowError, OSError):
        return None
    return MarkerState(kind=kind, running=True, pid=pid, started_at=started_at)
