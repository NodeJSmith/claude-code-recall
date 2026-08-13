"""Manual, queue-backed Session Recap selection and lifecycle operations."""

import argparse
import json
import logging
import shlex
import subprocess

from whenever import Instant

from ccrecall.config import load_settings, remove_pid_file, try_acquire_pid_file
from ccrecall.llm_summary_db import get_connection, recap_schema_capability
from ccrecall.process_cleanup import posix_process_groups_supported
from ccrecall.recap_eligibility import evaluate_branch
from ccrecall.recap_input import refresh_recap_input
from ccrecall.recap_state import create_run, finalize_run, run_accounting, targeted_retry, upsert_job

PID_KEY = "ccrecall-backfill-llm-summaries"
EXIT_OK = 0


def run(
    *,
    days: int | None = None,
    limit: int | None = None,
    session: str | None = None,
    current_session: bool = False,
    retry_failures: bool = False,
    model: str | None = None,
    max_budget_usd: float | None = None,
    timeout_seconds: int | None = None,
    verbose: bool = False,
    json_mode: bool = False,
) -> int:
    """Commit a durable manual snapshot, then ask the shared drainer to process it."""
    del current_session, verbose
    if not try_acquire_pid_file(PID_KEY):
        return EXIT_OK
    try:
        settings = load_settings()
        now = Instant.now().format_iso()
        platform_supported = posix_process_groups_supported()
        rerun_args = ["ccrecall"]
        if json_mode:
            rerun_args.append("--json")
        rerun_args.extend(["backfill", "llm-summaries"])
        if days is not None:
            rerun_args.extend(["--days", str(days)])
        if limit is not None:
            rerun_args.extend(["--limit", str(limit)])
        if session is not None:
            rerun_args.extend(["--session", session])
        if retry_failures:
            rerun_args.append("--retry-failures")
        if model is not None:
            rerun_args.extend(["--model", model])
        if max_budget_usd is not None:
            rerun_args.extend(["--max-budget-usd", str(max_budget_usd)])
        if timeout_seconds is not None:
            rerun_args.extend(["--timeout-seconds", str(timeout_seconds)])
        rerun_command = shlex.join(rerun_args)
        if platform_supported:
            # Reconcile cleanup ownership before explicit retries examine blocked jobs.
            recovered = subprocess.run(
                ["ccrecall-drain-session-recaps", "--recover-only"],  # noqa: S607 - installed internal entry point
                check=False,
            )
            if recovered.returncode != 0:
                reason = (
                    "recovery_busy"
                    if recovered.returncode == 75
                    else "cleanup_unresolved"
                    if recovered.returncode == 76
                    else "recovery_failed"
                )
                messages = {
                    "recovery_busy": "another recap recovery is active. Wait for it to finish",
                    "cleanup_unresolved": "prior provider cleanup could not be verified. Run ccrecall recap recover",
                    "recovery_failed": "recovery failed. Run ccrecall recap recover",
                }
                if json_mode:
                    print(
                        json.dumps(
                            {
                                "recovery": {
                                    "command": "ccrecall recap recover",
                                    "reason": reason,
                                    "retry_command": rerun_command,
                                    "state": "retry_deferred",
                                },
                                "session_recap_run": None,
                            },
                            sort_keys=True,
                        )
                    )
                else:
                    print(f"Session Recap retry deferred: {messages[reason]}, then rerun: {rerun_command}")
                return EXIT_OK
        selectors = {
            "days": days,
            "session": session,
            "retry_failures": retry_failures,
            "model": model,
            "max_budget_usd": max_budget_usd,
            "timeout_seconds": timeout_seconds,
        }
        with get_connection(settings) as conn:
            if recap_schema_capability(conn) != "ready":
                logging.getLogger(__name__).warning("Session Recap schema is unavailable; run ccrecall import")
                return EXIT_OK
            sql = (
                "SELECT s.id, s.uuid, b.id, b.recap_input_hash FROM sessions s "
                "JOIN branches b ON b.session_id = s.id WHERE b.is_active = 1"
            )
            params: list[object] = []
            if session:
                sql += " AND s.uuid LIKE ?"
                params.append(f"{session}%")
            if days:
                sql += " AND b.ended_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ? || ' days')"
                params.append(f"-{days}")
            sql += " ORDER BY b.ended_at, s.id"
            candidates: list[tuple[int, str | None, str]] = []
            for session_id, _uuid, branch_id, stored_hash in conn.execute(sql, params):
                decision = evaluate_branch(conn.cursor(), branch_id)
                if not decision.eligible:
                    # Never drained, so its hash is accounting only — not worth
                    # rebuilding the whole projection for.
                    candidates.append((session_id, stored_hash, "excluded"))
                    continue
                try:
                    # Snapshot the identity the drainer will compute, not a column an
                    # upgraded-from-v7 DB never backfilled. A NULL snapshot goes stale
                    # on the first claim, dropping the job out of this run and past
                    # both --limit and the run's model, budget, and timeout overrides.
                    input_hash = refresh_recap_input(conn.cursor(), branch_id).input_hash
                except ValueError:
                    # A concurrent reimport replaced this branch between the scan
                    # and here. Skip the one stale candidate, not the whole run.
                    continue
                existing = conn.execute(
                    "SELECT state, reason FROM session_recap_jobs WHERE session_id = ?", (session_id,)
                ).fetchone()
                if retry_failures and existing is not None and existing[0] == "blocked":
                    targeted_retry(conn, session_id, now)
                upsert_job(conn, session_id, input_hash, "manual", now)
                if not platform_supported:
                    conn.execute(
                        "UPDATE session_recap_jobs SET state = 'blocked', reason = 'platform_unsupported', "
                        "lease_expires_at = NULL WHERE session_id = ?",
                        (session_id,),
                    )
                state, reason = conn.execute(
                    "SELECT state, reason FROM session_recap_jobs WHERE session_id = ?", (session_id,)
                ).fetchone()
                candidates.append((session_id, input_hash, "pending" if state == "pending" else (reason or state)))
            run_id = create_run(
                conn,
                "manual",
                json.dumps(selectors, sort_keys=True, separators=(",", ":")),
                candidates,
                now,
                attempt_limit=limit,
            )
            if not platform_supported:
                finalize_run(conn, run_id, now)
        if platform_supported:
            # Intent is committed before the detached shared worker can observe it.
            subprocess.run(["ccrecall-drain-session-recaps"], check=False)  # noqa: S607 - installed internal entry point
        with get_connection(settings) as conn:
            accounting = run_accounting(conn, run_id)
        if json_mode:
            print(
                json.dumps(
                    {
                        "session_recap_run": accounting,
                        "provider_work_disabled": not platform_supported,
                        "provider_work_reason": None if platform_supported else "platform_unsupported",
                    },
                    sort_keys=True,
                )
            )
        else:
            print(
                f"Session Recap run {run_id}: population {accounting['population']}; "
                f"started {accounting['started_attempts']}; state {accounting['state']}"
            )
            if not platform_supported:
                print("  provider work disabled: platform_unsupported")
            print(f"  initial: {accounting['initial_dispositions']}")
            print(f"  final: {accounting['final_dispositions']}")
            print(f"  outcomes: {accounting['attempt_outcomes']}")
        logging.getLogger(__name__).info("Session Recap run %s created with %s candidates", run_id, len(candidates))
        return EXIT_OK
    finally:
        remove_pid_file(PID_KEY)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccrecall-llm-summaries", allow_abbrev=False)
    parser.add_argument("--days", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--session")
    parser.add_argument("--current-session", action="store_true")
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--max-budget-usd", type=float)
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error("--days must be >= 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.current_session and args.session is None:
        parser.error("--current-session requires --session")
    if args.max_budget_usd is not None and args.max_budget_usd <= 0:
        parser.error("--max-budget-usd must be > 0")
    if args.timeout_seconds is not None and args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be >= 1")
    return run(
        days=args.days,
        limit=args.limit,
        session=args.session,
        current_session=args.current_session,
        retry_failures=args.retry_failures,
        model=args.model,
        max_budget_usd=args.max_budget_usd,
        timeout_seconds=args.timeout_seconds,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    raise SystemExit(main())
