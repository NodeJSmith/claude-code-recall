"""Durable, provider-free state transitions for Session Recaps."""

import sqlite3

from ccrecall.recap_contract import ELIGIBILITY_POLICY_VERSION, RECAP_CONTRACT_VERSION, RECAP_INPUT_CONTRACT_VERSION

DIAGNOSTIC_LIMIT = 500
TERMINAL_ATTEMPTS = frozenset(
    {
        "succeeded",
        "stale_discarded",
        "timeout",
        "budget_exceeded",
        "unusable_output",
        "global_abort",
        "cleanup_failed",
        "abandoned",
        "cancelled_before_launch",
    }
)
DIAGNOSTICS = frozenset(
    {
        "rate_limited",
        "service_unavailable",
        "authentication_failed",
        "provider_error",
        "timeout",
        "budget_exceeded",
        "unusable_output",
        "cleanup_failed",
    }
)
CONTENT_DEPENDENT_BLOCK_REASONS = ("budget_exceeded", "unusable_output", "timeout_exhausted")


def _deadline(_now: str, _seconds: int) -> str:
    return "strftime('%Y-%m-%dT%H:%M:%fZ', julianday(?) + ? / 86400.0)"


def _diagnostic(code: str | None) -> str | None:
    if code is None:
        return None
    if code not in DIAGNOSTICS:
        raise ValueError(f"unsupported diagnostic code: {code}")
    return code[:DIAGNOSTIC_LIMIT]


def upsert_job(
    conn: sqlite3.Connection,
    session_id: int,
    input_hash: str | None,
    trigger: str,
    now: str,
) -> None:
    """Coalesce to the newest request and fence any prior claimant.

    A changed input cannot clear ``active_attempt_id``: that would assert cleanup
    for a process we have not proved is gone. Recovery must supply that proof.
    """
    conn.execute(
        """
        INSERT INTO session_recap_jobs (
          session_id, requested_input_hash, trigger, state, requested_at, updated_at
        ) VALUES (?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
          requested_input_hash = excluded.requested_input_hash,
          trigger = excluded.trigger,
          requested_at = excluded.requested_at,
          updated_at = excluded.updated_at,
           retry_lineage = session_recap_jobs.retry_lineage +
             (excluded.requested_input_hash IS NOT session_recap_jobs.requested_input_hash
              AND NOT (session_recap_jobs.state = 'blocked'
                       AND session_recap_jobs.reason = 'cleanup_failed'
                       AND session_recap_jobs.active_attempt_id IS NOT NULL)),
           claim_token = session_recap_jobs.claim_token +
             (excluded.requested_input_hash IS NOT session_recap_jobs.requested_input_hash
              AND NOT (session_recap_jobs.state = 'blocked'
                       AND session_recap_jobs.reason = 'cleanup_failed'
                       AND session_recap_jobs.active_attempt_id IS NOT NULL)),
           state = CASE
             WHEN excluded.requested_input_hash IS NOT session_recap_jobs.requested_input_hash
              AND NOT (session_recap_jobs.state = 'blocked'
                       AND session_recap_jobs.reason = 'cleanup_failed'
                       AND session_recap_jobs.active_attempt_id IS NOT NULL)
             THEN 'pending' ELSE session_recap_jobs.state END,
           reason = CASE
             WHEN excluded.requested_input_hash IS NOT session_recap_jobs.requested_input_hash
              AND NOT (session_recap_jobs.state = 'blocked'
                       AND session_recap_jobs.reason = 'cleanup_failed'
                       AND session_recap_jobs.active_attempt_id IS NOT NULL)
             THEN NULL ELSE session_recap_jobs.reason END,
           next_eligible_at = CASE
             WHEN excluded.requested_input_hash IS NOT session_recap_jobs.requested_input_hash
              AND NOT (session_recap_jobs.state = 'blocked'
                       AND session_recap_jobs.reason = 'cleanup_failed'
                       AND session_recap_jobs.active_attempt_id IS NOT NULL)
             THEN NULL ELSE session_recap_jobs.next_eligible_at END,
           lease_expires_at = CASE
             WHEN excluded.requested_input_hash IS NOT session_recap_jobs.requested_input_hash
              AND NOT (session_recap_jobs.state = 'blocked'
                       AND session_recap_jobs.reason = 'cleanup_failed'
                       AND session_recap_jobs.active_attempt_id IS NOT NULL)
             THEN NULL ELSE session_recap_jobs.lease_expires_at END
        """,
        (session_id, input_hash, trigger, now, now),
    )


def recap_state_changed_input(
    conn: sqlite3.Connection,
    session_id: int,
    input_hash: str,
    now: str | None,
) -> bool:
    """Fence changed input and reset eligible terminal or pre-attempt claim state."""
    resettable = """state IN ('current', 'excluded')
        OR (state = 'pending' AND reason = 'timeout_retry')
        OR (state = 'blocked' AND reason IN ('budget_exceeded', 'unusable_output', 'timeout_exhausted'))
        OR (state = 'claimed' AND active_attempt_id IS NULL)"""
    return bool(
        conn.execute(
            f"""UPDATE session_recap_jobs SET requested_input_hash = ?, updated_at = COALESCE(?, CURRENT_TIMESTAMP),
                   retry_lineage = retry_lineage + 1, claim_token = claim_token + 1,
                   state = CASE WHEN {resettable} THEN 'pending' ELSE state END,
                   reason = CASE WHEN {resettable} THEN NULL ELSE reason END,
                   lease_expires_at = CASE WHEN {resettable} THEN NULL ELSE lease_expires_at END,
                   next_eligible_at = CASE WHEN {resettable} THEN NULL ELSE next_eligible_at END
               WHERE session_id = ? AND requested_input_hash IS NOT ?""",
            (input_hash, now, session_id, input_hash),
        ).rowcount
    )


def claim_runtime(
    conn: sqlite3.Connection,
    owner_pid: int,
    now: str,
    lease_seconds: int,
    *,
    cleanup_proven: bool = False,
) -> int | None:
    conn.execute("INSERT OR IGNORE INTO session_recap_runtime (singleton) VALUES (1)")
    row = conn.execute(
        f"""UPDATE session_recap_runtime SET claim_token = claim_token + 1, owner_pid = ?,
            lease_expires_at = {_deadline(now, lease_seconds)}, heartbeat_at = ?
            WHERE singleton = 1 AND (owner_pid IS NULL OR (lease_expires_at <= ? AND ?))
            RETURNING claim_token""",
        (owner_pid, now, lease_seconds, now, now, int(cleanup_proven)),
    ).fetchone()
    return row[0] if row else None


def heartbeat_runtime(conn: sqlite3.Connection, token: int, now: str, lease_seconds: int) -> bool:
    return bool(
        conn.execute(
            f"UPDATE session_recap_runtime SET heartbeat_at = ?, "
            f"lease_expires_at = {_deadline(now, lease_seconds)} "
            "WHERE singleton = 1 AND claim_token = ? AND owner_pid IS NOT NULL",
            (now, now, lease_seconds, token),
        ).rowcount
    )


def release_runtime(conn: sqlite3.Connection, token: int) -> bool:
    return bool(
        conn.execute(
            "UPDATE session_recap_runtime SET owner_pid = NULL, lease_expires_at = NULL "
            "WHERE singleton = 1 AND claim_token = ?",
            (token,),
        ).rowcount
    )


def claim_job(
    conn: sqlite3.Connection,
    session_id: int,
    now: str,
    lease_seconds: int,
    *,
    cleanup_proven: bool = False,
) -> int | None:
    row = conn.execute(
        f"""UPDATE session_recap_jobs SET state = 'claimed', claim_token = claim_token + 1,
            lease_expires_at = {_deadline(now, lease_seconds)}, updated_at = ?, reason = NULL
            WHERE session_id = ? AND (active_attempt_id IS NULL OR ?)
              AND ((state = 'pending' AND (next_eligible_at IS NULL OR next_eligible_at <= ?))
                OR (state = 'claimed' AND lease_expires_at <= ? AND ?))
            RETURNING claim_token""",
        (now, lease_seconds, now, session_id, int(cleanup_proven), now, now, int(cleanup_proven)),
    ).fetchone()
    if row is None:
        return None
    token = row[0]
    if cleanup_proven:
        conn.execute(
            "UPDATE session_recap_attempts SET state = 'abandoned', finished_at = ? "
            "WHERE job_session_id = ? AND claim_token < ? AND state IN ('reserved', 'running')",
            (now, session_id, token),
        )
        conn.execute(
            "UPDATE session_recap_provider_health SET probe_active = 0, "
            "probe_session_id = NULL, probe_claim_token = NULL "
            "WHERE singleton = 1 AND probe_active = 1 AND probe_session_id = ? "
            "AND probe_claim_token < ?",
            (session_id, token),
        )
        conn.execute(
            "UPDATE session_recap_jobs SET active_attempt_id = NULL WHERE session_id = ? AND claim_token = ?",
            (session_id, token),
        )
    return token


def heartbeat_job(
    conn: sqlite3.Connection,
    session_id: int,
    token: int,
    now: str,
    lease_seconds: int,
) -> bool:
    return bool(
        conn.execute(
            f"UPDATE session_recap_jobs SET lease_expires_at = {_deadline(now, lease_seconds)}, "
            "updated_at = ? WHERE session_id = ? AND state = 'claimed' AND claim_token = ?",
            (now, now, lease_seconds, session_id, token),
        ).rowcount
    )


def acknowledge_cleanup(
    conn: sqlite3.Connection,
    attempt_id: int,
    token: int,
    cleanup_state: str,
    now: str,
) -> bool:
    """Persist cleanup only for the current, generation-matching live attempt."""
    return bool(
        conn.execute(
            "UPDATE session_recap_attempts SET cleanup_state = ?, "
            "finished_at = COALESCE(finished_at, ?) WHERE id = ? AND claim_token = ? "
            "AND state IN ('reserved', 'running') AND EXISTS ("
            "SELECT 1 FROM session_recap_jobs j WHERE j.session_id = job_session_id "
            "AND j.state = 'claimed' AND j.claim_token = ? "
            "AND j.active_attempt_id = session_recap_attempts.id "
            "AND j.requested_input_hash IS session_recap_attempts.input_hash "
            "AND j.retry_lineage = session_recap_attempts.retry_lineage)",
            (cleanup_state, now, attempt_id, token, token),
        ).rowcount
    )


def admit_provider(conn: sqlite3.Connection, session_id: int, token: int, now: str) -> int | None:
    """Atomically admit one provider call, including the post-cooldown probe."""
    conn.execute("INSERT OR IGNORE INTO session_recap_provider_health (singleton) VALUES (1)")
    row = conn.execute(
        """UPDATE session_recap_provider_health SET probe_active = 1, probe_token = probe_token + 1,
               probe_session_id = ?, probe_claim_token = ?
           WHERE singleton = 1 AND probe_active = 0 AND (retry_after IS NULL OR retry_after <= ?)
             AND EXISTS (
               SELECT 1 FROM session_recap_jobs
                WHERE session_id = ? AND state = 'claimed' AND claim_token = ?
                  AND active_attempt_id IS NULL
              ) RETURNING probe_token""",
        (session_id, token, now, session_id, token),
    ).fetchone()
    return row[0] if row else None


def defer_for_cooldown(conn: sqlite3.Connection, session_id: int, token: int, now: str) -> bool:
    return bool(
        conn.execute(
            """UPDATE session_recap_jobs SET state = 'pending', reason = 'deferred_cooldown',
               next_eligible_at = (
                 SELECT retry_after FROM session_recap_provider_health WHERE singleton = 1
               ), lease_expires_at = NULL, updated_at = ?
               WHERE session_id = ? AND claim_token = ? AND state = 'claimed'
                 AND active_attempt_id IS NULL
                 AND EXISTS (
                   SELECT 1 FROM session_recap_provider_health WHERE singleton = 1
                     AND (retry_after > ? OR probe_active = 1)
                 )""",
            (now, session_id, token, now),
        ).rowcount
    )


def reserve_attempt(
    conn: sqlite3.Connection,
    session_id: int,
    token: int,
    input_hash: str,
    trigger: str,
    now: str,
    *,
    provider_token: int | None = None,
    model: str | None = None,
    max_budget_usd: float | None = None,
    timeout_seconds: int | None = None,
) -> int:
    row = conn.execute(
        """INSERT INTO session_recap_attempts (
          session_id, job_session_id, input_hash, input_contract_version, policy_version,
          recap_contract_version, claim_token, trigger, model, max_budget_usd,
          timeout_seconds, state, created_at, retry_lineage, provider_token
        ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, retry_lineage, ?
          FROM session_recap_jobs WHERE session_id = ? AND state = 'claimed'
            AND claim_token = ? AND requested_input_hash IS ? AND active_attempt_id IS NULL
            AND (? IS NULL OR EXISTS (
              SELECT 1 FROM session_recap_provider_health WHERE singleton = 1
                AND probe_active = 1 AND probe_token = ? AND probe_session_id = ?
                AND probe_claim_token = ?
            )) RETURNING id""",
        (
            session_id,
            session_id,
            input_hash,
            RECAP_INPUT_CONTRACT_VERSION,
            ELIGIBILITY_POLICY_VERSION,
            RECAP_CONTRACT_VERSION,
            token,
            trigger,
            model,
            max_budget_usd,
            timeout_seconds,
            now,
            provider_token,
            session_id,
            token,
            input_hash,
            provider_token,
            provider_token,
            session_id,
            token,
        ),
    ).fetchone()
    if row is None:
        if provider_token is not None:
            conn.execute(
                "UPDATE session_recap_provider_health SET probe_active = 0, "
                "probe_session_id = NULL, probe_claim_token = NULL "
                "WHERE singleton = 1 AND probe_token = ? AND probe_active = 1 "
                "AND probe_session_id = ? AND probe_claim_token = ?",
                (provider_token, session_id, token),
            )
        raise RuntimeError("job input, claim, or provider admission is no longer active")
    attempt_id = row[0]
    updated = conn.execute(
        """UPDATE session_recap_jobs SET active_attempt_id = ?, updated_at = ?
           WHERE session_id = ? AND state = 'claimed' AND claim_token = ?
             AND requested_input_hash IS ? AND active_attempt_id IS NULL""",
        (attempt_id, now, session_id, token, input_hash),
    ).rowcount
    if not updated:
        if provider_token is not None:
            conn.execute(
                "UPDATE session_recap_provider_health SET probe_active = 0, "
                "probe_session_id = NULL, probe_claim_token = NULL "
                "WHERE singleton = 1 AND probe_token = ? AND probe_active = 1 "
                "AND probe_session_id = ? AND probe_claim_token = ?",
                (provider_token, session_id, token),
            )
        raise RuntimeError("job claim is no longer active")
    return attempt_id


def start_attempt(conn: sqlite3.Connection, attempt_id: int, token: int, now: str) -> bool:
    return bool(
        conn.execute(
            """UPDATE session_recap_attempts SET state = 'running', started_at = ?
               WHERE id = ? AND claim_token = ? AND state = 'reserved' AND EXISTS (
                 SELECT 1 FROM session_recap_jobs j
                 WHERE j.session_id = job_session_id AND j.state = 'claimed'
                   AND j.claim_token = ? AND j.active_attempt_id = session_recap_attempts.id
                   AND j.requested_input_hash IS session_recap_attempts.input_hash
                   AND j.retry_lineage = session_recap_attempts.retry_lineage
               )""",
            (now, attempt_id, token, token),
        ).rowcount
    )


def bind_attempt_packet(
    conn: sqlite3.Connection,
    attempt_id: int,
    token: int,
    packet_path: str,
    packet_nonce: str,
) -> bool:
    """Bind a deterministic packet owner before the filesystem is touched."""
    return bool(
        conn.execute(
            "UPDATE session_recap_attempts SET packet_path = ?, packet_nonce = ?, cleanup_state = 'reserved' "
            "WHERE id = ? AND claim_token = ? AND state = 'reserved' AND packet_path IS NULL",
            (packet_path, packet_nonce, attempt_id, token),
        ).rowcount
    )


def record_attempt_launch(
    conn: sqlite3.Connection,
    attempt_id: int,
    token: int,
    owner_pid: int,
    process_group_id: int,
    process_started_at: str,
    now: str,
) -> bool:
    """Persist exact launch identity before the provider boundary treats it as running."""
    return bool(
        conn.execute(
            "UPDATE session_recap_attempts SET owner_pid = ?, process_group_id = ?, process_started_at = ?, "
            "cleanup_state = 'launched', state = 'running', started_at = ? "
            "WHERE id = ? AND claim_token = ? AND state = 'reserved' AND packet_path IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM session_recap_jobs j WHERE j.session_id = job_session_id "
            "AND j.state = 'claimed' AND j.claim_token = ? AND j.active_attempt_id = session_recap_attempts.id)",
            (owner_pid, process_group_id, process_started_at, now, attempt_id, token, token),
        ).rowcount
    )


def quarantine_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    token: int,
    byte_size: int | None,
    cleanup_state: str,
    now: str,
) -> bool:
    """Retain only owner/process metadata when packet cleanup is uncertain."""
    return bool(
        conn.execute(
            "INSERT INTO session_recap_quarantine "
            "(attempt_id, path, nonce, byte_size, process_group_id, process_started_at, cleanup_state, created_at) "
            "SELECT id, packet_path, packet_nonce, ?, process_group_id, process_started_at, ?, ? "
            "FROM session_recap_attempts WHERE id = ? AND claim_token = ? AND packet_path IS NOT NULL "
            "AND packet_nonce IS NOT NULL ON CONFLICT(attempt_id) DO UPDATE SET byte_size = excluded.byte_size, "
            "cleanup_state = excluded.cleanup_state",
            (byte_size, cleanup_state, now, attempt_id, token),
        ).rowcount
    )


def quarantine_admission(
    conn: sqlite3.Connection, *, max_count: int, max_bytes: int
) -> tuple[bool, int, int, str | None]:
    """Return provider admission and content-free quarantine capacity metadata."""
    count, byte_size, oldest = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(byte_size), 0), MIN(created_at) FROM session_recap_quarantine"
    ).fetchone()
    return count < max_count and byte_size < max_bytes, count, byte_size, oldest


def cancel_attempt_before_launch(
    conn: sqlite3.Connection,
    attempt_id: int,
    token: int,
    input_hash: str,
    lineage: int,
    now: str,
) -> bool:
    """Close a fenced reservation only after its packet cleanup is proved."""
    attempt = conn.execute(
        """SELECT job_session_id, provider_token FROM session_recap_attempts
           WHERE id = ? AND claim_token = ? AND input_hash IS ? AND retry_lineage = ?
             AND state = 'reserved' AND started_at IS NULL AND cleanup_state = 'verified_removed'""",
        (attempt_id, token, input_hash, lineage),
    ).fetchone()
    if attempt is None:
        return False
    session_id, provider_token = attempt
    closed = conn.execute(
        """UPDATE session_recap_jobs SET active_attempt_id = NULL, lease_expires_at = NULL,
               state = 'pending', reason = 'cancelled_before_launch', next_eligible_at = NULL,
               updated_at = ? WHERE session_id = ? AND state = 'claimed' AND claim_token = ?
                 AND requested_input_hash IS ? AND retry_lineage = ?
                 AND active_attempt_id = ?""",
        (now, session_id, token, input_hash, lineage, attempt_id),
    ).rowcount
    if not closed:
        return False
    conn.execute(
        """UPDATE session_recap_attempts SET state = 'cancelled_before_launch', finished_at = ?
           WHERE id = ? AND claim_token = ? AND input_hash IS ? AND retry_lineage = ?
             AND state = 'reserved' AND started_at IS NULL AND cleanup_state = 'verified_removed'""",
        (now, attempt_id, token, input_hash, lineage),
    )
    if provider_token is not None:
        conn.execute(
            "UPDATE session_recap_provider_health SET probe_active = 0, "
            "probe_session_id = NULL, probe_claim_token = NULL "
            "WHERE singleton = 1 AND probe_token = ? AND probe_session_id = ? "
            "AND probe_claim_token = ?",
            (provider_token, session_id, token),
        )
    return True


def mark_current(
    conn: sqlite3.Connection,
    session_id: int,
    token: int,
    input_hash: str,
    lineage: int,
    now: str,
) -> bool:
    """Record a no-provider currentness decision for the claimed input generation."""
    return bool(
        conn.execute(
            """UPDATE session_recap_jobs SET state = 'current', reason = NULL,
               lease_expires_at = NULL, next_eligible_at = NULL, updated_at = ?
               WHERE session_id = ? AND state = 'claimed' AND claim_token = ?
                 AND requested_input_hash IS ? AND retry_lineage = ? AND active_attempt_id IS NULL""",
            (now, session_id, token, input_hash, lineage),
        ).rowcount
    )


def mark_excluded(
    conn: sqlite3.Connection,
    session_id: int,
    token: int,
    input_hash: str | None,
    lineage: int,
    reason: str,
    now: str,
) -> bool:
    """Record a no-provider eligibility decision for the claimed input generation."""
    return bool(
        conn.execute(
            """UPDATE session_recap_jobs SET state = 'excluded', reason = ?,
               lease_expires_at = NULL, next_eligible_at = NULL, updated_at = ?
               WHERE session_id = ? AND state = 'claimed' AND claim_token = ?
                 AND requested_input_hash IS ? AND retry_lineage = ? AND active_attempt_id IS NULL""",
            (reason, now, session_id, token, input_hash, lineage),
        ).rowcount
    )


def complete_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    token: int,
    outcome: str,
    now: str,
    *,
    diagnostic: str | None = None,
    retry_delay_seconds: int = 0,
) -> bool:
    """Complete only an active attempt for the job's exact current generation."""
    if outcome not in TERMINAL_ATTEMPTS or outcome in {"cancelled_before_launch", "global_abort"}:
        raise ValueError(f"not a normally completable terminal outcome: {outcome}")
    attempt = conn.execute(
        """SELECT job_session_id, retry_lineage, provider_token FROM session_recap_attempts
           WHERE id = ? AND claim_token = ? AND state IN ('reserved', 'running')""",
        (attempt_id, token),
    ).fetchone()
    if attempt is None:
        return False
    session_id, lineage, provider_token = attempt
    closed = conn.execute(
        """UPDATE session_recap_jobs SET
               active_attempt_id = CASE WHEN ? = 'cleanup_failed' THEN active_attempt_id ELSE NULL END,
               lease_expires_at = CASE WHEN ? = 'cleanup_failed' THEN lease_expires_at ELSE NULL END,
               updated_at = ? WHERE session_id = ? AND state = 'claimed' AND claim_token = ?
                 AND active_attempt_id = ? AND EXISTS (
                   SELECT 1 FROM session_recap_attempts a WHERE a.id = ?
                     AND a.input_hash IS session_recap_jobs.requested_input_hash
                     AND a.retry_lineage = session_recap_jobs.retry_lineage
                 )""",
        (outcome, outcome, now, session_id, token, attempt_id, attempt_id),
    ).rowcount
    if not closed:
        return False
    conn.execute(
        """UPDATE session_recap_attempts SET state = ?, diagnostic = ?, finished_at = ?
           WHERE id = ? AND claim_token = ? AND state IN ('reserved', 'running')""",
        (outcome, _diagnostic(diagnostic), now, attempt_id, token),
    )
    if provider_token is not None:
        conn.execute(
            "UPDATE session_recap_provider_health SET probe_active = 0, "
            "probe_session_id = NULL, probe_claim_token = NULL "
            "WHERE singleton = 1 AND probe_token = ? AND probe_session_id = ? "
            "AND probe_claim_token = ?",
            (provider_token, session_id, token),
        )
    if outcome == "succeeded":
        conn.execute(
            "UPDATE session_recap_jobs SET state = 'current', reason = NULL "
            "WHERE session_id = ? AND claim_token = ? AND retry_lineage = ?",
            (session_id, token, lineage),
        )
        if provider_token is not None:
            reset_health(conn)
    elif outcome == "timeout":
        count = conn.execute(
            "SELECT COUNT(*) FROM session_recap_attempts WHERE job_session_id = ? "
            "AND retry_lineage = ? AND state = 'timeout'",
            (session_id, lineage),
        ).fetchone()[0]
        if count == 1:
            conn.execute(
                f"UPDATE session_recap_jobs SET state = 'pending', reason = 'timeout_retry', "
                f"next_eligible_at = {_deadline(now, retry_delay_seconds)} "
                "WHERE session_id = ? AND claim_token = ? AND retry_lineage = ?",
                (now, retry_delay_seconds, session_id, token, lineage),
            )
        else:
            conn.execute(
                "UPDATE session_recap_jobs SET state = 'blocked', reason = 'timeout_exhausted' "
                "WHERE session_id = ? AND claim_token = ? AND retry_lineage = ?",
                (session_id, token, lineage),
            )
    elif outcome in {*CONTENT_DEPENDENT_BLOCK_REASONS, "cleanup_failed"}:
        conn.execute(
            "UPDATE session_recap_jobs SET state = 'blocked', reason = ? "
            "WHERE session_id = ? AND claim_token = ? AND retry_lineage = ?",
            (outcome, session_id, token, lineage),
        )
    else:
        conn.execute(
            "UPDATE session_recap_jobs SET state = 'pending', reason = ?, next_eligible_at = NULL "
            "WHERE session_id = ? AND claim_token = ? AND retry_lineage = ?",
            (outcome, session_id, token, lineage),
        )
    return True


def open_cooldown(
    conn: sqlite3.Connection,
    attempt_id: int,
    token: int,
    provider_token: int,
    reason: str,
    now: str,
    base_delay_seconds: int,
    max_delay_seconds: int,
    *,
    retry_after_seconds: int | None = None,
    diagnostic: str | None = None,
) -> int | None:
    """Atomically close the admitted attempt and open its fenced global cooldown."""
    conn.execute("INSERT OR IGNORE INTO session_recap_provider_health (singleton) VALUES (1)")
    admitted = conn.execute(
        """UPDATE session_recap_attempts SET state = 'global_abort', diagnostic = ?, finished_at = ?
           WHERE id = ? AND claim_token = ? AND provider_token = ?
              AND (state = 'running' OR (state = 'reserved' AND cleanup_state = 'verified_removed')) AND EXISTS (
               SELECT 1 FROM session_recap_jobs j WHERE j.session_id = job_session_id
                 AND j.state = 'claimed' AND j.claim_token = ?
                 AND j.active_attempt_id = session_recap_attempts.id
                 AND j.requested_input_hash IS session_recap_attempts.input_hash
                 AND j.retry_lineage = session_recap_attempts.retry_lineage
             ) AND EXISTS (
               SELECT 1 FROM session_recap_provider_health h WHERE h.singleton = 1
                  AND h.probe_active = 1 AND h.probe_token = ?
                  AND h.probe_session_id = session_recap_attempts.job_session_id
                  AND h.probe_claim_token = ?
              )""",
        (_diagnostic(diagnostic), now, attempt_id, token, provider_token, token, provider_token, token),
    ).rowcount
    if not admitted:
        return None
    conn.execute(
        """UPDATE session_recap_jobs SET active_attempt_id = NULL, lease_expires_at = NULL,
               state = 'pending', reason = 'global_abort', updated_at = ?
           WHERE session_id = (SELECT job_session_id FROM session_recap_attempts WHERE id = ?)
             AND state = 'claimed' AND claim_token = ?""",
        (now, attempt_id, token),
    )
    failures = conn.execute(
        """UPDATE session_recap_provider_health SET consecutive_failures = consecutive_failures + 1
           WHERE singleton = 1 AND probe_active = 1 AND probe_token = ?
           RETURNING consecutive_failures""",
        (provider_token,),
    ).fetchone()[0]
    delay = retry_after_seconds
    if delay is None:
        delay = min(base_delay_seconds * 2 ** (failures - 1), max_delay_seconds)
    delay = min(delay, max_delay_seconds)
    conn.execute(
        f"""UPDATE session_recap_provider_health SET reason = ?, diagnostic = ?, last_failed_at = ?,
                retry_after = {_deadline(now, delay)}, probe_active = 0, probe_session_id = NULL,
                probe_claim_token = NULL WHERE singleton = 1 AND probe_token = ?
                  AND probe_claim_token = ?""",
        (reason, _diagnostic(diagnostic), now, now, delay, provider_token, token),
    )
    conn.execute(
        f"""UPDATE session_recap_jobs SET next_eligible_at = {_deadline(now, delay)}
           WHERE session_id = (SELECT job_session_id FROM session_recap_attempts WHERE id = ?)
             AND claim_token = ? AND state = 'pending' AND reason = 'global_abort'""",
        (now, delay, attempt_id, token),
    )
    return delay


def reset_health(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE session_recap_provider_health SET reason = NULL, consecutive_failures = 0, "
        "diagnostic = NULL, last_failed_at = NULL, retry_after = NULL, probe_active = 0, "
        "probe_session_id = NULL, probe_claim_token = NULL "
        "WHERE singleton = 1"
    )


def targeted_retry(
    conn: sqlite3.Connection,
    session_id: int,
    now: str,
) -> bool:
    """Start a new user-authorized retry lineage after persisted cleanup proof."""
    row = conn.execute(
        "UPDATE session_recap_jobs SET state = 'pending', reason = NULL, next_eligible_at = NULL, "
        "active_attempt_id = NULL, retry_lineage = retry_lineage + 1, "
        "claim_token = claim_token + 1, updated_at = ? "
        "WHERE session_id = ? AND state = 'blocked' "
        "AND (active_attempt_id IS NULL OR EXISTS ("
        "SELECT 1 FROM session_recap_attempts a WHERE a.id = session_recap_jobs.active_attempt_id "
        "AND a.state = 'cleanup_failed' AND a.cleanup_state = 'verified_removed')) RETURNING claim_token",
        (now, session_id),
    ).fetchone()
    if row is None:
        return False
    return True


def requeue_changed_input_after_cleanup(conn: sqlite3.Connection, session_id: int, now: str) -> bool:
    """Release a cleanup-proven, changed-input job into a new retry lineage."""
    return bool(
        conn.execute(
            """UPDATE session_recap_jobs SET state = 'pending', reason = NULL,
                   active_attempt_id = NULL, lease_expires_at = NULL, next_eligible_at = NULL,
                   retry_lineage = retry_lineage + 1, claim_token = claim_token + 1,
                   updated_at = ?
               WHERE session_id = ? AND state = 'blocked' AND reason = 'cleanup_failed'
                 AND active_attempt_id IS NOT NULL
                 AND EXISTS (
                   SELECT 1 FROM session_recap_attempts a
                    WHERE a.id = session_recap_jobs.active_attempt_id
                      AND a.state = 'cleanup_failed'
                      AND a.cleanup_state = 'verified_removed'
                      AND a.input_hash IS NOT session_recap_jobs.requested_input_hash
                 )""",
            (now, session_id),
        ).rowcount
    )


def recover_expired_attempt(conn: sqlite3.Connection, attempt_id: int, now: str, *, cleanup_proven: bool) -> bool:
    """Fence an expired owner after recovery has resolved its cleanup obligation."""
    attempt = conn.execute(
        "SELECT job_session_id, claim_token, provider_token FROM session_recap_attempts "
        "WHERE id = ? AND state IN ('reserved', 'running')",
        (attempt_id,),
    ).fetchone()
    if attempt is None:
        return False
    session_id, token, provider_token = attempt
    if cleanup_proven:
        closed = conn.execute(
            """UPDATE session_recap_jobs SET state = 'pending', reason = NULL,
                   active_attempt_id = NULL, lease_expires_at = NULL, next_eligible_at = NULL,
                   retry_lineage = retry_lineage + 1, claim_token = claim_token + 1, updated_at = ?
               WHERE session_id = ? AND state = 'claimed' AND claim_token = ?
                 AND active_attempt_id = ?""",
            (now, session_id, token, attempt_id),
        ).rowcount
        outcome = "abandoned"
    else:
        closed = conn.execute(
            """UPDATE session_recap_jobs SET state = 'blocked', reason = 'cleanup_failed',
                   lease_expires_at = NULL, next_eligible_at = NULL, claim_token = claim_token + 1,
                   updated_at = ? WHERE session_id = ? AND state = 'claimed'
                     AND claim_token = ? AND active_attempt_id = ?""",
            (now, session_id, token, attempt_id),
        ).rowcount
        outcome = "cleanup_failed"
    if not closed:
        return False
    conn.execute(
        "UPDATE session_recap_attempts SET state = ?, finished_at = ? "
        "WHERE id = ? AND claim_token = ? AND state IN ('reserved', 'running')",
        (outcome, now, attempt_id, token),
    )
    if provider_token is not None:
        conn.execute(
            "UPDATE session_recap_provider_health SET probe_active = 0, "
            "probe_session_id = NULL, probe_claim_token = NULL "
            "WHERE singleton = 1 AND probe_token = ? AND probe_session_id = ? "
            "AND probe_claim_token = ?",
            (provider_token, session_id, token),
        )
    return True


def verify_cleanup_removed(conn: sqlite3.Connection, attempt_id: int, token: int, now: str) -> bool:
    """Persist removal proof and discard matching quarantine metadata."""
    verified = bool(
        conn.execute(
            """UPDATE session_recap_attempts SET cleanup_state = 'verified_removed', finished_at = ?
               WHERE id = ? AND claim_token = ? AND state = 'cleanup_failed' AND EXISTS (
                 SELECT 1 FROM session_recap_jobs j
                 WHERE j.session_id = session_recap_attempts.job_session_id
                   AND j.state = 'blocked' AND j.reason = 'cleanup_failed'
                   AND j.active_attempt_id = session_recap_attempts.id
               )""",
            (now, attempt_id, token),
        ).rowcount
    )
    if verified:
        conn.execute("DELETE FROM session_recap_quarantine WHERE attempt_id = ?", (attempt_id,))
    return verified


def create_run(
    conn: sqlite3.Connection,
    trigger: str,
    selector_json: str,
    candidates: list[tuple[int, str | None, str]],
    now: str,
    *,
    attempt_limit: int | None,
) -> int:
    run_id = conn.execute(
        "INSERT INTO session_recap_runs (trigger, selector_json, started_at, state, attempt_limit) "
        "VALUES (?, ?, ?, 'running', ?)",
        (trigger, selector_json, now, attempt_limit),
    ).lastrowid
    if run_id is None:
        raise RuntimeError("failed to create recap run")
    conn.executemany(
        "INSERT INTO session_recap_run_candidates "
        "(run_id, session_id, input_hash, initial_disposition, final_disposition) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (run_id, session_id, input_hash, disposition, disposition if disposition != "pending" else None)
            for session_id, input_hash, disposition in candidates
        ],
    )
    return run_id


def reserve_attempt_for_run(
    conn: sqlite3.Connection,
    run_id: int,
    session_id: int,
    token: int,
    input_hash: str,
    trigger: str,
    now: str,
    *,
    provider_token: int | None = None,
    model: str | None = None,
    max_budget_usd: float | None = None,
    timeout_seconds: int | None = None,
) -> int | None:
    """Reserve an attempt only when this running snapshot can own it within its limit."""
    conn.execute("SAVEPOINT reserve_attempt_for_run")
    try:
        attempt_id = reserve_attempt(
            conn,
            session_id,
            token,
            input_hash,
            trigger,
            now,
            provider_token=provider_token,
            model=model,
            max_budget_usd=max_budget_usd,
            timeout_seconds=timeout_seconds,
        )
        attached = conn.execute(
            """UPDATE session_recap_run_candidates SET started_attempt_id = ?,
               final_disposition = 'attempted' WHERE run_id = ? AND session_id = ?
                  AND started_attempt_id IS NULL AND final_disposition IS NULL
                  AND EXISTS (SELECT 1 FROM session_recap_runs WHERE id = ? AND state = 'running')
                  AND EXISTS (
                    SELECT 1 FROM session_recap_attempts a WHERE a.id = ?
                      AND a.job_session_id = session_recap_run_candidates.session_id
                      AND a.input_hash IS session_recap_run_candidates.input_hash
                  ) AND NOT EXISTS (
                    SELECT 1 FROM session_recap_run_candidates WHERE started_attempt_id = ?
                  ) AND ((SELECT attempt_limit FROM session_recap_runs WHERE id = ?) IS NULL
                    OR (SELECT COUNT(*) FROM session_recap_run_candidates
                        WHERE run_id = ? AND started_attempt_id IS NOT NULL)
                      < (SELECT attempt_limit FROM session_recap_runs WHERE id = ?))""",
            (attempt_id, run_id, session_id, run_id, attempt_id, attempt_id, run_id, run_id, run_id),
        ).rowcount
        if attached:
            conn.execute("RELEASE SAVEPOINT reserve_attempt_for_run")
            return attempt_id
    except RuntimeError:
        pass
    conn.execute("ROLLBACK TO SAVEPOINT reserve_attempt_for_run")
    conn.execute("RELEASE SAVEPOINT reserve_attempt_for_run")
    if provider_token is not None:
        conn.execute(
            "UPDATE session_recap_provider_health SET probe_active = 0, "
            "probe_session_id = NULL, probe_claim_token = NULL "
            "WHERE singleton = 1 AND probe_token = ? AND probe_active = 1 "
            "AND probe_session_id = ? AND probe_claim_token = ?",
            (provider_token, session_id, token),
        )
    return None


def finalize_run(conn: sqlite3.Connection, run_id: int, now: str, *, global_abort: bool = False) -> bool:
    """Close immutable accounting using the snapshot-owned final disposition."""
    if global_abort:
        conn.execute(
            "UPDATE session_recap_run_candidates SET final_disposition = 'deferred_after_abort' "
            "WHERE run_id = ? AND final_disposition IS NULL",
            (run_id,),
        )
        state = "incomplete"
    else:
        conn.execute(
            """UPDATE session_recap_run_candidates AS c SET final_disposition = CASE
                 WHEN EXISTS (SELECT 1 FROM session_recap_jobs j WHERE j.session_id = c.session_id
                   AND j.requested_input_hash IS c.input_hash AND j.state = 'current')
                 THEN 'already_current'
                 WHEN EXISTS (SELECT 1 FROM session_recap_jobs j WHERE j.session_id = c.session_id
                   AND j.state = 'claimed') THEN 'already_running'
                 WHEN EXISTS (SELECT 1 FROM session_recap_jobs j WHERE j.session_id = c.session_id
                   AND j.state = 'blocked') THEN 'blocked'
                 WHEN EXISTS (SELECT 1 FROM session_recap_provider_health h WHERE h.singleton = 1
                   AND (h.probe_active = 1 OR h.retry_after > ?)) THEN 'deferred_cooldown'
                 ELSE 'deferred_by_limit' END
               WHERE c.run_id = ? AND c.final_disposition IS NULL""",
            (now, run_id),
        )
        state = "complete"
    return bool(
        conn.execute(
            "UPDATE session_recap_runs SET state = ?, finished_at = ? WHERE id = ? AND state = 'running'",
            (state, now, run_id),
        ).rowcount
    )


def run_partitions(conn: sqlite3.Connection, run_id: int) -> tuple[int, int]:
    candidates = conn.execute(
        "SELECT COUNT(*) FROM session_recap_run_candidates WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    finalized = conn.execute(
        "SELECT COUNT(*) FROM session_recap_run_candidates WHERE run_id = ? AND final_disposition IS NOT NULL",
        (run_id,),
    ).fetchone()[0]
    return candidates, finalized


def latest_attempts(
    conn: sqlite3.Connection, session_id: int | None = None, *, limit: int | None = None
) -> list[sqlite3.Row | tuple]:
    sql = (
        "SELECT a.* FROM session_recap_attempts a WHERE a.id = ("
        "SELECT b.id FROM session_recap_attempts b WHERE b.job_session_id = a.job_session_id "
        "ORDER BY b.id DESC LIMIT 1)"
    )
    if session_id is not None:
        sql += " AND a.job_session_id = ?"
        params: tuple[object, ...] = (session_id,)
    else:
        params = ()
    sql += " ORDER BY a.id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit,)
    return conn.execute(sql, params).fetchall()


def retention_candidates(conn: sqlite3.Connection, cutoff: str, *, limit: int) -> list[int]:
    """Return bounded terminal attempts outside retained retry lineages."""
    rows = conn.execute(
        """WITH protected_attempts AS (
             SELECT a.id FROM session_recap_attempts a
              JOIN session_recap_jobs j ON j.session_id = a.job_session_id
              WHERE a.state IN ('reserved', 'running')
                 OR (a.state = 'succeeded' AND a.input_hash IS j.requested_input_hash
                    AND a.id = (SELECT MAX(s.id) FROM session_recap_attempts s
                                WHERE s.job_session_id = a.job_session_id
                                  AND s.input_hash IS a.input_hash AND s.state = 'succeeded'))
                OR a.id = (SELECT MAX(t.id) FROM session_recap_attempts t
                            WHERE t.job_session_id = a.job_session_id
                              AND t.state IN ('succeeded', 'stale_discarded', 'timeout',
                                'budget_exceeded', 'unusable_output', 'global_abort',
                                'cleanup_failed', 'abandoned', 'cancelled_before_launch'))
            ), retained_lineages AS (
              SELECT DISTINCT a.job_session_id, a.retry_lineage FROM session_recap_attempts a
              JOIN protected_attempts p ON p.id = a.id
            ) SELECT a.id FROM session_recap_attempts a
            WHERE a.finished_at < ? AND a.state IN ('succeeded', 'stale_discarded', 'timeout',
                'budget_exceeded', 'unusable_output', 'global_abort', 'cleanup_failed',
                'abandoned', 'cancelled_before_launch')
              AND (a.state IS NOT 'cleanup_failed' OR a.cleanup_state = 'verified_removed')
              AND NOT EXISTS (
                 SELECT 1 FROM retained_lineages r WHERE r.job_session_id = a.job_session_id
                   AND r.retry_lineage = a.retry_lineage
               ) AND NOT EXISTS (
                 SELECT 1 FROM session_recap_run_candidates c WHERE c.started_attempt_id = a.id
               ) ORDER BY a.job_session_id, a.retry_lineage, a.id LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return [row[0] for row in rows]


def run_retention_candidates(conn: sqlite3.Connection, cutoff: str, *, limit: int) -> list[int]:
    """Detailed closed run records become prune-eligible after the retention window."""
    rows = conn.execute(
        """SELECT r.id FROM session_recap_runs r WHERE r.started_at < ?
           AND r.state IN ('complete', 'incomplete') AND NOT EXISTS (
             SELECT 1 FROM session_recap_run_candidates c JOIN session_recap_attempts a
               ON a.id = c.started_attempt_id WHERE c.run_id = r.id AND (
                 a.finished_at >= ? OR a.state NOT IN ('succeeded', 'stale_discarded', 'timeout',
                   'budget_exceeded', 'unusable_output', 'global_abort', 'cleanup_failed',
                   'abandoned', 'cancelled_before_launch') OR EXISTS (
                     SELECT 1 FROM session_recap_attempts p WHERE p.job_session_id = a.job_session_id
                       AND p.retry_lineage = a.retry_lineage AND (p.state IN ('reserved', 'running')
                         OR (p.state = 'succeeded' AND p.input_hash IS (
                           SELECT j.requested_input_hash FROM session_recap_jobs j WHERE j.session_id = p.job_session_id
                         ) AND p.id = (SELECT MAX(s.id) FROM session_recap_attempts s
                           WHERE s.job_session_id = p.job_session_id AND s.input_hash IS p.input_hash
                             AND s.state = 'succeeded'))
                         OR p.id = (SELECT MAX(t.id) FROM session_recap_attempts t
                           WHERE t.job_session_id = p.job_session_id AND t.state IN ('succeeded', 'stale_discarded',
                             'timeout', 'budget_exceeded', 'unusable_output', 'global_abort', 'cleanup_failed',
                             'abandoned', 'cancelled_before_launch')))
                   )
               )
           ) ORDER BY r.started_at, r.id LIMIT ?""",
        (cutoff, cutoff, limit),
    ).fetchall()
    return [row[0] for row in rows]


def prune_retention(conn: sqlite3.Connection, cutoff: str, *, limit: int) -> tuple[int, int]:
    """Prune bounded terminal detail without deleting jobs, recaps, or uncertain packets."""
    runs = run_retention_candidates(conn, cutoff, limit=limit)
    # Run candidates retain their selected attempt for immutable accounting. Remove
    # only eligible closed-run accounting before considering those attempts.
    conn.executemany("DELETE FROM session_recap_run_candidates WHERE run_id = ?", [(value,) for value in runs])
    attempts = retention_candidates(conn, cutoff, limit=limit)
    # Only cleanup-proven attempts reach this list, so their owner metadata can
    # be removed with the corresponding terminal history.
    conn.executemany("DELETE FROM session_recap_quarantine WHERE attempt_id = ?", [(value,) for value in attempts])
    conn.executemany("DELETE FROM session_recap_attempts WHERE id = ?", [(value,) for value in attempts])
    conn.executemany("DELETE FROM session_recap_runs WHERE id = ?", [(value,) for value in runs])
    return len(attempts), len(runs)


def run_accounting(conn: sqlite3.Connection, run_id: int) -> dict[str, object]:
    """Return durable immutable membership and started-attempt partitions."""
    population, finalized = run_partitions(conn, run_id)
    initial = dict(
        conn.execute(
            "SELECT initial_disposition, COUNT(*) FROM session_recap_run_candidates WHERE run_id = ? "
            "GROUP BY initial_disposition",
            (run_id,),
        )
    )
    final = dict(
        conn.execute(
            "SELECT final_disposition, COUNT(*) FROM session_recap_run_candidates WHERE run_id = ? "
            "AND final_disposition IS NOT NULL GROUP BY final_disposition",
            (run_id,),
        )
    )
    outcomes = dict(
        conn.execute(
            "SELECT a.state, COUNT(*) FROM session_recap_run_candidates c "
            "JOIN session_recap_attempts a ON a.id = c.started_attempt_id WHERE c.run_id = ? GROUP BY a.state",
            (run_id,),
        )
    )
    run = conn.execute("SELECT state, attempt_limit FROM session_recap_runs WHERE id = ?", (run_id,)).fetchone()
    return {
        "run_id": run_id,
        "state": run[0] if run else "missing",
        "attempt_limit": run[1] if run else None,
        "population": population,
        "finalized": finalized,
        "initial_dispositions": initial,
        "final_dispositions": final,
        "started_attempts": sum(outcomes.values()),
        "attempt_outcomes": outcomes,
    }
