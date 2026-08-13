import pytest

import ccrecall.recap_state as recap_state
from ccrecall.llm_summary_db import get_connection
from ccrecall.recap_state import (
    acknowledge_cleanup,
    admit_provider,
    cancel_attempt_before_launch,
    claim_job,
    claim_runtime,
    complete_attempt,
    create_run,
    finalize_run,
    heartbeat_job,
    heartbeat_runtime,
    latest_attempts,
    mark_current,
    mark_excluded,
    open_cooldown,
    prune_retention,
    recap_state_changed_input,
    recover_expired_attempt,
    requeue_changed_input_after_cleanup,
    reserve_attempt,
    reserve_attempt_for_run,
    retention_candidates,
    run_partitions,
    run_retention_candidates,
    start_attempt,
    targeted_retry,
    upsert_job,
    verify_cleanup_removed,
)

NOW = "2026-08-12T10:00:00Z"


def _connection(tmp_path):
    settings = {"db_path": str(tmp_path / "recaps.db")}
    with get_connection(settings) as conn:
        conn.execute("INSERT INTO projects (path, key, name) VALUES ('/tmp', 'p', 'p')")
        conn.executemany("INSERT INTO sessions (uuid, project_id) VALUES (?, 1)", [("session-a",), ("session-b",)])
    return settings


def _attempt(conn, session_id, token, input_hash="input-a", *, provider_token=None):
    attempt = reserve_attempt(conn, session_id, token, input_hash, "test", NOW, provider_token=provider_token)
    assert start_attempt(conn, attempt, token, NOW)
    return attempt


def test_newest_input_and_targeted_retry_create_new_lineages(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        upsert_job(conn, 1, "input-b", "end", "2026-08-12T10:00:01Z")
        assert conn.execute("SELECT requested_input_hash, state, retry_lineage FROM session_recap_jobs").fetchone() == (
            "input-b",
            "pending",
            1,
        )
        token = claim_job(conn, 1, NOW, 60)
        assert complete_attempt(conn, _attempt(conn, 1, token, "input-b"), token, "unusable_output", NOW)
        assert targeted_retry(conn, 1, NOW)
        assert conn.execute("SELECT state, retry_lineage, claim_token FROM session_recap_jobs").fetchone() == (
            "pending",
            2,
            token + 1,
        )


def test_every_old_token_mutation_is_fenced_after_reclaim(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        first = claim_job(conn, 1, NOW, 1)
        attempt = reserve_attempt(conn, 1, first, "input-a", "end", NOW)
        assert claim_job(conn, 1, "2026-08-12T10:01:00Z", 60, cleanup_proven=True) == first + 1
        assert not heartbeat_job(conn, 1, first, NOW, 60)
        assert not start_attempt(conn, attempt, first, NOW)
        assert not acknowledge_cleanup(conn, attempt, first, "removed", NOW)
        assert not complete_attempt(conn, attempt, first, "cleanup_failed", NOW)
        runtime = claim_runtime(conn, 10, NOW, 1)
        assert claim_runtime(conn, 11, "2026-08-12T10:01:00Z", 60, cleanup_proven=True) == runtime + 1
        assert not heartbeat_runtime(conn, runtime, NOW, 60)


def test_cancel_before_launch_requires_a_fenced_cleaned_reservation(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        token = claim_job(conn, 1, NOW, 60)
        attempt = reserve_attempt(conn, 1, token, "input-a", "test", NOW)

        assert not cancel_attempt_before_launch(conn, attempt, token, "input-a", 0, NOW)
        assert acknowledge_cleanup(conn, attempt, token, "verified_removed", NOW)
        assert cancel_attempt_before_launch(conn, attempt, token, "input-a", 0, NOW)
        assert conn.execute("SELECT state FROM session_recap_attempts WHERE id = ?", (attempt,)).fetchone() == (
            "cancelled_before_launch",
        )
        assert conn.execute("SELECT state, active_attempt_id FROM session_recap_jobs").fetchone() == (
            "pending",
            None,
        )


def test_running_attempt_cannot_be_cancelled_before_launch(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        token = claim_job(conn, 1, NOW, 60)
        attempt = _attempt(conn, 1, token)

        with pytest.raises(ValueError, match="not a normally completable"):
            complete_attempt(conn, attempt, token, "cancelled_before_launch", NOW)
        assert not cancel_attempt_before_launch(conn, attempt, token, "input-a", 0, NOW)
        assert conn.execute("SELECT state FROM session_recap_attempts WHERE id = ?", (attempt,)).fetchone() == (
            "running",
        )


@pytest.mark.parametrize("decision", ["current", "excluded"])
def test_no_provider_decisions_are_fenced_to_the_claimed_input_generation(tmp_path, decision):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        token = claim_job(conn, 1, NOW, 60)
        decide = mark_current if decision == "current" else mark_excluded
        arguments = (conn, 1, token, "input-a", 0)
        if decision == "excluded":
            arguments += ("not_meaningful",)
        assert decide(*arguments, NOW)
        assert conn.execute("SELECT state FROM session_recap_jobs").fetchone() == (decision,)


@pytest.mark.parametrize("decision", ["current", "excluded"])
def test_no_provider_decisions_reject_reclaimed_or_changed_input_claims(tmp_path, decision):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        first = claim_job(conn, 1, NOW, 1)
        second = claim_job(conn, 1, "2026-08-12T10:01:00Z", 60, cleanup_proven=True)
        decide = mark_current if decision == "current" else mark_excluded
        arguments = (conn, 1, first, "input-a", 0)
        if decision == "excluded":
            arguments += ("not_meaningful",)
        assert not decide(*arguments, NOW)
        upsert_job(conn, 1, "input-b", "end", "2026-08-12T10:01:01Z")
        arguments = (conn, 1, second, "input-a", 0)
        if decision == "excluded":
            arguments += ("not_meaningful",)
        assert not decide(*arguments, NOW)


def test_runtime_and_provider_admission_are_exclusive(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        upsert_job(conn, 2, "input-a", "end", NOW)
        assert claim_runtime(conn, 1, NOW, 60) == 1
        assert claim_runtime(conn, 2, NOW, 60) is None
        one, two = claim_job(conn, 1, NOW, 60), claim_job(conn, 2, NOW, 60)
        probe = admit_provider(conn, 1, one, NOW)
        assert probe == 1
        assert admit_provider(conn, 2, two, NOW) is None
        attempt = _attempt(conn, 1, one, provider_token=probe)
        assert complete_attempt(conn, attempt, one, "succeeded", NOW)
        assert admit_provider(conn, 2, two, NOW) == 2


def test_stale_reservation_releases_its_unused_provider_admission(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        upsert_job(conn, 2, "input-a", "end", NOW)
        first, second = claim_job(conn, 1, NOW, 60), claim_job(conn, 2, NOW, 60)
        probe = admit_provider(conn, 1, first, NOW)
        upsert_job(conn, 1, "input-b", "end", "2026-08-12T10:00:01Z")
        with pytest.raises(RuntimeError, match="input"):
            reserve_attempt(conn, 1, first, "input-a", "test", NOW, provider_token=probe)
        assert admit_provider(conn, 2, second, NOW) == probe + 1


def test_provider_admission_cannot_be_stolen_by_another_claimed_job(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        upsert_job(conn, 2, "input-a", "end", NOW)
        one, two = claim_job(conn, 1, NOW, 60), claim_job(conn, 2, NOW, 60)
        probe = admit_provider(conn, 1, one, NOW)

        with pytest.raises(RuntimeError, match="provider admission"):
            reserve_attempt(conn, 2, two, "input-a", "test", NOW, provider_token=probe)

        assert conn.execute(
            "SELECT probe_active, probe_session_id, probe_claim_token FROM session_recap_provider_health"
        ).fetchone() == (1, 1, one)


def test_provider_admission_rejects_a_job_with_an_active_attempt(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        token = claim_job(conn, 1, NOW, 60)
        _attempt(conn, 1, token)

        assert admit_provider(conn, 1, token, NOW) is None


def test_recovery_does_not_release_another_jobs_unowned_provider_admission(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        upsert_job(conn, 2, "input-a", "end", NOW)
        first = claim_job(conn, 1, NOW, 1)
        second = claim_job(conn, 2, NOW, 60)
        probe = admit_provider(conn, 2, second, NOW)

        assert claim_job(conn, 1, "2026-08-12T10:01:00Z", 60, cleanup_proven=True) == first + 1
        assert conn.execute("SELECT probe_active FROM session_recap_provider_health").fetchone() == (1,)
        assert _attempt(conn, 2, second, provider_token=probe)


def test_cooldown_is_capped_and_allows_one_post_expiry_probe(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        token = claim_job(conn, 1, NOW, 60)
        probe = admit_provider(conn, 1, token, NOW)
        attempt = _attempt(conn, 1, token, provider_token=probe)
        assert open_cooldown(conn, attempt, token, probe, "rate_limited", NOW, 10, 30, diagnostic="rate_limited") == 10
        assert claim_job(conn, 1, "2026-08-12T10:00:09Z", 60) is None
        token = claim_job(conn, 1, "2026-08-12T10:00:11Z", 60)
        probe = admit_provider(conn, 1, token, "2026-08-12T10:00:11Z")
        attempt = _attempt(conn, 1, token, provider_token=probe)
        assert (
            open_cooldown(
                conn, attempt, token, probe, "rate_limited", "2026-08-12T10:00:11Z", 10, 30, retry_after_seconds=99
            )
            == 30
        )
        token = claim_job(conn, 1, "2026-08-12T10:00:42Z", 60)
        probe = admit_provider(conn, 1, token, "2026-08-12T10:00:42Z")
        assert probe is not None
        assert admit_provider(conn, 1, token, "2026-08-12T10:00:42Z") is None
        attempt = _attempt(conn, 1, token, provider_token=probe)
        assert complete_attempt(conn, attempt, token, "succeeded", NOW)
        assert conn.execute(
            "SELECT consecutive_failures, retry_after FROM session_recap_provider_health"
        ).fetchone() == (0, None)


def test_global_cooldown_attempt_diagnostic_is_sanitized_and_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(recap_state, "DIAGNOSTIC_LIMIT", 4)
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        token = claim_job(conn, 1, NOW, 60)
        probe = admit_provider(conn, 1, token, NOW)
        attempt = _attempt(conn, 1, token, provider_token=probe)
        assert open_cooldown(conn, attempt, token, probe, "rate_limited", NOW, 10, 30, diagnostic="rate_limited") == 10
        assert conn.execute("SELECT diagnostic FROM session_recap_attempts WHERE id = ?", (attempt,)).fetchone() == (
            "rate",
        )
        assert conn.execute("SELECT diagnostic FROM session_recap_provider_health").fetchone() == ("rate",)


@pytest.mark.parametrize("outcome", ["unusable_output", "budget_exceeded"])
def test_stable_failures_have_no_automatic_retry_but_timeout_gets_one(tmp_path, outcome):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        token = claim_job(conn, 1, NOW, 60)
        assert complete_attempt(conn, _attempt(conn, 1, token), token, outcome, NOW)
        assert claim_job(conn, 1, "2026-08-12T11:00:00Z", 60) is None
        assert targeted_retry(conn, 1, NOW)
        token = claim_job(conn, 1, "2026-08-12T11:00:00Z", 60)
        assert complete_attempt(conn, _attempt(conn, 1, token), token, "timeout", NOW)
        token = claim_job(conn, 1, "2026-08-12T11:00:01Z", 60)
        assert complete_attempt(conn, _attempt(conn, 1, token), token, "timeout", NOW)
        assert conn.execute("SELECT reason FROM session_recap_jobs").fetchone() == ("timeout_exhausted",)


def test_run_snapshot_ownership_and_abort_partition_are_exact(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        upsert_job(conn, 2, "input-a", "end", NOW)
        run = create_run(
            conn, "backfill", "{}", [(1, "input-a", "pending"), (2, "input-a", "pending")], NOW, attempt_limit=1
        )
        other = create_run(conn, "backfill", "{}", [(1, "input-a", "pending")], NOW, attempt_limit=1)
        one = claim_job(conn, 1, NOW, 60)
        claim_job(conn, 2, NOW, 60)
        attempt = reserve_attempt_for_run(conn, run, 1, one, "input-a", "backfill", NOW)
        assert attempt is not None
        assert reserve_attempt_for_run(conn, other, 1, one, "input-a", "backfill", NOW) is None
        assert conn.execute("SELECT COUNT(*) FROM session_recap_attempts").fetchone() == (1,)
        assert finalize_run(conn, run, NOW, global_abort=True)
        assert run_partitions(conn, run) == (2, 2)
        assert conn.execute("SELECT state FROM session_recap_runs WHERE id = ?", (run,)).fetchone() == ("incomplete",)
        assert conn.execute(
            "SELECT final_disposition FROM session_recap_run_candidates WHERE run_id = ? AND session_id = 2", (run,)
        ).fetchone() == ("deferred_after_abort",)


def test_latest_access_path_and_lineage_preserving_bounded_retention(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        for number in range(15):
            token = claim_job(conn, 1, f"2025-01-{number + 1:02d}T00:00:00Z", 60, cleanup_proven=True)
            attempt = reserve_attempt(conn, 1, token, "input-a", "test", NOW)
            assert complete_attempt(conn, attempt, token, "timeout", f"2025-01-{number + 1:02d}T00:00:00Z")
            token = claim_job(conn, 1, f"2025-01-{number + 1:02d}T00:00:01Z", 60)
            attempt = reserve_attempt(conn, 1, token, "input-a", "test", NOW)
            assert complete_attempt(conn, attempt, token, "timeout", f"2025-01-{number + 1:02d}T00:00:01Z")
            if number < 14:
                assert targeted_retry(conn, 1, NOW)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM session_recap_attempts WHERE job_session_id = ? ORDER BY id DESC LIMIT 1",
            (1,),
        ).fetchall()
        assert any("idx_recap_attempts_job_latest" in row[3] for row in plan)
        assert len(latest_attempts(conn, 1)) == 1
        candidates = retention_candidates(conn, "2026-01-01T00:00:00Z", limit=5)
        assert len(candidates) <= 5
        assert max(candidates, default=0) < conn.execute("SELECT MAX(id) FROM session_recap_attempts").fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM session_recap_jobs").fetchone() == (1,)


def test_new_input_fences_old_attempt_without_claiming_cleanup(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        token = claim_job(conn, 1, NOW, 60)
        attempt = _attempt(conn, 1, token)
        upsert_job(conn, 1, "input-b", "end", "2026-08-12T10:00:01Z")
        assert not complete_attempt(conn, attempt, token, "succeeded", NOW)
        assert not start_attempt(conn, attempt, token, NOW)
        assert claim_job(conn, 1, NOW, 60) is None
        assert claim_job(conn, 1, NOW, 60, cleanup_proven=True) == token + 2
        row = conn.execute("SELECT requested_input_hash, state, active_attempt_id FROM session_recap_jobs").fetchone()
        assert row == ("input-b", "claimed", None)
        assert conn.execute("SELECT state FROM session_recap_attempts WHERE id = ?", (attempt,)).fetchone() == (
            "abandoned",
        )


def test_changed_input_fences_a_claimed_job_without_releasing_its_attempt(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        first = claim_job(conn, 1, NOW, 60)
        attempt = _attempt(conn, 1, first)

        assert recap_state_changed_input(conn, 1, "input-b", "2026-08-12T10:00:01Z")
        assert conn.execute(
            "SELECT requested_input_hash, state, active_attempt_id, retry_lineage, claim_token FROM session_recap_jobs"
        ).fetchone() == ("input-b", "claimed", attempt, 1, first + 1)
        assert not heartbeat_job(conn, 1, first, NOW, 60)
        assert not complete_attempt(conn, attempt, first, "succeeded", NOW)


def test_recover_expired_attempt_reclaims_a_claim_fenced_by_changed_input(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        first = claim_job(conn, 1, NOW, 60)
        attempt = _attempt(conn, 1, first)

        assert recap_state_changed_input(conn, 1, "input-b", "2026-08-12T10:00:01Z")

        assert recover_expired_attempt(conn, attempt, "2026-08-12T10:01:00Z", cleanup_proven=True)
        assert conn.execute(
            "SELECT requested_input_hash, state, active_attempt_id FROM session_recap_jobs"
        ).fetchone() == ("input-b", "pending", None)
        assert conn.execute("SELECT state FROM session_recap_attempts WHERE id = ?", (attempt,)).fetchone() == (
            "abandoned",
        )


def test_changed_input_requeues_a_claimed_job_before_attempt_reservation(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        first = claim_job(conn, 1, NOW, 60)

        assert recap_state_changed_input(conn, 1, "input-b", "2026-08-12T10:00:01Z")
        assert conn.execute(
            "SELECT requested_input_hash, state, reason, lease_expires_at, next_eligible_at, "
            "active_attempt_id, retry_lineage, claim_token FROM session_recap_jobs"
        ).fetchone() == ("input-b", "pending", None, None, None, None, 1, first + 1)
        assert claim_job(conn, 1, "2026-08-12T10:00:01Z", 60) == first + 2


def test_proven_cleanup_releases_fenced_running_attempt_provider_admission(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        first = claim_job(conn, 1, NOW, 60)
        probe = admit_provider(conn, 1, first, NOW)
        _attempt(conn, 1, first, provider_token=probe)
        upsert_job(conn, 1, "input-b", "end", "2026-08-12T10:00:01Z")

        second = claim_job(conn, 1, NOW, 60, cleanup_proven=True)

        assert admit_provider(conn, 1, second, NOW) == probe + 1


def test_changed_input_cleanup_failure_requires_proof_before_requeue(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        first = claim_job(conn, 1, NOW, 60)
        probe = admit_provider(conn, 1, first, NOW)
        attempt = _attempt(conn, 1, first, provider_token=probe)
        assert complete_attempt(conn, attempt, first, "cleanup_failed", NOW)

        upsert_job(conn, 1, "input-b", "end", "2026-08-12T10:00:01Z")

        assert conn.execute(
            "SELECT state, reason, active_attempt_id, retry_lineage, claim_token FROM session_recap_jobs"
        ).fetchone() == ("blocked", "cleanup_failed", attempt, 0, first)
        assert claim_job(conn, 1, NOW, 60) is None
        assert admit_provider(conn, 1, first, NOW) is None

        assert not requeue_changed_input_after_cleanup(conn, 1, "2026-08-12T10:00:01Z")
        assert verify_cleanup_removed(conn, attempt, first, "2026-08-12T10:00:01Z")
        assert requeue_changed_input_after_cleanup(conn, 1, "2026-08-12T10:00:01Z")
        second = claim_job(conn, 1, NOW, 60)
        assert second == first + 2
        assert admit_provider(conn, 1, second, NOW) == probe + 1


def test_proven_cleanup_releases_unreserved_provider_admission(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        first = claim_job(conn, 1, NOW, 1)
        probe = admit_provider(conn, 1, first, NOW)

        second = claim_job(conn, 1, "2026-08-12T10:01:00Z", 60, cleanup_proven=True)

        assert admit_provider(conn, 1, second, NOW) == probe + 1


def test_targeted_retry_fences_a_stale_claim_heartbeat(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        first = claim_job(conn, 1, NOW, 60)
        assert complete_attempt(conn, _attempt(conn, 1, first), first, "unusable_output", NOW)

        assert targeted_retry(conn, 1, NOW)
        second = claim_job(conn, 1, NOW, 60)
        assert second > first
        assert not heartbeat_job(conn, 1, first, NOW, 60)


def test_targeted_retry_requires_cleanup_proof_before_replacement_admission(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        first = claim_job(conn, 1, NOW, 60)
        probe = admit_provider(conn, 1, first, NOW)
        attempt = _attempt(conn, 1, first, provider_token=probe)
        assert complete_attempt(conn, attempt, first, "cleanup_failed", NOW)

        assert not targeted_retry(conn, 1, NOW)
        assert conn.execute("SELECT state, active_attempt_id FROM session_recap_jobs").fetchone() == (
            "blocked",
            attempt,
        )
        assert admit_provider(conn, 1, first, NOW) is None

        assert verify_cleanup_removed(conn, attempt, first, NOW)
        assert targeted_retry(conn, 1, NOW)
        second = claim_job(conn, 1, NOW, 60)
        assert admit_provider(conn, 1, second, NOW) == probe + 1


def test_reservation_for_run_requires_snapshot_input_ownership(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        token = claim_job(conn, 1, NOW, 60)
        with pytest.raises(RuntimeError, match="input"):
            reserve_attempt(conn, 1, token, "input-b", "test", NOW)
        run = create_run(conn, "backfill", "{}", [(1, "input-b", "pending")], NOW, attempt_limit=1)
        assert reserve_attempt_for_run(conn, run, 1, token, "input-a", "test", NOW) is None
        assert conn.execute("SELECT COUNT(*) FROM session_recap_attempts").fetchone() == (0,)


def test_late_worker_cannot_open_cooldown_or_mutate_health(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        token = claim_job(conn, 1, NOW, 60)
        probe = admit_provider(conn, 1, token, NOW)
        attempt = _attempt(conn, 1, token, provider_token=probe)
        upsert_job(conn, 1, "input-b", "end", "2026-08-12T10:00:01Z")
        assert open_cooldown(conn, attempt, token, probe, "rate_limited", NOW, 10, 30) is None
        assert conn.execute(
            "SELECT consecutive_failures, retry_after FROM session_recap_provider_health"
        ).fetchone() == (0, None)
        assert conn.execute("SELECT state FROM session_recap_attempts WHERE id = ?", (attempt,)).fetchone() == (
            "running",
        )


def test_global_abort_closes_only_admitted_attempt_and_defers_run(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        run = create_run(conn, "backfill", "{}", [(1, "input-a", "pending")], NOW, attempt_limit=1)
        token = claim_job(conn, 1, NOW, 60)
        probe = admit_provider(conn, 1, token, NOW)
        attempt = reserve_attempt_for_run(conn, run, 1, token, "input-a", "backfill", NOW, provider_token=probe)
        assert attempt is not None
        assert start_attempt(conn, attempt, token, NOW)
        assert open_cooldown(conn, attempt, token, probe, "rate_limited", NOW, 10, 30) == 10
        assert conn.execute("SELECT state, active_attempt_id FROM session_recap_jobs").fetchone() == ("pending", None)
        assert conn.execute("SELECT state FROM session_recap_attempts WHERE id = ?", (attempt,)).fetchone() == (
            "global_abort",
        )
        assert finalize_run(conn, run, NOW, global_abort=True)
        assert run_partitions(conn, run) == (1, 1)


def test_run_reservation_rejects_limit_without_a_live_unowned_attempt(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        upsert_job(conn, 2, "input-a", "end", NOW)
        run = create_run(
            conn,
            "backfill",
            "{}",
            [(1, "input-a", "pending"), (2, "input-a", "pending")],
            NOW,
            attempt_limit=1,
        )
        one, two = claim_job(conn, 1, NOW, 60), claim_job(conn, 2, NOW, 60)
        attempt = reserve_attempt_for_run(conn, run, 1, one, "input-a", "backfill", NOW)
        assert attempt is not None
        probe = admit_provider(conn, 2, two, NOW)
        assert reserve_attempt_for_run(conn, run, 2, two, "input-a", "backfill", NOW, provider_token=probe) is None
        assert conn.execute(
            "SELECT state, active_attempt_id FROM session_recap_jobs WHERE session_id = 2"
        ).fetchone() == ("claimed", None)
        assert conn.execute(
            "SELECT COUNT(*) FROM session_recap_attempts WHERE job_session_id = 2 AND state IN ('reserved', 'running')"
        ).fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM session_recap_attempts").fetchone() == (1,)
        assert conn.execute("SELECT probe_active FROM session_recap_provider_health").fetchone() == (0,)
        assert finalize_run(conn, run, NOW)
        rows = conn.execute(
            "SELECT session_id, input_hash, final_disposition FROM session_recap_run_candidates WHERE run_id = ? ORDER BY session_id, input_hash",
            (run,),
        ).fetchall()
        assert rows == [(1, "input-a", "attempted"), (2, "input-a", "already_running")]
        empty = create_run(conn, "backfill", "{}", [(1, "input-a", "already_current")], NOW, attempt_limit=0)
        assert finalize_run(conn, empty, NOW)
        assert run_partitions(conn, empty) == (1, 1)


def test_retention_preserves_whole_retained_lineages_and_closed_runs(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        old_ids = []
        for timestamp in ("2025-01-01T00:00:00Z", "2025-01-01T00:00:01Z"):
            token = claim_job(conn, 1, timestamp, 60)
            attempt = reserve_attempt(conn, 1, token, "input-a", "test", timestamp)
            old_ids.append(attempt)
            assert complete_attempt(conn, attempt, token, "timeout", timestamp)
        assert retention_candidates(conn, "2026-01-01T00:00:00Z", limit=10) == []
        assert targeted_retry(conn, 1, NOW)
        token = claim_job(conn, 1, NOW, 60)
        attempt = reserve_attempt(conn, 1, token, "input-a", "test", NOW)
        assert complete_attempt(conn, attempt, token, "succeeded", NOW)
        assert retention_candidates(conn, "2026-01-01T00:00:00Z", limit=10) == old_ids
        run = create_run(conn, "backfill", "{}", [], "2025-01-01T00:00:00Z", attempt_limit=0)
        assert finalize_run(conn, run, "2025-01-01T00:00:01Z")
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM session_recap_runs WHERE started_at < ? AND state IN ('complete', 'incomplete') ORDER BY started_at, id LIMIT ?",
            ("2026-01-01T00:00:00Z", 1),
        ).fetchall()
        assert any("idx_recap_runs_started" in row[3] for row in plan)
        assert run_retention_candidates(conn, "2026-01-01T00:00:00Z", limit=1) == [run]


def test_retention_keeps_retained_terminal_ancestors_but_prunes_old_lineages(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        old_ids = []
        for timestamp in ("2025-01-01T00:00:00Z", "2025-01-01T00:00:01Z"):
            token = claim_job(conn, 1, timestamp, 60)
            attempt = reserve_attempt(conn, 1, token, "input-a", "test", timestamp)
            old_ids.append(attempt)
            assert complete_attempt(conn, attempt, token, "timeout", timestamp)
        assert targeted_retry(conn, 1, NOW)
        retained_ids = []
        for timestamp in ("2025-01-02T00:00:00Z", "2025-01-02T00:00:01Z"):
            token = claim_job(conn, 1, timestamp, 60)
            attempt = reserve_attempt(conn, 1, token, "input-a", "test", timestamp)
            retained_ids.append(attempt)
            assert complete_attempt(conn, attempt, token, "timeout", timestamp)
        assert retention_candidates(conn, "2026-01-01T00:00:00Z", limit=10) == old_ids
        assert not set(retained_ids) & set(retention_candidates(conn, "2026-01-01T00:00:00Z", limit=10))


def test_retention_keeps_uncertain_cleanup_artifacts_until_persisted_proof(tmp_path):
    with get_connection(_connection(tmp_path)) as conn:
        upsert_job(conn, 1, "input-a", "end", NOW)
        failed_attempt = conn.execute(
            "INSERT INTO session_recap_attempts(session_id, job_session_id, input_hash, input_contract_version, "
            "policy_version, recap_contract_version, claim_token, trigger, state, cleanup_state, created_at, "
            "finished_at, retry_lineage) VALUES (1, 1, 'old-input', 1, 1, 2, 0, 'test', 'cleanup_failed', "
            "'uncertain', '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z', 0) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO session_recap_quarantine(attempt_id, path, nonce, cleanup_state, created_at) "
            "VALUES (?, 'packet.json', 'nonce', 'uncertain', '2025-01-01T00:00:00Z')",
            (failed_attempt,),
        )
        upsert_job(conn, 1, "input-b", "end", "2025-01-01T00:00:01Z")
        token = claim_job(conn, 1, NOW, 60)
        replacement = _attempt(conn, 1, token, "input-b")
        assert complete_attempt(conn, replacement, token, "succeeded", NOW)

        assert retention_candidates(conn, "2026-01-01T00:00:00Z", limit=10) == []
        assert prune_retention(conn, "2026-01-01T00:00:00Z", limit=10) == (0, 0)
        assert conn.execute(
            "SELECT COUNT(*) FROM session_recap_attempts WHERE id = ?", (failed_attempt,)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM session_recap_quarantine WHERE attempt_id = ?", (failed_attempt,)
        ).fetchone() == (1,)

        conn.execute(
            "UPDATE session_recap_attempts SET cleanup_state = 'verified_removed' WHERE id = ?", (failed_attempt,)
        )
        assert retention_candidates(conn, "2026-01-01T00:00:00Z", limit=10) == [failed_attempt]
        assert prune_retention(conn, "2026-01-01T00:00:00Z", limit=10) == (1, 0)
        assert conn.execute(
            "SELECT COUNT(*) FROM session_recap_quarantine WHERE attempt_id = ?", (failed_attempt,)
        ).fetchone() == (0,)
