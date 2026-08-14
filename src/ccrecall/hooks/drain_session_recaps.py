"""Detached, serialized Session Recap finalizer.

This module is deliberately the only hook-side module that imports the provider
boundary.  It operates exclusively on imported SQLite state after startup.
"""

import argparse
import contextlib
import json
import logging
import os
import re
import signal
import time
import uuid
from pathlib import Path

from whenever import Instant

from ccrecall.config import get_db_path, load_settings, remove_pid_file, setup_logging, try_acquire_pid_file

# The vec-aware boundary, not the lightweight one. This process already loads the
# whole embedding stack through sync_current, so it costs nothing here — and it is
# the only thing on the recap path that can migrate a database carrying chunk_vec.
# Opening through llm_summary_db instead leaves those users permanently unmigrated.
from ccrecall.db import get_connection
from ccrecall.hooks.durability import fsync_directory, journal_lock
from ccrecall.hooks.session_end import JOURNAL_PREFIX, JOURNAL_VERSION, journal_path
from ccrecall.hooks.sync_current import EXCLUDED_PROJECT, sync_session_for_finalization
from ccrecall.llm_summarizer import (
    STATUS_AUTH_REQUIRED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLAUDE_UNAVAILABLE,
    STATUS_CLEANUP_FAILED,
    STATUS_ERROR,
    STATUS_INVALID_OUTPUT,
    STATUS_PLATFORM_UNSUPPORTED,
    STATUS_RATE_LIMITED,
    STATUS_TIMEOUT,
    STATUS_UNSUPPORTED_CLI,
    invoke_claude,
    posix_process_groups_supported,
    remove_packet,
    write_packet,
)
from ccrecall.llm_summary_db import recap_schema_capability
from ccrecall.models import LOGGER_NAME
from ccrecall.process_cleanup import process_group_absent, process_start_identity
from ccrecall.recap_contract import ELIGIBILITY_POLICY_VERSION, RECAP_INPUT_CONTRACT_VERSION
from ccrecall.recap_eligibility import evaluate_branch
from ccrecall.recap_input import load_recap_input, refresh_recap_input
from ccrecall.recap_state import (
    acknowledge_cleanup,
    admit_provider,
    bind_attempt_packet,
    block_job_internal_error,
    cancel_attempt_before_launch,
    claim_job,
    claim_runtime,
    complete_attempt,
    defer_for_cooldown,
    finalize_run,
    heartbeat_job,
    heartbeat_runtime,
    mark_attempt_spawning,
    mark_current,
    mark_excluded,
    open_cooldown,
    quarantine_admission,
    quarantine_attempt,
    record_attempt_launch,
    recover_expired_attempt,
    release_runtime,
    requeue_changed_input_after_cleanup,
    reserve_attempt,
    reserve_attempt_for_run,
    upsert_job,
    verify_cleanup_removed,
)
from ccrecall.summary_enrichment import build_stored_enrichment_envelope, valid_current_enrichment

log = logging.getLogger(LOGGER_NAME)

PID_KEY = "ccrecall-drain-session-recaps"
JOURNAL_BATCH_SIZE = 32
EXIT_BUSY = 75
EXIT_CLEANUP_UNRESOLVED = 76
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _now() -> str:
    return Instant.now().format_iso()


def _override(controls: dict, key: str, default: object) -> object:
    """Take a run's one-off control when it set one, including a zero."""
    value = controls.get(key)
    return default if value is None else value


def _quarantine(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.replace(path.with_suffix(path.suffix + ".bad"))


def _valid_journal(value: object, marker: Path, settings: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("journal is not an object")
    session_uuid = value.get("session_uuid")
    requested_at = value.get("requested_at")
    if (
        value.get("version") != JOURNAL_VERSION
        or not isinstance(session_uuid, str)
        or _UUID_RE.fullmatch(session_uuid) is None
        or not isinstance(requested_at, str)
        or not requested_at
        or value.get("trigger") != "session_end"
        or marker != journal_path(get_db_path(settings), session_uuid)
    ):
        raise ValueError("malformed recap journal")
    return value


def replay_journal(settings: dict, *, limit: int = JOURNAL_BATCH_SIZE) -> int:
    """Replay committed intent before deleting its owner-only journal marker."""
    root = get_db_path(settings).parent
    replayed = 0
    # Bound successful replays, not markers examined. A marker whose session is
    # not imported yet is retained, so slicing the sorted scan would let a fixed
    # prefix of unresolved markers starve every later intent indefinitely.
    for marker in sorted(root.glob(f"{JOURNAL_PREFIX}*.json")):
        if replayed >= limit:
            break
        try:
            with journal_lock(marker):
                try:
                    value = json.loads(marker.read_text(encoding="utf-8"))
                    value = _valid_journal(value, marker, settings)
                except (ValueError, json.JSONDecodeError):
                    # The shared lock prevents a concurrent SessionEnd writer
                    # from replacing this malformed marker before its quarantine.
                    _quarantine(marker)
                    continue
                # A valid SessionEnd UUID is durable intent even when Stop has not
                # imported it yet. Final sync is silent and precedes this lookup.
                if sync_session_for_finalization(settings, value["session_uuid"]) == EXCLUDED_PROJECT:
                    # Void intent, not pending intent. Retaining it would leave a
                    # marker no run can ever resolve, re-synced on every drain.
                    marker.unlink()
                    fsync_directory(root)
                    continue
                with get_connection(settings) as conn:
                    if recap_schema_capability(conn) != "ready":
                        return replayed
                    row = conn.execute(
                        "SELECT s.id, b.recap_input_hash FROM sessions s JOIN branches b ON b.session_id = s.id "
                        "WHERE s.uuid = ? AND b.is_active = 1",
                        (value["session_uuid"],),
                    ).fetchone()
                    if row is None:
                        continue
                    upsert_job(conn, row[0], row[1], "session_end", value["requested_at"])
                marker.unlink()
                fsync_directory(root)
                replayed += 1
        except OSError:
            continue
    return replayed


def _terminate_exact_group(pid: int, group_id: int, started_at: str) -> bool:
    """Terminate only the persisted process-group leader, never a reused PID."""
    if process_start_identity(pid) != started_at:
        return False
    for signum in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(group_id, signum)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if process_group_absent(group_id):
                return True
            time.sleep(0.01)
    return process_group_absent(group_id)


def _recover_expired_claims(settings: dict, now: str) -> tuple[bool, set[int]]:
    """Durably prove stale packet/process cleanup before any replacement claim."""
    recovered: set[int] = set()
    all_proven = True
    with get_connection(settings) as conn:
        unreserved = conn.execute(
            "UPDATE session_recap_jobs SET state = 'pending', claim_token = claim_token + 1, "
            "lease_expires_at = NULL, updated_at = ?, reason = NULL "
            "WHERE state = 'claimed' AND active_attempt_id IS NULL AND lease_expires_at <= ? "
            "RETURNING session_id, claim_token",
            (now, now),
        ).fetchall()
        for session_id, token in unreserved:
            # A crash can occur after provider admission but before the attempt
            # reservation; release only the admission fenced by this old claim.
            conn.execute(
                "UPDATE session_recap_provider_health SET probe_active = 0, "
                "probe_session_id = NULL, probe_claim_token = NULL "
                "WHERE singleton = 1 AND probe_active = 1 AND probe_session_id = ? "
                "AND probe_claim_token < ?",
                (session_id, token),
            )
            recovered.add(session_id)
        rows = conn.execute(
            "SELECT a.id, a.job_session_id, a.packet_path, a.owner_pid, a.process_group_id, "
            "a.process_started_at, a.cleanup_state FROM session_recap_attempts a JOIN session_recap_jobs j "
            "ON j.session_id = a.job_session_id WHERE a.state IN ('reserved', 'running') "
            "AND j.state = 'claimed' AND j.lease_expires_at <= ?",
            (now,),
        ).fetchall()
        for attempt_id, session_id, packet, pid, group, started, cleanup_state in rows:
            proven = False
            if pid is None and group is None and started is None:
                # No owner identity. That means the attempt never reached the
                # spawn, or died in the window between spawning and recording
                # who it spawned. Only the first is provably clean: deleting a
                # packet says nothing about a process in its own session, and
                # requeuing on that would launch a second provider call
                # alongside one still running.
                proven = cleanup_state != "spawning" and (packet is None or remove_packet(Path(packet)))
            elif isinstance(pid, int) and isinstance(group, int) and isinstance(started, str):
                proven = True if process_group_absent(group) else _terminate_exact_group(pid, group, started)
                if proven and packet is not None:
                    proven = remove_packet(Path(packet))
            if proven:
                conn.execute(
                    "UPDATE session_recap_attempts SET cleanup_state = 'verified_removed', "
                    "finished_at = ? WHERE id = ?",
                    (now, attempt_id),
                )
                if recover_expired_attempt(conn, attempt_id, now, cleanup_proven=True):
                    recovered.add(session_id)
            else:
                all_proven = False
                conn.execute(
                    "UPDATE session_recap_attempts SET cleanup_state = 'uncertain', finished_at = ? WHERE id = ?",
                    (now, attempt_id),
                )
                quarantine_attempt(
                    conn,
                    attempt_id,
                    conn.execute(
                        "SELECT claim_token FROM session_recap_attempts WHERE id = ?", (attempt_id,)
                    ).fetchone()[0],
                    None,
                    "uncertain",
                    now,
                )
                recover_expired_attempt(conn, attempt_id, now, cleanup_proven=False)
    return all_proven, recovered


def _recover_cleanup_failed_attempts(settings: dict, now: str) -> tuple[bool, set[int]]:
    """Repair cleanup obligations and return only jobs requeued by changed input."""
    requeued: set[int] = set()
    all_proven = True
    with get_connection(settings) as conn:
        rows = conn.execute(
            "SELECT a.id, a.job_session_id, a.claim_token, a.packet_path, a.packet_nonce, a.owner_pid, "
            "a.process_group_id, a.process_started_at FROM session_recap_attempts a JOIN session_recap_jobs j "
            "ON j.session_id = a.job_session_id WHERE a.state = 'cleanup_failed' "
            "AND j.state = 'blocked' AND j.reason = 'cleanup_failed' AND j.active_attempt_id = a.id"
        ).fetchall()
        for attempt_id, session_id, token, packet, nonce, pid, group, started in rows:
            expected_packet = _packet_path(settings, session_id, attempt_id, nonce) if isinstance(nonce, str) else None
            owner_bound = isinstance(packet, str) and expected_packet == Path(packet)
            if pid is None and group is None and started is None:
                group_gone = True
            elif isinstance(pid, int) and isinstance(group, int) and isinstance(started, str):
                group_gone = process_group_absent(group) or _terminate_exact_group(pid, group, started)
            else:
                group_gone = False
            removed = group_gone and owner_bound and remove_packet(Path(packet))
            if removed and verify_cleanup_removed(conn, attempt_id, token, now):
                if requeue_changed_input_after_cleanup(conn, session_id, now):
                    requeued.add(session_id)
            else:
                all_proven = False
    return all_proven, requeued


def _next_pending(conn, now: str) -> tuple[int, int | None, dict] | None:
    """Prefer durable manual-run membership over ordinary queued work."""
    # Run membership owns an immutable input snapshot. A later import creates a
    # new job generation, so close the old member without reserving an attempt.
    conn.execute(
        "UPDATE session_recap_run_candidates AS c SET final_disposition = 'stale_input_changed' "
        "WHERE final_disposition IS NULL AND EXISTS ("
        "SELECT 1 FROM session_recap_runs r WHERE r.id = c.run_id AND r.state = 'running') "
        "AND EXISTS (SELECT 1 FROM session_recap_jobs j WHERE j.session_id = c.session_id "
        "AND j.requested_input_hash IS NOT c.input_hash)"
    )
    row = conn.execute(
        "SELECT c.session_id, c.run_id, r.selector_json FROM session_recap_run_candidates c "
        "JOIN session_recap_runs r ON r.id = c.run_id JOIN session_recap_jobs j ON j.session_id = c.session_id "
        "WHERE r.state = 'running' AND c.final_disposition IS NULL AND j.state = 'pending' "
        "AND j.requested_input_hash IS c.input_hash "
        "AND (j.next_eligible_at IS NULL OR j.next_eligible_at <= ?) AND (r.attempt_limit IS NULL "
        "OR (SELECT COUNT(*) FROM session_recap_run_candidates rc WHERE rc.run_id = r.id "
        "AND rc.started_attempt_id IS NOT NULL) < r.attempt_limit) ORDER BY r.id, j.requested_at LIMIT 1",
        (now,),
    ).fetchone()
    if row:
        return row[0], row[1], json.loads(row[2] or "{}")
    row = conn.execute(
        "SELECT session_id FROM session_recap_jobs WHERE state = 'pending' "
        "AND (next_eligible_at IS NULL OR next_eligible_at <= ?) AND NOT EXISTS ("
        "SELECT 1 FROM session_recap_run_candidates c JOIN session_recap_runs r ON r.id = c.run_id "
        "WHERE c.session_id = session_recap_jobs.session_id AND r.state = 'running' "
        "AND c.final_disposition IS NULL AND session_recap_jobs.requested_input_hash IS c.input_hash) "
        "ORDER BY requested_at LIMIT 1",
        (now,),
    ).fetchone()
    return (row[0], None, {}) if row else None


def _packet_path(settings: dict, session_id: int, attempt_id: int, nonce: str) -> Path:
    return get_db_path(settings).parent / "recap-packets" / str(session_id) / f"{attempt_id}-{nonce}.json"


def _materialize(
    settings: dict,
    branch_id: int,
    session_id: int,
    token: int,
    attempt_id: int,
    input_hash: str,
    response: dict,
    model: str,
) -> bool:
    """Write only if the captured branch and claim generation remain current."""
    now = _now()
    envelope = build_stored_enrichment_envelope(
        response, model=model, generated_at=now, attempt_id=attempt_id, recap_input_hash=input_hash
    )
    with get_connection(settings) as conn:
        try:
            current = load_recap_input(conn.cursor(), branch_id)
        except ValueError:
            return False
        if current.input_hash != input_hash:
            return False
        changed = conn.execute(
            """UPDATE branches SET summary_enrichment_json = ?, summary_enrichment_version = 2,
                   summary_enrichment_status = 'ok', summary_enrichment_error = NULL,
                   summary_enrichment_updated_at = ?, summary_enrichment_input_hash = ?,
                   summary_enrichment_input_contract_version = ?, summary_enrichment_policy_version = ?
                WHERE id = ? AND is_active = 1 AND recap_input_hash IS ? AND EXISTS (
                  SELECT 1 FROM session_recap_jobs j WHERE j.session_id = ? AND j.state = 'claimed'
                    AND j.claim_token = ? AND j.active_attempt_id = ? AND j.requested_input_hash IS ?
                )""",
            (
                json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
                now,
                input_hash,
                RECAP_INPUT_CONTRACT_VERSION,
                ELIGIBILITY_POLICY_VERSION,
                branch_id,
                input_hash,
                session_id,
                token,
                attempt_id,
                input_hash,
            ),
        ).rowcount
        return bool(changed)


def _process_job(
    settings: dict,
    session_id: int,
    runtime_token: int,
    *,
    cleanup_proven: bool = False,
    run_id: int | None = None,
    controls: dict | None = None,
) -> bool:
    """Capture, invoke, and materialize one fenced job. Returns false after global abort."""
    now = _now()
    with get_connection(settings) as conn:
        token = claim_job(conn, session_id, now, settings["recap_job_lease_seconds"], cleanup_proven=cleanup_proven)
        if token is None:
            return True
        heartbeat_runtime(conn, runtime_token, now, settings["recap_runtime_lease_seconds"])
        row = conn.execute("SELECT uuid FROM sessions WHERE id = ?", (session_id,)).fetchone()
    # This intentionally happens after the fenced job claim and before every
    # recap input/eligibility read. It has no hook stdout and closes DB first.
    if row is not None and sync_session_for_finalization(settings, row[0]) == EXCLUDED_PROJECT:
        # The project was added to exclude_projects after these sessions were
        # imported, so the branch is still sitting in the database. Journal replay
        # already treats this sentinel as void intent; the claimed path must too,
        # or the contents of a project the user asked us to leave alone get read
        # and shipped to the provider. Stop before materializing anything.
        with get_connection(settings) as exclusion_conn:
            lineage_row = exclusion_conn.execute(
                "SELECT retry_lineage FROM session_recap_jobs WHERE session_id = ?", (session_id,)
            ).fetchone()
            branch_row = exclusion_conn.execute(
                "SELECT recap_input_hash FROM branches WHERE session_id = ? AND is_active = 1", (session_id,)
            ).fetchone()
            # This branch skips the refresh_recap_input reconciliation the normal
            # path runs, so its fence can miss on a job whose requested hash never
            # matched the stored one. A missed fence leaves the job claimed until
            # its lease expires and re-claimed every pass after that — silent
            # livelock. Nothing else observes this path, so say so out loud.
            if not mark_excluded(
                exclusion_conn,
                session_id,
                token,
                branch_row[0] if branch_row else None,
                lineage_row[0] if lineage_row else 0,
                "excluded_project",
                now,
            ):
                log.warning("Could not mark session %s excluded; its claim will expire and be retried", session_id)
        return True
    with get_connection(settings) as conn:
        # The capture snapshot reads the branch identity and writes it back, so it
        # takes the write lock up front the way record_intent does. Without that,
        # an import committing between the read and the write would have its newer
        # hash overwritten by this stale one.
        conn.execute("BEGIN IMMEDIATE")
        branch = conn.execute(
            "SELECT id, recap_input_hash, summary_enrichment_json, summary_enrichment_version, "
            "summary_enrichment_status, summary_enrichment_input_hash, "
            "summary_enrichment_input_contract_version, summary_enrichment_policy_version "
            "FROM branches WHERE session_id = ? AND is_active = 1",
            (session_id,),
        ).fetchone()
        lineage_row = conn.execute(
            "SELECT retry_lineage FROM session_recap_jobs WHERE session_id = ?", (session_id,)
        ).fetchone()
        lineage = lineage_row[0] if lineage_row else 0
        if branch is None:
            mark_excluded(conn, session_id, token, None, lineage, "missing_active_branch", now)
            return True
        branch_id, input_hash = branch[0], branch[1]
        # Persist as well as read: a DB upgraded from v7 has recap_input_hash as
        # NULL with no backfill, so a branch the import fast-skips would keep
        # recomputing the mismatch below and never reach a provider.
        recap = refresh_recap_input(conn.cursor(), branch_id)
        if recap.input_hash != input_hash:
            # The capture snapshot is authoritative even if a concurrent import
            # updated the branch immediately before this claim.
            upsert_job(conn, session_id, recap.input_hash, "session_end", now)
            return True
        # Release the write lock before eligibility, admission, and reservation
        # work; each of those fences itself on the claim token it already holds.
        conn.commit()
        try:
            stored_enrichment = json.loads(branch[2]) if branch[2] else None
        except json.JSONDecodeError:
            # A corrupt blob cannot describe current content, so treat it as
            # absent and regenerate. Raising here would abort the whole drain
            # and take every job queued behind this one with it, on every run.
            stored_enrichment = None
        if valid_current_enrichment(
            stored_enrichment,
            status=branch[4],
            current_input_hash=input_hash,
            materialized_input_hash=branch[5],
            materialized_input_contract_version=branch[6],
            materialized_policy_version=branch[7],
            stored_enrichment_version=branch[3],
        ):
            mark_current(conn, session_id, token, input_hash, lineage, now)
            return True
        decision = evaluate_branch(conn.cursor(), branch_id)
        if not decision.eligible:
            mark_excluded(conn, session_id, token, input_hash, lineage, decision.reason, now)
            return True
        if not posix_process_groups_supported():
            conn.execute(
                "UPDATE session_recap_jobs SET state = 'blocked', reason = 'platform_unsupported', "
                "lease_expires_at = NULL "
                "WHERE session_id = ? AND claim_token = ? AND state = 'claimed'",
                (session_id, token),
            )
            return True
        provider_token = admit_provider(conn, session_id, token, now)
        if provider_token is None:
            defer_for_cooldown(conn, session_id, token, now)
            return True
        nonce = uuid.uuid4().hex
        try:
            controls = controls or {}
            effective_settings = {
                **settings,
                # Explicit None, not falsiness: a run that asked for a zero budget
                # or timeout means it, and `or` would silently restore the default.
                "llm_summary_model": _override(controls, "model", settings["llm_summary_model"]),
                "llm_summary_max_budget_usd": _override(
                    controls, "max_budget_usd", settings["llm_summary_max_budget_usd"]
                ),
                "llm_summary_timeout_seconds": _override(
                    controls, "timeout_seconds", settings["llm_summary_timeout_seconds"]
                ),
            }
            if run_id is not None:
                attempt_id = reserve_attempt_for_run(
                    conn,
                    run_id,
                    session_id,
                    token,
                    input_hash,
                    "manual",
                    now,
                    provider_token=provider_token,
                    model=effective_settings["llm_summary_model"],
                    max_budget_usd=effective_settings["llm_summary_max_budget_usd"],
                    timeout_seconds=effective_settings["llm_summary_timeout_seconds"],
                )
            else:
                attempt_id = reserve_attempt(
                    conn,
                    session_id,
                    token,
                    input_hash,
                    "session_end",
                    now,
                    provider_token=provider_token,
                    model=effective_settings["llm_summary_model"],
                    max_budget_usd=effective_settings["llm_summary_max_budget_usd"],
                    timeout_seconds=effective_settings["llm_summary_timeout_seconds"],
                )
            if attempt_id is None:
                return True
        except RuntimeError:
            return True
        packet_path = _packet_path(settings, session_id, attempt_id, nonce)
        if not bind_attempt_packet(conn, attempt_id, token, str(packet_path), nonce):
            return True

    # The packet is a copied DB snapshot and every provider callback opens its
    # own brief transaction. No SQLite connection spans filesystem/provider work.
    def persist_cleanup(state: str, metadata: dict) -> None:
        with get_connection(settings) as callback_conn:
            acknowledge_cleanup(callback_conn, attempt_id, token, state, _now())
            if state == "verified_removed":
                cancel_attempt_before_launch(callback_conn, attempt_id, token, input_hash, lineage, _now())
            else:
                quarantine_attempt(callback_conn, attempt_id, token, metadata.get("byte_size"), state, _now())

    def persist_launch(pid: int, group: int, started: str) -> bool:
        with get_connection(settings) as callback_conn:
            return record_attempt_launch(callback_conn, attempt_id, token, pid, group, started, _now())

    def persist_spawn_intent() -> bool:
        with get_connection(settings) as callback_conn:
            return mark_attempt_spawning(callback_conn, attempt_id, token)

    def persist_prelaunch_cleanup(state: str, metadata: dict) -> None:
        """Keep a cleaned reservation fenced until its provider cooldown opens."""
        with get_connection(settings) as callback_conn:
            acknowledged = acknowledge_cleanup(callback_conn, attempt_id, token, state, _now())
            if state != "verified_removed":
                if acknowledged:
                    quarantine_attempt(callback_conn, attempt_id, token, metadata.get("byte_size"), state, _now())
                complete_attempt(
                    callback_conn, attempt_id, token, "cleanup_failed", _now(), diagnostic="provider_error"
                )

    def admit_launch() -> bool:
        with get_connection(settings) as callback_conn:
            allowed, _count, _bytes, _oldest = quarantine_admission(
                callback_conn,
                max_count=settings["recap_quarantine_max_count"],
                max_bytes=settings["recap_quarantine_max_bytes"],
            )
            return allowed and heartbeat_job(
                callback_conn, session_id, token, _now(), settings["recap_job_lease_seconds"]
            )

    if not write_packet(
        packet_path,
        recap.packet,
        admit_launch=admit_launch,
        persist_write_failure=persist_cleanup,
        packet_nonce=nonce,
    ):
        # Cancelling before launch returns the job to immediately eligible
        # pending, so continuing would reselect this same job at once and spin,
        # writing a cancelled_before_launch row every pass while holding the
        # drainer guard. Every reason this fails is a standing condition — a
        # full quarantine, an unusable packet directory, a full disk — so none
        # of them is fixed by trying the next job. Stop and let the next run,
        # after recovery, find a changed world.
        return False
    result = invoke_claude(
        packet_path,
        effective_settings,
        persist_launch=persist_launch,
        persist_cleanup=persist_prelaunch_cleanup,
        persist_spawn_intent=persist_spawn_intent,
        admit_launch=admit_launch,
        packet_nonce=nonce,
    )
    if result.status == "ok" and result.response_body is not None:
        outcome = (
            "succeeded"
            if _materialize(
                settings,
                branch_id,
                session_id,
                token,
                attempt_id,
                input_hash,
                result.response_body,
                effective_settings["llm_summary_model"],
            )
            else "stale_discarded"
        )
        with get_connection(settings) as conn:
            heartbeat_runtime(conn, runtime_token, _now(), settings["recap_runtime_lease_seconds"])
            complete_attempt(conn, attempt_id, token, outcome, _now())
    elif result.status in {
        STATUS_RATE_LIMITED,
        STATUS_AUTH_REQUIRED,
        STATUS_CLAUDE_UNAVAILABLE,
        STATUS_UNSUPPORTED_CLI,
        STATUS_ERROR,
    }:
        with get_connection(settings) as conn:
            heartbeat_runtime(conn, runtime_token, _now(), settings["recap_runtime_lease_seconds"])
            open_cooldown(
                conn,
                attempt_id,
                token,
                provider_token,
                result.status,
                _now(),
                30,
                settings["recap_cooldown_max_seconds"],
                diagnostic="provider_error",
            )
        return False
    else:
        outcomes = {
            STATUS_BUDGET_EXCEEDED: "budget_exceeded",
            STATUS_TIMEOUT: "timeout",
            STATUS_INVALID_OUTPUT: "unusable_output",
            STATUS_CLEANUP_FAILED: "cleanup_failed",
            STATUS_PLATFORM_UNSUPPORTED: "cleanup_failed",
        }
        with get_connection(settings) as conn:
            heartbeat_runtime(conn, runtime_token, _now(), settings["recap_runtime_lease_seconds"])
            complete_attempt(
                conn,
                attempt_id,
                token,
                outcomes.get(result.status, "cleanup_failed"),
                _now(),
                # Provider diagnostics may contain transcript or provider text;
                # persist only the lifecycle's content-free outcome code.
                diagnostic=outcomes.get(result.status, "cleanup_failed"),
                retry_delay_seconds=settings["recap_timeout_retry_seconds"],
            )
    # An unproven process group is the one outcome that must not be followed by
    # another provider call: complete_attempt has already released admission and
    # a single quarantine row sits under the ceiling, so the next job would
    # start a second Claude beside a group that may still be alive.
    return result.status != STATUS_CLEANUP_FAILED


def run(*, recover_only: bool = False) -> int:
    """Replay durable intent, reconcile cleanup, and optionally skip provider work."""
    settings = load_settings()
    logger = setup_logging(settings, process_name="drain-session-recaps")
    replay_journal(settings)
    if not try_acquire_pid_file(PID_KEY):
        return EXIT_BUSY if recover_only else 0
    try:
        with get_connection(settings) as conn:
            if recap_schema_capability(conn) != "ready":
                return 0
        cleanup_proven, recovered = _recover_expired_claims(settings, _now())
        cleanup_failed_proven, cleanup_failed_requeued = _recover_cleanup_failed_attempts(settings, _now())
        cleanup_proven = cleanup_proven and cleanup_failed_proven
        recovered |= cleanup_failed_requeued
        if not cleanup_proven:
            return EXIT_CLEANUP_UNRESOLVED if recover_only else 0
        if recover_only:
            return 0
        with get_connection(settings) as conn:
            runtime_token = claim_runtime(
                conn,
                os.getpid(),
                _now(),
                settings["recap_runtime_lease_seconds"],
                cleanup_proven=cleanup_proven,
            )
        if runtime_token is None:
            return 0
        with get_connection(settings) as conn:
            observed_run_ids = {
                row[0] for row in conn.execute("SELECT id FROM session_recap_runs WHERE state = 'running'")
            }
        aborted_run_id = None
        try:
            while True:
                with get_connection(settings) as conn:
                    next_job = _next_pending(conn, _now())
                if next_job is None:
                    break
                session_id, run_id, controls = next_job
                if run_id is not None:
                    observed_run_ids.add(run_id)
                try:
                    keep_draining = _process_job(
                        settings,
                        session_id,
                        runtime_token,
                        cleanup_proven=session_id in recovered,
                        run_id=run_id,
                        controls=controls,
                    )
                except Exception:
                    # One job's failure is not the queue's. Without this the
                    # exception escapes to the handler below, and _next_pending's
                    # oldest-first ordering hands the same job back on the next
                    # run, so a single bad row stalls recaps for every session.
                    logger.exception("Recap job for session %s failed; blocking it and continuing", session_id)
                    with get_connection(settings) as conn:
                        block_job_internal_error(conn, session_id, _now())
                    continue
                if not keep_draining:
                    aborted_run_id = run_id
                    break
        finally:
            with get_connection(settings) as conn:
                release_runtime(conn, runtime_token)
        with get_connection(settings) as conn:
            for run_id in observed_run_ids:
                finalize_run(conn, run_id, _now(), global_abort=run_id == aborted_run_id)
        return 0
    except Exception:
        logger.exception("Session recap drainer failed")
        return 1
    finally:
        remove_pid_file(PID_KEY)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ccrecall-drain-session-recaps", allow_abbrev=False)
    parser.add_argument(
        "--recover-only",
        action="store_true",
        help="Internal: replay intent and reconcile cleanup without provider work; nonzero when busy or unresolved.",
    )
    args = parser.parse_args(argv)
    raise SystemExit(run(recover_only=args.recover_only))


if __name__ == "__main__":
    main()
