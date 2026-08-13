"""Consolidated health/status reporting for the CLI.

Default status reads are read-only; ``--check-ingestion`` also records
confirmed-OK cache metadata for future deep-check runs.
"""

import contextlib
import json
import sqlite3
import sys
from pathlib import Path

from ccrecall.config import DEFAULT_DB_PATH, get_db_path, load_settings
from ccrecall.db import branch_embedding_coverage, chunk_vec_queryable, get_connection, vec_available
from ccrecall.hooks.backfill_status import count_status as count_embedding_status
from ccrecall.import_log_ops import import_log_source_index
from ccrecall.ingestion_status import summarize_ingestion
from ccrecall.llm_summary_db import recap_schema_capability
from ccrecall.process_cleanup import posix_process_groups_supported
from ccrecall.recap_contract import ELIGIBILITY_POLICY_VERSION, RECAP_CONTRACT_VERSION, RECAP_INPUT_CONTRACT_VERSION
from ccrecall.recap_eligibility import iter_evaluate_branches
from ccrecall.recap_state import latest_attempts, quarantine_admission
from ccrecall.summary_enrichment import valid_current_enrichment
from ccrecall.tool_content_status import (
    count_eligible as count_tool_content_pending,
)
from ccrecall.tool_content_status import (
    count_pending_missing_jsonl,
)
from ccrecall.tool_content_status import (
    count_total_sessions as count_tool_content_total,
)


def _settings_for_db(db: Path) -> dict:
    settings = load_settings()
    if db != DEFAULT_DB_PATH:
        settings["db_path"] = str(db)
    return settings


@contextlib.contextmanager
def _readonly_connection(db_path: Path, *, load_vec: bool = False):
    """Open an existing SQLite DB without creating/migrating it."""
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    try:
        if load_vec:
            vec_available(conn)
        yield conn
    finally:
        conn.close()


def _db_counts(conn: sqlite3.Connection) -> dict[str, int]:
    cursor = conn.cursor()
    projects = cursor.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    sessions = cursor.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    messages = cursor.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    branches = cursor.execute("SELECT COUNT(*) FROM branches").fetchone()[0]
    active_branches = cursor.execute("SELECT COUNT(*) FROM branches WHERE is_active = 1").fetchone()[0]
    branch_invariant_violations = count_branch_invariant_violations(conn)
    return {
        "projects": projects,
        "sessions": sessions,
        "messages": messages,
        "branches": branches,
        "active_branches": active_branches,
        "branch_invariant_violations": branch_invariant_violations,
    }


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def count_branch_invariant_violations(conn: sqlite3.Connection) -> int:
    """Count sessions with more than one active branch row."""
    return len(
        conn.execute(
            "SELECT session_id FROM branches WHERE is_active = 1 GROUP BY session_id HAVING COUNT(*) > 1"
        ).fetchall()
    )


def _recap_status(conn: sqlite3.Connection, settings: dict) -> dict:
    """Report recap state using only bounded SQLite metadata reads."""
    capability = recap_schema_capability(conn)
    provider_supported = posix_process_groups_supported()
    result = {
        "capability": capability,
        "contracts": {
            "recap": RECAP_CONTRACT_VERSION,
            "input": RECAP_INPUT_CONTRACT_VERSION,
            "eligibility_policy": ELIGIBILITY_POLICY_VERSION,
        },
        "platform": {
            "provider_supported": provider_supported,
            "reason": None if provider_supported else "platform_unsupported",
        },
        "defaults": {
            "enabled": settings["llm_summaries_enabled"],
            "model": settings["llm_summary_model"],
            "max_budget_usd": settings["llm_summary_max_budget_usd"],
            "timeout_seconds": settings["llm_summary_timeout_seconds"],
        },
    }
    if capability != "ready":
        return result
    eligibility_reasons: dict[str, int] = {}
    for _branch_id, decision in iter_evaluate_branches(conn.cursor(), active_only=True):
        eligibility_reasons[decision.reason] = eligibility_reasons.get(decision.reason, 0) + 1
    jobs = dict(conn.execute("SELECT state, COUNT(*) FROM session_recap_jobs GROUP BY state"))
    blocked_platform = conn.execute(
        "SELECT COUNT(*) FROM session_recap_jobs WHERE state = 'blocked' AND reason = 'platform_unsupported'"
    ).fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM session_recap_jobs WHERE state = 'pending'").fetchone()[0]
    runnable = conn.execute(
        "SELECT COUNT(*) FROM session_recap_jobs WHERE state = 'pending' "
        "AND (next_eligible_at IS NULL OR next_eligible_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
    ).fetchone()[0]
    provider_disabled_pending = pending if not provider_supported else 0
    if not provider_supported:
        runnable = 0
    health = conn.execute(
        "SELECT reason, consecutive_failures, retry_after FROM session_recap_provider_health WHERE singleton = 1"
    ).fetchone() or (None, 0, None)
    admitted, quarantine_count, quarantine_bytes, quarantine_oldest = quarantine_admission(
        conn,
        max_count=settings["recap_quarantine_max_count"],
        max_bytes=settings["recap_quarantine_max_bytes"],
    )
    overdue = conn.execute(
        "SELECT COUNT(*) FROM session_recap_jobs WHERE state = 'claimed' "
        "AND lease_expires_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
    ).fetchone()[0]
    retryable = [
        {
            "session": session_uuid,
            "command": f"ccrecall backfill llm-summaries --session {session_uuid} --retry-failures",
        }
        for (session_uuid,) in conn.execute(
            "SELECT s.uuid FROM session_recap_jobs j JOIN sessions s ON s.id = j.session_id "
            "WHERE j.state = 'blocked' AND j.reason IN ('budget_exceeded', 'unusable_output', 'timeout_exhausted') "
            "ORDER BY s.uuid"
        )
    ]
    cleanup = [
        {
            "session": session_uuid,
            "command": "ccrecall recap recover",
        }
        for (session_uuid,) in conn.execute(
            "SELECT s.uuid FROM session_recap_jobs j JOIN sessions s ON s.id = j.session_id "
            "WHERE j.state = 'blocked' AND j.reason = 'cleanup_failed' ORDER BY s.uuid"
        )
    ]
    populations = {"current": 0, "stale": 0, "legacy": 0, "pre_recap": 0}
    for enrichment, version, status, input_hash, materialized_hash, input_contract, policy in conn.execute(
        "SELECT summary_enrichment_json, summary_enrichment_version, summary_enrichment_status, "
        "recap_input_hash, summary_enrichment_input_hash, summary_enrichment_input_contract_version, "
        "summary_enrichment_policy_version FROM branches WHERE is_active = 1"
    ):
        if enrichment is None:
            populations["pre_recap"] += 1
        elif version != RECAP_CONTRACT_VERSION:
            populations["legacy"] += 1
        elif valid_current_enrichment(
            enrichment,
            status=status,
            current_input_hash=input_hash,
            materialized_input_hash=materialized_hash,
            materialized_input_contract_version=input_contract,
            materialized_policy_version=policy,
            stored_enrichment_version=version,
        ):
            populations["current"] += 1
        else:
            populations["stale"] += 1

    result.update(
        {
            "jobs": {
                "by_state": jobs,
                "runnable": runnable,
                "platform_unsupported": blocked_platform,
                "provider_disabled_pending": provider_disabled_pending,
            },
            "overdue_claims": overdue,
            "provider_health": {"reason": health[0], "consecutive_failures": health[1], "retry_after": health[2]},
            "populations": populations,
            "eligibility": {"by_reason": eligibility_reasons},
            "latest_attempt_outcomes": {},
            "quarantine": {
                "count": quarantine_count,
                "bytes": quarantine_bytes,
                "oldest": quarantine_oldest,
                "max_count": settings["recap_quarantine_max_count"],
                "max_bytes": settings["recap_quarantine_max_bytes"],
                "admission_paused": not admitted,
            },
            "guidance": {
                "recover": "ccrecall recap recover" if overdue else None,
                "retry": retryable,
                "cleanup": cleanup,
                "maintain": "ccrecall recap maintain",
                "quarantine": "ccrecall recap maintain" if not admitted else None,
            },
        }
    )
    outcomes: dict[str, int] = {}
    for attempt in latest_attempts(conn, limit=20):
        state = attempt[12]
        outcomes[state] = outcomes.get(state, 0) + 1
    result["latest_attempt_outcomes"] = outcomes
    return result


def collect_status(*, db: Path = DEFAULT_DB_PATH, days: int | None = None, check_ingestion: bool = False) -> dict:
    """Collect status across DB, ingestion, tool content, and embeddings."""
    settings = _settings_for_db(db)
    db_path = get_db_path(settings)
    ingestion = None

    with _readonly_connection(db_path, load_vec=False) as conn:
        cursor = conn.cursor()
        db_counts = _db_counts(conn)
        recap = _recap_status(conn, settings)
        schema_current = _has_column(conn, "messages", "tool_content")

        if not schema_current:
            return {
                "db_path": str(db_path),
                "db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
                "days": days,
                "database": db_counts,
                "schema": {"current": False, "reason": "messages.tool_content column missing"},
                "recap": recap,
                "ingestion": None,
                "tool_content": {
                    "total_sessions": None,
                    "done_sessions": None,
                    "pending_sessions": None,
                    "pending_backfillable_sessions": None,
                    "pending_missing_jsonl_sessions": None,
                },
                "embeddings": {
                    "watermark": {"embedded_branches": None, "total_branches": None},
                    "backfill": {
                        "available": False,
                        "error": "database schema is out of date; run `ccrecall import` to migrate",
                        "branches": {"embedded": None, "total": None, "remaining": None, "errored": None},
                        "chunks": {"done": None, "total": None},
                    },
                },
            }

        tool_pending = count_tool_content_pending(cursor, days)
        source_index = import_log_source_index(cursor) if tool_pending else None
        tool_missing = count_pending_missing_jsonl(cursor, days, source_index) if tool_pending else 0
        tool_total = count_tool_content_total(cursor, days)
        embedded_watermark, embeddable_watermark = branch_embedding_coverage(conn)

    if check_ingestion:
        if not db_path.exists():
            raise FileNotFoundError(db_path)
        with get_connection(settings, load_vec=False) as conn:
            ingestion = summarize_ingestion(conn, sources=import_log_source_index(conn.cursor()))

    embedding_backfill: dict = {
        "available": False,
        "error": "sqlite-vec unavailable or not initialized",
        "branches": {"embedded": None, "total": None, "remaining": None, "errored": None},
        "chunks": {"done": None, "total": None},
    }
    try:
        with _readonly_connection(db_path, load_vec=True) as conn:
            if chunk_vec_queryable(conn):
                counts = count_embedding_status(conn.cursor(), days)
                embedding_backfill = {
                    "available": True,
                    "error": None,
                    "branches": {
                        "embedded": counts["embedded_branches"],
                        "total": counts["total_branches"],
                        "remaining": counts["eligible"],
                        "errored": counts["errored"],
                    },
                    "chunks": {"done": counts["done"], "total": counts["universe"]},
                }
    except (sqlite3.Error, OSError) as exc:
        embedding_backfill["error"] = str(exc)

    return {
        "db_path": str(db_path),
        "db_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "days": days,
        "database": db_counts,
        "schema": {"current": True, "reason": None},
        "recap": recap,
        "ingestion": ingestion,
        "tool_content": {
            "total_sessions": tool_total,
            "done_sessions": tool_total - tool_pending,
            "pending_sessions": tool_pending,
            "pending_backfillable_sessions": tool_pending - tool_missing,
            "pending_missing_jsonl_sessions": tool_missing,
        },
        "embeddings": {
            "watermark": {"embedded_branches": embedded_watermark, "total_branches": embeddable_watermark},
            "backfill": embedding_backfill,
        },
    }


def print_status_report(status: dict) -> None:
    """Render collected status in a compact human-readable form."""
    db = status["database"]
    print(f"Database: {status['db_path']}")
    print(f"Size: {status['db_size_bytes'] / (1024 * 1024):.2f} MB")
    print(f"Projects: {db['projects']}")
    print(f"Sessions: {db['sessions']}")
    print(f"Branches: {db['branches']} ({db['active_branches']} active)")
    print(f"Messages: {db['messages']}")
    print(f"Branch invariant violations: {db['branch_invariant_violations']} session(s) with multiple active branches")

    recap = status.get("recap")
    if recap:
        print(f"Session Recaps: {recap['capability']}")
        if recap["capability"] != "ready":
            print("  Run `ccrecall import` before recap status is available.")
        else:
            health = recap["provider_health"]
            jobs = recap["jobs"]
            print(f"  platform: {'ready' if recap['platform']['provider_supported'] else recap['platform']['reason']}")
            print(f"  enabled: {recap['defaults']['enabled']}; model: {recap['defaults']['model']}")
            print(
                f"  jobs: {jobs['by_state']}; runnable: {jobs['runnable']}; "
                f"provider-disabled pending: {jobs['provider_disabled_pending']}; "
                f"overdue: {recap['overdue_claims']}"
            )
            print(f"  populations: {recap['populations']}; latest outcomes: {recap['latest_attempt_outcomes']}")
            print(f"  eligibility: {recap['eligibility']['by_reason']}")
            print(f"  cooldown: {health['reason'] or 'none'}; retry after: {health['retry_after'] or 'none'}")
            quarantine = recap["quarantine"]
            print(
                f"  quarantine: {quarantine['count']}/{quarantine['max_count']} files, "
                f"{quarantine['bytes']}/{quarantine['max_bytes']} bytes, "
                f"oldest {quarantine['oldest'] or 'none'}, admission paused {quarantine['admission_paused']}"
            )
            for retry in recap["guidance"]["retry"]:
                print(f"  retry: {retry['command']}")
            for cleanup in recap["guidance"]["cleanup"]:
                print(f"  cleanup recovery ({cleanup['session']}): {cleanup['command']}")
            if recap["guidance"]["quarantine"]:
                print(f"  quarantine recovery: {recap['guidance']['quarantine']}")
            print(f"  next: {recap['guidance']['recover'] or recap['guidance']['maintain']}")

    schema = status.get("schema", {"current": True})
    if not schema["current"]:
        print(f"Schema: out of date ({schema['reason']})")
        print("Run `ccrecall import` to apply migrations before checking ingestion/backfill status.")
        return

    ingestion = status["ingestion"]
    if ingestion is None:
        print("Ingestion: not checked (run `ccrecall status --check-ingestion`)")
    else:
        print(
            "Ingestion: "
            f"{ingestion['ok_sessions']}/{ingestion['sessions_checked']} sessions up to date; "
            f"pending tail {ingestion['pending_tail_sessions']} session(s) "
            f"/{ingestion['pending_tail_turns']} turn(s); "
            f"stale tail {ingestion['stale_tail_sessions']} session(s) "
            f"/{ingestion['stale_tail_turns']} turn(s); "
            f"gaps {ingestion['ingestion_gap_sessions']} session(s) "
            f"/{ingestion['ingestion_gap_turns']} turn(s); "
            f"missing source {ingestion['missing_source_sessions']} session(s)"
        )

    tool = status["tool_content"]
    total = tool["total_sessions"]
    done = tool["done_sessions"]
    pct = (done / total * 100) if total else 0.0
    print(f"Tool content: {done}/{total} sessions backfilled ({pct:.0f}%)")
    print(
        f"  remaining: {tool['pending_sessions']} sessions; "
        f"backfillable: {tool['pending_backfillable_sessions']}; "
        f"missing JSONL: {tool['pending_missing_jsonl_sessions']}"
    )

    embeddings = status["embeddings"]
    backfill = embeddings["backfill"]
    if backfill["available"]:
        branches = backfill["branches"]
        chunks = backfill["chunks"]
        total = branches["total"]
        embedded = branches["embedded"]
        pct = (embedded / total * 100) if total else 0.0
        print(f"Embeddings: {embedded}/{total} branches embedded ({pct:.0f}%)")
        print(f"  branch backfill: {branches['remaining']} remaining; {branches['errored']} errored")
        print(f"  chunk coverage: {chunks['done']}/{chunks['total']} chunks at current version")
    else:
        watermark = embeddings["watermark"]
        total = watermark["total_branches"]
        embedded = watermark["embedded_branches"]
        print(f"Embeddings: unavailable ({backfill['error']})")
        print(f"  watermark: {embedded}/{total} branches")


def run(
    *,
    db: Path = DEFAULT_DB_PATH,
    days: int | None = None,
    check_ingestion: bool = False,
    output_format: str = "markdown",
) -> None:
    try:
        status = collect_status(db=db, days=days, check_ingestion=check_ingestion)
    except FileNotFoundError as exc:
        if output_format == "json":
            print(json.dumps({"error": "database_not_found", "path": str(exc.filename or exc.args[0])}))
        else:
            print(f"ccrecall status: database not found: {exc.filename or exc.args[0]}", file=sys.stderr)
        raise SystemExit(1) from exc
    if output_format == "json":
        print(json.dumps(status, indent=2))
    else:
        print_status_report(status)
