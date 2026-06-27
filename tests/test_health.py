"""Tests for ccrecall.health — probes, sidecar helpers, snooze ledger, alert builder."""

import json
import sqlite3

from whenever import Instant

from ccrecall.health import (
    ALERT_CANT_PERSIST,
    ALERT_EMBEDDINGS_FAILING,
    RECALL_CAVEAT_COVERAGE_THRESHOLD,
    ProbeResult,
    _read_snooze_ledger,
    _write_snooze_ledger,
    build_alert_block,
    clear_embedding_failure,
    evaluate_alerts,
    probe_db,
    probe_filesystem,
    read_embedding_status,
    record_embedding_failure,
)


class TestProbeFilesystem:
    """Filesystem writability probe — O_CREAT|O_TRUNC, not O_EXCL."""

    def test_returns_ok_on_writable_dir(self, tmp_path):
        """Writable dir → ok result and marker cleaned up."""
        marker = tmp_path / ".write-probe"
        result = probe_filesystem(marker_path=marker)
        assert result.ok is True
        assert not marker.exists(), "marker must be removed after probe"

    def test_returns_fault_on_unwritable_dir(self, tmp_path):
        """Unwritable dir → fault with a reason string."""
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o555)
        try:
            marker = readonly_dir / ".write-probe"
            result = probe_filesystem(marker_path=marker)
            assert result.ok is False
            assert result.reason, "fault result must carry a reason"
        finally:
            readonly_dir.chmod(0o755)

    def test_idempotent_with_preexisting_marker(self, tmp_path):
        """Pre-existing marker is overwritten (O_TRUNC) — probe succeeds idempotently."""
        marker = tmp_path / ".write-probe"
        marker.write_bytes(b"stale content from a prior crash")
        result = probe_filesystem(marker_path=marker)
        assert result.ok is True
        assert not marker.exists()

    def test_two_consecutive_calls_both_succeed(self, tmp_path):
        """O_CREAT|O_TRUNC is idempotent: running probe twice in a row succeeds both times."""
        marker = tmp_path / ".write-probe"
        r1 = probe_filesystem(marker_path=marker)
        r2 = probe_filesystem(marker_path=marker)
        assert r1.ok is True
        assert r2.ok is True

    def test_fault_distinguishable_from_ok(self, tmp_path):
        """Fault result has ok=False and non-empty reason; ok result has ok=True and empty reason."""
        marker = tmp_path / ".write-probe"
        ok = probe_filesystem(marker_path=marker)
        assert ok.ok is True
        assert ok.reason == ""

        readonly_dir = tmp_path / "ro"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o555)
        try:
            fault = probe_filesystem(marker_path=readonly_dir / ".write-probe")
            assert fault.ok is False
            assert fault.reason != ""
        finally:
            readonly_dir.chmod(0o755)


class TestProbeDb:
    """DB writability probe — BEGIN IMMEDIATE / ROLLBACK."""

    def test_returns_ok_for_healthy_conn(self):
        """Healthy in-memory conn → ok."""
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA journal_mode=WAL")
        result = probe_db(conn)
        assert result.ok is True
        conn.close()

    def test_returns_ok_for_locked_error(self):
        """'database is locked' OperationalError → ok (concurrency, not a fault)."""

        class _LockedConn:
            def execute(self, sql):
                if "BEGIN" in sql.upper():
                    raise sqlite3.OperationalError("database is locked")

        result = probe_db(_LockedConn())
        assert result.ok is True

    def test_returns_ok_for_busy_error(self):
        """'database is busy' OperationalError → ok (treated as lock contention)."""

        class _BusyConn:
            def execute(self, sql):
                if "BEGIN" in sql.upper():
                    raise sqlite3.OperationalError("unable to acquire lock — database is busy")

        result = probe_db(_BusyConn())
        assert result.ok is True

    def test_returns_fault_for_readonly_error(self):
        """Read-only OperationalError → fault."""

        class _ReadOnlyConn:
            def execute(self, sql):
                if "BEGIN" in sql.upper():
                    raise sqlite3.OperationalError("attempt to write a readonly database")

        result = probe_db(_ReadOnlyConn())
        assert result.ok is False
        assert result.reason

    def test_returns_fault_for_conn_none(self):
        """conn=None → fault with a reason string."""
        result = probe_db(None)
        assert result.ok is False
        assert result.reason, "fault result for conn=None must carry a reason"

    def test_fault_reason_distinguishable_from_ok(self):
        """ok result has empty reason; fault result has non-empty reason."""
        ok = probe_db(sqlite3.connect(":memory:"))
        fault = probe_db(None)
        assert ok.ok is True
        assert ok.reason == ""
        assert fault.ok is False
        assert fault.reason != ""

    def test_sqlite_error_other_than_operational_is_fault(self):
        """Any sqlite3.Error that is not a lock/busy OperationalError → fault."""

        class _DatabaseError:
            def execute(self, sql):
                if "BEGIN" in sql.upper():
                    raise sqlite3.DatabaseError("disk I/O error")

        result = probe_db(_DatabaseError())
        assert result.ok is False


class TestEmbeddingStatus:
    """embedding-status.json sidecar helpers."""

    def test_record_and_read_round_trip(self, tmp_path):
        """record_embedding_failure writes reason + since; read_embedding_status returns them."""
        path = tmp_path / "embedding-status.json"
        record_embedding_failure("model unavailable", path=path)
        data = read_embedding_status(path=path)
        assert data is not None
        assert data["reason"] == "model unavailable"
        assert "since" in data

    def test_clear_removes_status(self, tmp_path):
        """clear_embedding_failure removes the sidecar; read returns None afterward."""
        path = tmp_path / "embedding-status.json"
        record_embedding_failure("vec unavailable", path=path)
        clear_embedding_failure(path=path)
        assert read_embedding_status(path=path) is None

    def test_read_missing_file_returns_none(self, tmp_path):
        """Missing embedding-status.json → None (no error)."""
        path = tmp_path / "embedding-status.json"
        assert read_embedding_status(path=path) is None

    def test_read_malformed_json_returns_none(self, tmp_path):
        """Malformed JSON → None."""
        path = tmp_path / "embedding-status.json"
        path.write_text("{bad json}")
        assert read_embedding_status(path=path) is None

    def test_read_non_dict_json_returns_none(self, tmp_path):
        """Non-dict JSON (array, string) → None."""
        path = tmp_path / "embedding-status.json"
        path.write_text(json.dumps([1, 2, 3]))
        assert read_embedding_status(path=path) is None

    def test_since_is_parseable_iso_timestamp(self, tmp_path):
        """The 'since' field is a parseable ISO timestamp."""
        path = tmp_path / "embedding-status.json"
        record_embedding_failure("test reason", path=path)
        data = read_embedding_status(path=path)
        assert data is not None
        # Must not raise
        Instant.parse_iso(data["since"])

    def test_overwrite_updates_reason(self, tmp_path):
        """Recording a new failure updates the reason."""
        path = tmp_path / "embedding-status.json"
        record_embedding_failure("first failure", path=path)
        record_embedding_failure("second failure", path=path)
        data = read_embedding_status(path=path)
        assert data is not None
        assert data["reason"] == "second failure"


class TestSnoozeLedger:
    """Snooze ledger: evaluate_alerts and atomic write helpers."""

    def test_first_call_fires_all_active_keys(self, tmp_path):
        """No ledger → all active keys fire on first call."""
        path = tmp_path / "alert-snooze.json"
        result = evaluate_alerts({"a", "b"}, snooze_hours=24, snooze_path=path)
        assert set(result) == {"a", "b"}

    def test_second_call_within_window_suppresses(self, tmp_path):
        """Within the snooze window → empty result (all suppressed)."""
        path = tmp_path / "alert-snooze.json"
        evaluate_alerts({"a"}, snooze_hours=24, snooze_path=path)
        result = evaluate_alerts({"a"}, snooze_hours=24, snooze_path=path)
        assert result == []

    def test_fire_after_window_lapses(self, tmp_path):
        """A stale ledger entry (beyond the window) fires again."""
        path = tmp_path / "alert-snooze.json"
        stale_iso = Instant.now().subtract(hours=25).format_iso()
        _write_snooze_ledger(path, {"a": stale_iso})
        result = evaluate_alerts({"a"}, snooze_hours=24, snooze_path=path)
        assert "a" in result

    def test_auto_clear_drops_inactive_key(self, tmp_path):
        """Key absent from active_keys is dropped from the ledger (auto-clear FR#9)."""
        path = tmp_path / "alert-snooze.json"
        evaluate_alerts({"a", "b"}, snooze_hours=24, snooze_path=path)
        # "b" condition clears
        evaluate_alerts({"a"}, snooze_hours=24, snooze_path=path)
        ledger = _read_snooze_ledger(path)
        assert "b" not in ledger
        assert "a" in ledger

    def test_auto_clear_reset_fires_immediately_on_recurrence(self, tmp_path):
        """AC#5: after auto-clear, key's recurrence fires immediately (no stale record)."""
        path = tmp_path / "alert-snooze.json"
        # Fire both
        evaluate_alerts({"a", "b"}, snooze_hours=24, snooze_path=path)
        # "b" condition clears
        evaluate_alerts({"a"}, snooze_hours=24, snooze_path=path)
        # "b" condition returns — must fire immediately (no suppression from stale record)
        result = evaluate_alerts({"b"}, snooze_hours=24, snooze_path=path)
        assert "b" in result

    def test_unwritable_dir_still_fires_alert(self, tmp_path):
        """AC#6 / FR#10: unwritable runtime dir → alert fires every evaluation."""
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o555)
        try:
            path = readonly_dir / "alert-snooze.json"
            result = evaluate_alerts({"a"}, snooze_hours=24, snooze_path=path)
            assert "a" in result, "alert must fire even when the ledger cannot be written"
        finally:
            readonly_dir.chmod(0o755)

    def test_atomic_write_no_tmp_orphan(self, tmp_path):
        """Successful write leaves no .tmp files (FR#8)."""
        path = tmp_path / "alert-snooze.json"
        _write_snooze_ledger(path, {"key": "val"})
        leftover = list(tmp_path.glob("*.tmp"))
        assert leftover == []

    def test_atomic_write_last_writer_wins(self, tmp_path):
        """Sequential writes: last write wins and file is valid JSON (FR#8)."""
        path = tmp_path / "alert-snooze.json"
        _write_snooze_ledger(path, {"a": "t1"})
        _write_snooze_ledger(path, {"b": "t2"})
        data = json.loads(path.read_text())
        assert data == {"b": "t2"}

    def test_small_snooze_window_fires_when_expired(self, tmp_path):
        """Small snooze window: stale record exceeds the window and fires."""
        path = tmp_path / "alert-snooze.json"
        stale_iso = Instant.now().subtract(seconds=10).format_iso()
        _write_snooze_ledger(path, {"x": stale_iso})
        # 0.001 hours = 3.6 seconds — 10s ago exceeds this window
        result = evaluate_alerts({"x"}, snooze_hours=0.001, snooze_path=path)
        assert "x" in result

    def test_fire_once_suppress_within_window_refire_after(self, tmp_path):
        """AC#4: fire-once → suppress-within-window → fire-after-window."""
        path = tmp_path / "alert-snooze.json"

        # First: fires
        r1 = evaluate_alerts({"k"}, snooze_hours=24, snooze_path=path)
        assert "k" in r1, "first call must fire"

        # Within window: suppressed
        r2 = evaluate_alerts({"k"}, snooze_hours=24, snooze_path=path)
        assert "k" not in r2, "second call within window must be suppressed"

        # After window: overwrite with stale entry
        stale_iso = Instant.now().subtract(hours=25).format_iso()
        _write_snooze_ledger(path, {"k": stale_iso})
        r3 = evaluate_alerts({"k"}, snooze_hours=24, snooze_path=path)
        assert "k" in r3, "call after window lapses must fire again"

    def test_unwritable_dir_fires_every_evaluation(self, tmp_path):
        """AC#6: consecutive evaluations with unwritable dir each fire (degrade to re-tell)."""
        readonly_dir = tmp_path / "readonly2"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o555)
        try:
            path = readonly_dir / "alert-snooze.json"
            r1 = evaluate_alerts({"a"}, snooze_hours=24, snooze_path=path)
            r2 = evaluate_alerts({"a"}, snooze_hours=24, snooze_path=path)
            assert "a" in r1
            assert "a" in r2, "each session must re-fire when snooze cannot be persisted"
        finally:
            readonly_dir.chmod(0o755)

    def test_snooze_hours_zero_fires_every_time(self, tmp_path):
        """snooze_hours=0 means every call fires (0s window — always expired)."""
        path = tmp_path / "alert-snooze.json"
        r1 = evaluate_alerts({"a"}, snooze_hours=0, snooze_path=path)
        r2 = evaluate_alerts({"a"}, snooze_hours=0, snooze_path=path)
        assert "a" in r1
        assert "a" in r2


class TestBlockBuilder:
    """Alert-block builder — AC#12: not a bare heading."""

    def test_empty_keys_returns_empty_string(self):
        """No active alerts → empty string."""
        result = build_alert_block([])
        assert result == ""

    def test_single_alert_has_heading(self):
        """Block starts with ## ⚠ heading."""
        result = build_alert_block([ALERT_CANT_PERSIST])
        assert "## ⚠" in result

    def test_cant_persist_block_contains_cause(self):
        """cant_persist block embeds the fault reason as cause."""
        result = build_alert_block([ALERT_CANT_PERSIST], fault_reason="Permission denied")
        assert "Permission denied" in result

    def test_cant_persist_block_has_action_text(self):
        """cant_persist block contains a suggested action."""
        result = build_alert_block([ALERT_CANT_PERSIST])
        lower = result.lower()
        assert "suggested action" in lower or "disk space" in lower or "permission" in lower

    def test_cant_persist_block_has_relay_instruction(self):
        """cant_persist block instructs relay without hard-coded prominence."""
        result = build_alert_block([ALERT_CANT_PERSIST])
        lower = result.lower()
        assert "surface" in lower, "must instruct to surface to user"
        assert "hard-code" in lower or "hard code" in lower, "must say not to hard-code prominence"

    def test_embeddings_block_contains_cause(self):
        """embeddings_failing block embeds the embedding reason as cause."""
        result = build_alert_block([ALERT_EMBEDDINGS_FAILING], embedding_reason="sqlite-vec not found")
        assert "sqlite-vec not found" in result

    def test_embeddings_block_has_action_text(self):
        """embeddings_failing block contains a suggested action."""
        result = build_alert_block([ALERT_EMBEDDINGS_FAILING])
        lower = result.lower()
        assert "suggested action" in lower or "sqlite-vec" in lower or "install" in lower

    def test_embeddings_block_has_relay_instruction(self):
        """embeddings_failing block instructs relay without hard-coded prominence."""
        result = build_alert_block([ALERT_EMBEDDINGS_FAILING])
        lower = result.lower()
        assert "surface" in lower
        assert "hard-code" in lower or "hard code" in lower

    def test_multiple_alerts_single_heading(self):
        """FR#13: multiple alerts → exactly one ## ⚠ heading."""
        result = build_alert_block([ALERT_CANT_PERSIST, ALERT_EMBEDDINGS_FAILING])
        assert result.count("## ⚠") == 1

    def test_multiple_alerts_both_causes_present(self):
        """Both alert causes appear in the combined block."""
        result = build_alert_block(
            [ALERT_CANT_PERSIST, ALERT_EMBEDDINGS_FAILING],
            fault_reason="disk full",
            embedding_reason="model missing",
        )
        assert "disk full" in result
        assert "model missing" in result

    def test_ac12_block_is_not_bare_heading(self):
        """AC#12: block has substantial prose beyond the heading."""
        result = build_alert_block([ALERT_CANT_PERSIST], fault_reason="disk full")
        lines = [ln for ln in result.split("\n") if ln.strip()]
        assert len(lines) >= 2, "must have at least heading + prose"
        prose = " ".join(lines[1:])
        assert len(prose) > 50, "prose must be substantial (cause + action + relay)"

    def test_block_contains_cause_keyword(self):
        """Block prose includes 'cause' to convey why the alert fired."""
        result = build_alert_block([ALERT_CANT_PERSIST])
        assert "cause" in result.lower()

    def test_default_fault_reason_when_not_provided(self):
        """Block contains a fallback cause description when fault_reason is empty."""
        result = build_alert_block([ALERT_CANT_PERSIST])
        # Should name some likely cause even without an explicit reason
        lower = result.lower()
        assert "disk" in lower or "permission" in lower or "unavailable" in lower


class TestConstants:
    """Module-level constants are correct."""

    def test_recall_caveat_threshold_is_0_95(self):
        assert RECALL_CAVEAT_COVERAGE_THRESHOLD == 0.95

    def test_alert_keys_are_distinct_nonempty_strings(self):
        assert isinstance(ALERT_CANT_PERSIST, str)
        assert isinstance(ALERT_EMBEDDINGS_FAILING, str)
        assert ALERT_CANT_PERSIST != ALERT_EMBEDDINGS_FAILING
        assert ALERT_CANT_PERSIST
        assert ALERT_EMBEDDINGS_FAILING

    def test_probe_result_frozen_dataclass(self):
        """ProbeResult is frozen and carries ok + reason fields."""
        ok = ProbeResult(ok=True, reason="")
        fault = ProbeResult(ok=False, reason="disk full")
        assert ok.ok is True
        assert fault.reason == "disk full"
