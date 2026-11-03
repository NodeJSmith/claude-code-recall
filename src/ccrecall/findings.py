"""What a state of the install means: the findings `ccrecall doctor` and `ccrecall status` render.

Both commands render one `Diagnosis`, so they can't disagree. A finding's group
is its severity: `action` (the user should do something), `working` (ccrecall
or a running job has the next move), or `none` (true and normal). The verdict
is the worst group present.

Every check reads the database through one read-only connection, inside one
deferred read transaction, so a sync committing mid-scan can't count a session
as queued in one check and embedded in another. Nothing here writes: not the
database, not the ok-verdict cache, not a sidecar. Each check is isolated: one
that raises becomes a `check-failed` finding and the rest still run.

SessionStart alerts keep their own hot-path checks (health.py); this module
imports the vector stack, which the hook path must not.
"""

import contextlib
import logging
import os
import shlex
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, TypeVar

from whenever import Instant

from ccrecall import config, health, job_lock, run_history
from ccrecall.config import resolve_backfill_stale_hours
from ccrecall.db import CHUNK_DRAFT_QUALITY_FILTER, CHUNK_EMBEDDABLE_BRANCH_FILTER, CONTENT_ERROR_VERSION
from ccrecall.db_base import SCHEMA_VERSION, describe_schema_ahead
from ccrecall.db_vec import (
    chunk_vec_queryable,
    count_exchanges_without_chunks,
    find_stamped_branches_missing_chunks,
    vec_available,
)
from ccrecall.embeddings import (
    EMBEDDING_MODEL,
    EMBEDDING_VERSION,
    MODEL_TOKEN_LIMIT,
    is_model_cached_on_disk,
    model_cache_root,
)
from ccrecall.import_log_ops import import_log_source_index
from ccrecall.ingestion_status import STALE_TAIL_SECONDS, source_fingerprint, summarize_ingestion
from ccrecall.models import LOGGER_NAME
from ccrecall.tool_content_status import classify_pending_sessions
from ccrecall.tool_content_status import count_eligible as count_tool_content_pending

log = logging.getLogger(LOGGER_NAME)

T = TypeVar("T")

GROUP_ACTION = "action"
GROUP_WORKING = "working"
GROUP_NONE = "none"

VERDICT_NEEDS_ATTENTION = "needs-attention"
VERDICT_WORKING = "working"
VERDICT_HEALTHY = "healthy"

# A queued session active this recently is treated as still being written, the
# same window that separates a pending tail from a stale one.
IN_PROGRESS_SECONDS = STALE_TAIL_SECONDS


REPORT_A_BUG = "report a bug at https://github.com/NodeJSmith/claude-code-recall/issues"
REINSTALL_FOR_VEC = "reinstall ccrecall so the sqlite-vec extension loads"
UPGRADE_CCRECALL = "upgrade ccrecall the way you installed it"

# embedding-backlog row reasons, in `doctor --list embedding-backlog`.
BACKLOG_QUEUED = "queued"
BACKLOG_REPAIRED = "repaired"
BACKLOG_MISSING_VECTORS = "missing-vectors"

KIND_LABELS = {
    run_history.KIND_IMPORT: "import",
    run_history.KIND_REPAIR: "gap repair",
    run_history.KIND_SYNC: "sync",
    run_history.KIND_BACKFILL: "embedding backfill",
}
# None: no command reruns it; sync runs again at the end of the next turn.
KIND_FIXES: dict[str, str | None] = {
    run_history.KIND_IMPORT: health.FIX_IMPORT,
    run_history.KIND_REPAIR: health.FIX_REPAIR,
    run_history.KIND_SYNC: None,
    run_history.KIND_BACKFILL: health.FIX_BACKFILL_EMBEDDINGS,
}
KIND_LOGS = {
    run_history.KIND_IMPORT: "ccrecall-import.log",
    run_history.KIND_REPAIR: "ccrecall-import.log",
    run_history.KIND_SYNC: "ccrecall-sync.log",
    run_history.KIND_BACKFILL: "ccrecall-backfill-embed.log",
}
RUN_REASON_PROSE = {
    run_history.REASON_DATABASE_BUSY: "the database was busy; the next run retries",
    run_history.REASON_VECTOR_UNAVAILABLE: "the vector extension (sqlite-vec) couldn't load",
    run_history.REASON_MODEL_UNAVAILABLE: "the embedding model couldn't load",
    run_history.REASON_ERROR: "an error",
}
# Kinds whose run is long enough to report as running, or stuck.
LONG_RUNNING_KINDS = (run_history.KIND_IMPORT, run_history.KIND_BACKFILL)

# Every id a finding can have: the `--json` contract, and what `doctor --list` accepts.
FINDING_IDS = frozenset(
    {
        "no-database",
        "schema-behind",
        "schema-ahead",
        "fixable-sessions",
        "embedding-unavailable",
        "embedding-backlog",
        "not-writable",
        "duplicate-active-branch",
        "sessions-in-progress",
        "deleted-transcripts",
        "skipped-by-design",
        "draft-sessions",
        "unembeddable-sessions",
        "unfixable-sessions",
        "tool-content-pending",
        "check-failed",
        *(f"{kind}-failed" for kind in run_history.RUN_KINDS),
        *(f"{kind}-killed" for kind in run_history.KIND_JOB_KEYS),
        *(f"{kind}-running" for kind in LONG_RUNNING_KINDS),
        *(f"{kind}-stuck" for kind in LONG_RUNNING_KINDS),
    }
)
LISTABLE_IDS = frozenset(
    {
        "fixable-sessions",
        "embedding-backlog",
        "sessions-in-progress",
        "deleted-transcripts",
        "draft-sessions",
        "unembeddable-sessions",
        "unfixable-sessions",
    }
)


@dataclass(frozen=True)
class Finding:
    """One fact about the install. ``id`` and the field names are part of the ``--json`` contract."""

    id: str
    group: str
    message: str
    count: int | None = None
    # An exact command, or a named step where no one command fits. Allowed in any
    # group, so an optional step can be offered without demanding action.
    fix: str | None = None
    listable: bool = False


@dataclass(frozen=True)
class SessionRow:
    """One session behind a listable finding."""

    session_id: str
    project: str | None
    date: str | None
    reason: str | None = None


@dataclass(frozen=True)
class Diagnosis:
    findings: tuple[Finding, ...]
    # sessions: every stored session; embedded_pct: share of embeddable sessions
    # with every exchange vectored (None with nothing embeddable); last_sync: the
    # last successful sync's finish time (None until one has succeeded).
    summary: Mapping[str, object]
    # The sessions behind each listable finding, by finding id.
    sessions: Mapping[str, tuple[SessionRow, ...]]

    @property
    def verdict(self) -> str:
        return verdict_for(self.findings)


class DatabaseUnreadableError(Exception):
    """The database exists but can't be read at all, so nothing could be diagnosed."""


class Found(NamedTuple):
    finding: Finding
    rows: tuple[SessionRow, ...] | None = None


@dataclass(frozen=True)
class Scan:
    """What every check may read besides the database."""

    db_path: Path
    now: Instant
    stale_seconds: float
    runs: Mapping[str, dict]
    job_states: Mapping[str, job_lock.JobState | None]


@dataclass
class Results:
    """Accumulates findings, running each check in isolation."""

    findings: list[Finding] = field(default_factory=list)
    sessions: dict[str, tuple[SessionRow, ...]] = field(default_factory=dict)

    def add(self, found: Found) -> None:
        self.findings.append(found.finding)
        if found.rows is not None:
            self.sessions[found.finding.id] = self.sessions.get(found.finding.id, ()) + found.rows

    def run_check(self, name: str, check: Callable[[], tuple[list[Found], T]]) -> T | None:
        """Run one check; a check that raises becomes a `check-failed` finding, and None is returned."""
        try:
            found, extra = check()
        except Exception as exc:
            log.exception("doctor check failed", extra={"check": name})
            self.add(
                Found(
                    Finding(
                        id="check-failed",
                        group=GROUP_ACTION,
                        message=f"Couldn't check {name}: {type(exc).__name__}. The other checks still ran.",
                        fix=REPORT_A_BUG,
                    )
                )
            )
            return None
        for item in found:
            self.add(item)
        return extra


def verdict_for(findings: Iterable[Finding]) -> str:
    groups = {finding.group for finding in findings}
    if GROUP_ACTION in groups:
        return VERDICT_NEEDS_ATTENTION
    if GROUP_WORKING in groups:
        return VERDICT_WORKING
    return VERDICT_HEALTHY


def for_db(command: str, db_path: Path) -> str:
    """``command`` aimed at ``db_path``: a fix run without ``--db`` would act on the default database."""
    if db_path == config.DEFAULT_DB_PATH:
        return command
    return f"{command} --db {shlex.quote(str(db_path))}"


def diagnose(db_path: Path, settings: dict) -> Diagnosis:
    """Collect every finding about the install whose database is ``db_path``.

    Raises DatabaseUnreadableError when the database exists but can't be opened or read.
    A missing database is a finding, not an error.
    """
    now = Instant.now()
    scan = Scan(
        db_path=db_path,
        now=now,
        stale_seconds=resolve_backfill_stale_hours(settings.get("backfill_stale_hours")) * health.SECONDS_PER_HOUR,
        runs=run_history.read_runs(db_path),
        job_states={kind: job_lock.probe(key) for kind, key in run_history.KIND_JOB_KEYS.items()},
    )
    results = Results()
    summary: dict[str, object] = {"sessions": None, "embedded_pct": None, "last_sync": last_sync_iso(scan.runs)}

    results.run_check("background runs", lambda: (run_findings(scan), None))
    results.run_check("permissions", lambda: (writable_findings(db_path), None))
    results.add(skipped_by_design(settings))

    if not db_path.exists():
        results.add(
            Found(
                Finding(
                    id="no-database",
                    group=GROUP_ACTION,
                    message=f"No database at {db_path} yet, so no history is stored.",
                    fix=for_db(health.FIX_IMPORT, db_path),
                )
            )
        )
        # No database means nothing is stored: a known 0, not an unknown count.
        return Diagnosis(tuple(results.findings), {**summary, "sessions": 0}, results.sessions)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DatabaseUnreadableError(str(exc)) from exc
    with contextlib.closing(conn):
        try:
            conn.execute("PRAGMA query_only = ON")
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as exc:
            # A WAL database in a directory ccrecall can't write to can't even be
            # read: SQLite must create its shared-memory file first.
            blocked = writable_findings(db_path)
            hint = f"; {blocked[0].finding.message}" if blocked else ""
            raise DatabaseUnreadableError(f"{exc}{hint}") from exc
        vec_loaded = load_vec(conn)

        results.run_check(
            "embedding availability", lambda: (embedding_availability_findings(vec_loaded, db_path), None)
        )

        ahead = describe_schema_ahead(db_path)
        if ahead:
            results.add(
                Found(
                    Finding(
                        id="schema-ahead",
                        group=GROUP_ACTION,
                        message=f"Newer version than this install: {ahead}. Checks ran, but may misread it.",
                        fix=UPGRADE_CCRECALL,
                    )
                )
            )
        elif user_version < SCHEMA_VERSION:
            results.add(
                Found(
                    Finding(
                        id="schema-behind",
                        group=GROUP_ACTION,
                        message=(
                            f"The database is at schema v{user_version}, but this install expects "
                            f"v{SCHEMA_VERSION}. Database checks were skipped until it's migrated."
                        ),
                        fix=for_db(health.FIX_IMPORT, db_path),
                    )
                )
            )
            return Diagnosis(tuple(results.findings), summary, results.sessions)

        # One snapshot for every database check. Never committed: this
        # connection only reads.
        conn.execute("BEGIN")
        try:
            run_database_checks(conn, scan, results, summary, vec_loaded=vec_loaded)
        finally:
            conn.rollback()

    return Diagnosis(tuple(results.findings), summary, results.sessions)


def run_database_checks(
    conn: sqlite3.Connection, scan: Scan, results: Results, summary: dict, *, vec_loaded: bool
) -> None:
    lookup = results.run_check("stored sessions", lambda: ([], session_lookup(conn))) or {}
    sources = results.run_check("transcript sources", lambda: ([], import_log_source_index(conn.cursor())))

    pending_tail = None
    if sources is not None:
        pending_tail = results.run_check("ingestion", lambda: ingestion_findings(conn, scan, sources, lookup))
    coverage = results.run_check("embedding coverage", lambda: embedding_findings(conn, scan, lookup, vec_loaded))
    if coverage is not None:
        summary["embedded_pct"] = coverage.embedded_pct

    # In-progress sessions come from both checks; report what the ones that ran found.
    if pending_tail is not None or coverage is not None:
        in_progress = sorted({*(pending_tail or ()), *(coverage.in_progress if coverage else ())})
        if in_progress:
            results.add(
                Found(
                    Finding(
                        id="sessions-in-progress",
                        group=GROUP_NONE,
                        message=(
                            f"{plural(len(in_progress), 'session')} still being written or active in the last "
                            f"{IN_PROGRESS_SECONDS // 60} minutes. Sync catches them up at the end of each turn."
                        ),
                        count=len(in_progress),
                        listable=True,
                    ),
                    rows_for(in_progress, lookup),
                )
            )

    results.run_check("tool content", lambda: (tool_content_findings(conn, sources, scan.db_path), None))
    results.run_check("active branches", lambda: (duplicate_branch_findings(conn), None))
    stored = results.run_check(
        "session count", lambda: ([], conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
    )
    if stored is not None:
        summary["sessions"] = stored


def load_vec(conn: sqlite3.Connection) -> bool:
    try:
        return vec_available(conn)
    except Exception:
        log.exception("sqlite-vec failed to load on the doctor connection")
        return False


def last_sync_iso(runs: Mapping[str, dict]) -> str | None:
    last_ok = runs.get(run_history.KIND_SYNC, {}).get("last_ok")
    return last_ok.get("finished_at") if isinstance(last_ok, dict) else None


def kind_fix(kind: str, scan: Scan) -> str | None:
    command = KIND_FIXES[kind]
    return None if command is None else for_db(command, scan.db_path)


def run_findings(scan: Scan) -> list[Found]:
    """Failed, killed, running and stuck runs, from the run history and the job locks."""
    found: list[Found] = []
    for kind in run_history.RUN_KINDS:
        last = scan.runs.get(kind, {}).get("last")
        if not isinstance(last, dict) or last.get("outcome") not in (
            run_history.OUTCOME_FAILED,
            run_history.OUTCOME_PARTIAL,
        ):
            continue
        reason = last.get("reason") or run_history.REASON_ERROR
        busy = reason == run_history.REASON_DATABASE_BUSY
        # Rerunning can't fix a missing sqlite-vec, so that cause gets the reinstall fix.
        rerun = REINSTALL_FOR_VEC if reason == run_history.REASON_VECTOR_UNAVAILABLE else kind_fix(kind, scan)
        how = "failed" if last.get("outcome") == run_history.OUTCOME_FAILED else "finished with failures"
        started = format_when(parse_instant(last.get("started_at")))
        found.append(
            Found(
                Finding(
                    id=f"{kind}-failed",
                    group=GROUP_WORKING if busy else GROUP_ACTION,
                    message=(
                        f"The last {KIND_LABELS[kind]}, started {started}, {how}: "
                        f"{RUN_REASON_PROSE.get(reason, reason)}. See {KIND_LOGS[kind]}."
                    ),
                    fix=None if busy else (rerun or f"check {KIND_LOGS[kind]}"),
                )
            )
        )

    for kind, state in scan.job_states.items():
        if state is None:
            continue
        label = KIND_LABELS[kind]
        a_label = f"{'An' if label[0] in 'aeiou' else 'A'} {label}"
        started = "at an unknown time" if state.started_at is None else format_when(state.started_at)
        pid_note = "" if state.pid is None else f" (PID {state.pid})"
        age = None if state.started_at is None else (scan.now - state.started_at).total("seconds")
        if not state.running:
            found.append(
                Found(
                    Finding(
                        id=f"{kind}-killed",
                        group=GROUP_WORKING if age is None or age < scan.stale_seconds else GROUP_NONE,
                        message=(
                            f"{a_label} started {started}{pid_note} was killed before it finished. "
                            "The next run of that kind starts over cleanly."
                        ),
                        fix=kind_fix(kind, scan),
                    )
                )
            )
        elif kind in LONG_RUNNING_KINDS and is_stuck(state, scan):
            stop = f"kill {state.pid}" if state.pid is not None else f"stop the running {label}"
            found.append(
                Found(
                    Finding(
                        id=f"{kind}-stuck",
                        group=GROUP_ACTION,
                        message=(
                            f"{a_label} has been running since {started}{pid_note}, "
                            f"longer than backfill_stale_hours. It may still be making progress."
                        ),
                        fix=f"if it's no longer making progress, {stop}, then run `{kind_fix(kind, scan)}`",
                    )
                )
            )
        elif kind in LONG_RUNNING_KINDS:
            running_since = "" if state.started_at is None else f", started {started}"
            found.append(
                Found(
                    Finding(
                        id=f"{kind}-running",
                        group=GROUP_WORKING,
                        message=f"{a_label} is running{running_since}{pid_note}.",
                    )
                )
            )
    return found


def writable_findings(db_path: Path) -> list[Found]:
    """Directories and files ccrecall writes to, checked with os.access (no write probe)."""
    paths = [config.RUNTIME_DIR, db_path.parent, db_path]
    blocked = [str(path) for path in dict.fromkeys(paths) if path.exists() and not os.access(path, os.W_OK)]
    if not blocked:
        return []
    return [
        Found(
            Finding(
                id="not-writable",
                group=GROUP_ACTION,
                message=f"ccrecall can't write to {', '.join(blocked)}, so new history won't be saved.",
                fix="check the permissions and free disk space there",
            )
        )
    ]


def skipped_by_design(settings: dict) -> Found:
    excluded = settings.get("exclude_projects") or []
    projects = f"the {plural(len(excluded), 'project')} in exclude_projects" if excluded else "no excluded projects"
    return Found(
        Finding(
            id="skipped-by-design",
            group=GROUP_NONE,
            message=f"Not indexed by design: {projects}, and subagent transcripts on their own.",
        )
    )


def embedding_availability_findings(vec_loaded: bool, db_path: Path) -> list[Found]:
    """Read from the extension load, the embedding-status record, and the model's disk cache. Never loads the model."""
    causes: list[tuple[str, str]] = []
    if not vec_loaded:
        causes.append(("the vector extension (sqlite-vec) won't load", REINSTALL_FOR_VEC))
    status = health.read_embedding_status()
    if status is not None:
        reason = str(status.get("reason") or "")
        fix = (
            REINSTALL_FOR_VEC
            if reason == health.REASON_VEC_UNAVAILABLE
            else for_db(health.FIX_BACKFILL_EMBEDDINGS, db_path)
        )
        since = format_when(parse_instant(status.get("since")))
        causes.append((f"the last embedding run reported {health.describe_embedding_reason(reason)} ({since})", fix))
    if not is_model_cached_on_disk():
        causes.append(
            (f"the embedding model isn't downloaded yet (not found under {model_cache_root()})", health.FIX_WARM_MODEL)
        )
    if not causes:
        return []
    return [
        Found(
            Finding(
                id="embedding-unavailable",
                group=GROUP_ACTION,
                message=(
                    "Semantic search can't embed new sessions: "
                    + "; ".join(cause for cause, _ in causes)
                    + ". Keyword search still works."
                ),
                fix=causes[0][1],
            )
        )
    ]


def session_lookup(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str | None]]:
    """``{session_uuid: (project, ended_at)}`` for listing rows."""
    rows = conn.execute(
        """
        SELECT s.uuid, COALESCE(p.name, p.key), b.ended_at
        FROM sessions s
        LEFT JOIN projects p ON p.id = s.project_id
        LEFT JOIN branches b ON b.session_id = s.id AND b.is_active = 1
        """
    ).fetchall()
    return {uuid: (project, ended_at) for uuid, project, ended_at in rows}


def rows_for(
    uuids: Iterable[str],
    lookup: Mapping[str, tuple[str | None, str | None]],
    reason: str | None = None,
) -> tuple[SessionRow, ...]:
    rows = [SessionRow(uuid, *lookup.get(uuid, (None, None)), reason=reason) for uuid in uuids]
    return tuple(sorted(rows, key=lambda row: row.date or "", reverse=True))


def ingestion_findings(
    conn: sqlite3.Connection,
    scan: Scan,
    sources: dict[str, dict[str, list[Path]]],
    lookup: Mapping[str, tuple[str | None, str | None]],
) -> tuple[list[Found], list[str]]:
    """Incomplete, unfixable and deleted-transcript sessions. Returns them plus the pending-tail sessions."""
    by_category = summarize_ingestion(conn, sources=sources)["sessions"]
    stored_unfixable = run_history.read_unfixable(scan.db_path)

    fixable: list[str] = []
    unfixable: list[str] = []
    for uuid in by_category["stale_tail"] + by_category["ingestion_gap"]:
        if uuid in stored_unfixable and stored_unfixable[uuid] == current_fingerprint(sources, uuid):
            unfixable.append(uuid)
        else:
            fixable.append(uuid)

    found: list[Found] = []
    if fixable:
        import_state = scan.job_states.get(run_history.KIND_IMPORT)
        # Past the age limit, `import-stuck` reports the run and the gaps need action again.
        stuck = is_stuck(import_state, scan)
        repairing = job_lock.repairs_gaps(import_state, scan.db_path) and not stuck
        if repairing:
            note = "; the running import repairs them."
        elif stuck and job_lock.repairs_gaps(import_state, scan.db_path):
            note = (
                ". The running import repairs gaps but has been going longer than backfill_stale_hours"
                " (see import-stuck)."
            )
        elif import_state is not None and import_state.running and import_state.scope is None:
            note = ". An import is running, but what it covers can't be read."
        elif job_lock.scope_on(import_state, scan.db_path) is not None:
            note = ". An import is running, but it doesn't repair gaps."
        else:
            note = "."
        found.append(
            Found(
                Finding(
                    id="fixable-sessions",
                    group=GROUP_WORKING if repairing else GROUP_ACTION,
                    message=f"{plural(len(fixable), 'session')} missing messages their transcripts still have{note}",
                    count=len(fixable),
                    fix=None if repairing else for_db(health.FIX_REPAIR, scan.db_path),
                    listable=True,
                ),
                rows_for(fixable, lookup),
            )
        )
    if unfixable:
        found.append(
            Found(
                Finding(
                    id="unfixable-sessions",
                    group=GROUP_NONE,
                    message=(
                        f"{plural(len(unfixable), 'session')} can't be completed: the transcript itself is missing "
                        "messages, so retrying won't help. What's stored stays searchable."
                    ),
                    count=len(unfixable),
                    listable=True,
                ),
                rows_for(unfixable, lookup),
            )
        )
    deleted = by_category["missing_source"]
    if deleted:
        found.append(
            Found(
                Finding(
                    id="deleted-transcripts",
                    group=GROUP_NONE,
                    message=(
                        f"{plural(len(deleted), 'session')} whose transcript was deleted or can't be read. "
                        "Their messages are still stored and searchable."
                    ),
                    count=len(deleted),
                    listable=True,
                ),
                rows_for(deleted, lookup),
            )
        )
    return found, by_category["pending_tail"]


def current_fingerprint(sources: dict[str, dict[str, list[Path]]], uuid: str) -> str | None:
    try:
        return source_fingerprint(sources[uuid]["existing"])
    except OSError:
        return None


@dataclass(frozen=True)
class Coverage:
    embedded_pct: int | None
    in_progress: tuple[str, ...]


def embedding_findings(
    conn: sqlite3.Connection,
    scan: Scan,
    lookup: Mapping[str, tuple[str | None, str | None]],
    vec_loaded: bool,
) -> tuple[list[Found], Coverage]:
    """Classify every embeddable session's vectors; report the backlog, drafts and unembeddable sessions."""
    cursor = conn.cursor()
    # Without the extension, chunk_vec can't be read; a chunk then counts as
    # vectored, and embedding-unavailable reports the real problem. With it but
    # no chunk_vec table (only a vector-loading connection creates one), no
    # chunk has a vector yet.
    if not vec_loaded:
        no_vector = ""
    elif chunk_vec_queryable(conn):
        no_vector = "OR NOT EXISTS (SELECT 1 FROM chunk_vec WHERE chunk_id = c.id)"
    else:
        no_vector = "OR 1"
    branches = cursor.execute(
        f"""
        SELECT branches.id, s.uuid, branches.ended_at, branches.embedding_version, branches.embedding_model,
          (SELECT COUNT(*) FROM chunks c WHERE c.branch_id = branches.id),
          (SELECT COUNT(*) FROM chunks c WHERE c.branch_id = branches.id
             AND (c.embedding_version IS NOT ? OR c.embedding_model IS NOT ? {no_vector})),
          (SELECT COUNT(*) FROM chunks c WHERE c.branch_id = branches.id AND {CHUNK_DRAFT_QUALITY_FILTER})
        FROM branches JOIN sessions s ON s.id = branches.session_id
        WHERE branches.{CHUNK_EMBEDDABLE_BRANCH_FILTER}
        """,
        (EMBEDDING_VERSION, EMBEDDING_MODEL, MODEL_TOKEN_LIMIT),
    ).fetchall()
    stamped_missing = find_stamped_branches_missing_chunks(cursor)

    backlog: dict[str, list[tuple[str, str | None]]] = {
        BACKLOG_QUEUED: [],
        BACKLOG_REPAIRED: [],
        BACKLOG_MISSING_VECTORS: [],
    }
    drafts: list[str] = []
    unembeddable: list[str] = []
    in_progress: list[str] = []
    for branch_id, uuid, ended_at, version, model, chunks, stale_chunks, draft_chunks in branches:
        if version == CONTENT_ERROR_VERSION:
            unembeddable.append(uuid)
            continue
        state = vector_state(
            cursor,
            branch_id,
            stamped=version == EMBEDDING_VERSION and model == EMBEDDING_MODEL,
            stamped_missing=branch_id in stamped_missing,
            chunks=chunks,
            stale_chunks=stale_chunks,
            draft_chunks=draft_chunks,
        )
        if state == "draft":
            drafts.append(uuid)
        elif state in backlog:
            ended = parse_instant(ended_at)
            if ended is not None and (scan.now - ended).total("seconds") <= IN_PROGRESS_SECONDS:
                in_progress.append(uuid)
            else:
                backlog[state].append((uuid, ended_at))

    embeddable = len(branches) - len(unembeddable)
    behind = sum(len(rows) for rows in backlog.values()) + len(in_progress)
    embedded_pct = (embeddable - behind) * 100 // embeddable if embeddable else None

    found: list[Found] = []
    backlog_found = backlog_finding(scan, backlog, lookup)
    if backlog_found is not None:
        found.append(backlog_found)
    if drafts:
        recent_backfill = backfill_succeeded_within(scan, scan.stale_seconds)
        found.append(
            Found(
                Finding(
                    id="draft-sessions",
                    group=GROUP_WORKING if recent_backfill else GROUP_NONE,
                    message=(
                        f"{plural(len(drafts), 'session')} searchable at draft quality: long exchanges were "
                        "embedded with a shorter limit. "
                        + (
                            "The next backfill upgrades them."
                            if recent_backfill
                            else "A backfill upgrades them; that's optional."
                        )
                    ),
                    count=len(drafts),
                    fix=None if recent_backfill else for_db(health.FIX_BACKFILL_EMBEDDINGS, scan.db_path),
                    listable=True,
                ),
                rows_for(drafts, lookup),
            )
        )
    if unembeddable:
        found.append(
            Found(
                Finding(
                    id="unembeddable-sessions",
                    group=GROUP_NONE,
                    message=(
                        f"{plural(len(unembeddable), 'session')} couldn't be embedded because of their content "
                        "and won't be retried. Keyword search still finds them."
                    ),
                    count=len(unembeddable),
                    listable=True,
                ),
                rows_for(unembeddable, lookup),
            )
        )
    return found, Coverage(embedded_pct=embedded_pct, in_progress=tuple(in_progress))


def vector_state(
    cursor: sqlite3.Cursor,
    branch_id: int,
    *,
    stamped: bool,
    stamped_missing: bool,
    chunks: int,
    stale_chunks: int,
    draft_chunks: int,
) -> str:
    """One embeddable branch's vectors: a backlog reason, "draft", or "embedded".

    A branch with no chunks was never embedded (queued). One with chunks but
    exchanges that have none got new messages after it was embedded, which is
    what repair produces. A chunk with no current vector needs re-embedding. A
    branch whose every exchange has a current vector is embedded, at draft
    quality if any chunk was capped.
    """
    if stamped:
        return BACKLOG_MISSING_VECTORS if stamped_missing or stale_chunks else "embedded"
    if not chunks:
        return BACKLOG_QUEUED
    if count_exchanges_without_chunks(cursor, branch_id):
        return BACKLOG_REPAIRED
    if stale_chunks:
        return BACKLOG_MISSING_VECTORS
    return "draft" if draft_chunks else "embedded"


def backlog_finding(
    scan: Scan,
    backlog: Mapping[str, list[tuple[str, str | None]]],
    lookup: Mapping[str, tuple[str | None, str | None]],
) -> Found | None:
    """The embedding backlog, which needs action once both clocks pass backfill_stale_hours.

    Those are: no unscoped backfill has succeeded within the limit, and some
    backlog session ended longer ago than the limit. A live, unscoped backfill on
    this database keeps it "working on it" while it is younger than the limit;
    past the limit, `backfill-stuck` reports that run and the backlog is judged
    as if no backfill were running.
    """
    total = sum(len(rows) for rows in backlog.values())
    if not total:
        return None
    rows = tuple(
        row for reason, entries in backlog.items() for row in rows_for([uuid for uuid, _ in entries], lookup, reason)
    )
    parts = [
        f"{len(backlog[BACKLOG_QUEUED])} never embedded",
        f"{len(backlog[BACKLOG_REPAIRED])} with new messages since they were embedded",
        f"{len(backlog[BACKLOG_MISSING_VECTORS])} with exchanges missing vectors",
    ]
    detail = ", ".join(part for part in parts if not part.startswith("0 "))
    message = f"{plural(total, 'session')} waiting for embedding ({detail})."

    backfill_state = scan.job_states.get(run_history.KIND_BACKFILL)
    # A backfill on another database (--db) does nothing for this one, so it reads as no backfill.
    scope_unknown = backfill_state is not None and backfill_state.running and backfill_state.scope is None
    backfilling = scope_unknown or job_lock.scope_on(backfill_state, scan.db_path) is not None
    draining = job_lock.drains_backlog(backfill_state, scan.db_path)
    if draining and not is_stuck(backfill_state, scan):
        group = GROUP_WORKING
        message += " The running backfill is working through them."
        fix = None
    else:
        if draining:
            message += " The running backfill has been going longer than backfill_stale_hours (see backfill-stuck)."
        elif scope_unknown:
            message += " A backfill is running, but what it covers can't be read."
        elif backfilling:
            message += " A backfill limited with --days or --limit is running; it may not reach them all."
        ended = [parse_instant(ended_at) for entries in backlog.values() for _, ended_at in entries]
        oldest = min((instant for instant in ended if instant is not None), default=None)
        overdue = oldest is not None and (scan.now - oldest).total("seconds") > scan.stale_seconds
        if overdue and not backfill_succeeded_within(scan, scan.stale_seconds):
            group = GROUP_ACTION
            message += (
                f" No full backfill has succeeded in the last {scan.stale_seconds / health.SECONDS_PER_HOUR:g} hours"
                f" ({describe_last_backfill(scan)})" + ("." if backfilling else ", so nothing is draining them.")
            )
            fix = for_db(health.FIX_BACKFILL_EMBEDDINGS, scan.db_path)
        else:
            group = GROUP_WORKING
            message += " The next backfill embeds them."
            fix = None
    return Found(
        Finding(id="embedding-backlog", group=group, message=message, count=total, fix=fix, listable=True),
        rows,
    )


def is_stuck(state: job_lock.JobState | None, scan: Scan) -> bool:
    """A live job running longer than backfill_stale_hours. One with no readable start time can't be stuck."""
    if state is None or not state.running or state.started_at is None:
        return False
    return (scan.now - state.started_at).total("seconds") >= scan.stale_seconds


def backfill_succeeded_within(scan: Scan, seconds: float) -> bool:
    last_ok = scan.runs.get(run_history.KIND_BACKFILL, {}).get("last_ok")
    finished = parse_instant(last_ok.get("finished_at")) if isinstance(last_ok, dict) else None
    return finished is not None and (scan.now - finished).total("seconds") <= seconds


def describe_last_backfill(scan: Scan) -> str:
    state = scan.job_states.get(run_history.KIND_BACKFILL)
    if state is not None and not state.running:
        return f"last run, started {format_when(state.started_at)}, was killed"
    last = scan.runs.get(run_history.KIND_BACKFILL, {}).get("last")
    if not isinstance(last, dict):
        return "no backfill has been recorded"
    started = format_when(parse_instant(last.get("started_at")))
    outcome = last.get("outcome")
    if outcome == run_history.OUTCOME_OK:
        return f"last run, started {started}, was limited with --days or --limit"
    if outcome == run_history.OUTCOME_PARTIAL:
        return f"last run, started {started}, finished with failures"
    return f"last run, started {started}, failed"


def tool_content_findings(
    conn: sqlite3.Connection, sources: dict[str, dict[str, list[Path]]] | None, db_path: Path
) -> list[Found]:
    """Older sessions whose tool-call content isn't indexed yet, counting only ones a backfill can fill."""
    cursor = conn.cursor()
    pending = count_tool_content_pending(cursor, None)
    if not pending:
        return []
    if sources is None:
        sources = import_log_source_index(cursor)
    classification = classify_pending_sessions(cursor, None, sources)
    backfillable = pending - classification["missing"] - classification["no_usable_branch"]
    if backfillable <= 0:
        return []
    return [
        Found(
            Finding(
                id="tool-content-pending",
                group=GROUP_NONE,
                message=(
                    f"{plural(backfillable, 'older session')} without searchable tool-call content. "
                    "Indexing it is optional."
                ),
                count=backfillable,
                fix=for_db(health.FIX_BACKFILL_TOOL_CONTENT, db_path),
            )
        )
    ]


def count_branch_invariant_violations(conn: sqlite3.Connection) -> int:
    """Count sessions with more than one active branch row."""
    return len(
        conn.execute(
            "SELECT session_id FROM branches WHERE is_active = 1 GROUP BY session_id HAVING COUNT(*) > 1"
        ).fetchall()
    )


def duplicate_branch_findings(conn: sqlite3.Connection) -> list[Found]:
    violations = count_branch_invariant_violations(conn)
    if not violations:
        return []
    log.warning("sessions with more than one active branch", extra={"sessions": violations})
    return [
        Found(
            Finding(
                id="duplicate-active-branch",
                group=GROUP_ACTION,
                message=f"{plural(violations, 'session')} with more than one active branch, which shouldn't happen.",
                count=violations,
                fix=REPORT_A_BUG,
            )
        )
    ]


def parse_instant(value: object) -> Instant | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return Instant.parse_iso(value)
    except ValueError:
        return None


def format_when(instant: Instant | None) -> str:
    """A local time a reader can place: ``Nov 03 07:59``."""
    if instant is None:
        return "at an unknown time"
    return instant.to_system_tz().to_stdlib().strftime("%b %d %H:%M")


def plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"
