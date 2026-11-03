"""Tests for the per-database run history and unfixable-sessions sidecars."""

import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest
from conftest import locked_error

from ccrecall import run_history
from ccrecall.config import PID_KEY_BACKFILL_EMBEDDINGS, pid_file_path
from ccrecall.run_history import (
    KIND_BACKFILL,
    KIND_IMPORT,
    KIND_SYNC,
    OUTCOME_FAILED,
    OUTCOME_OK,
    OUTCOME_PARTIAL,
    REASON_DATABASE_BUSY,
    REASON_ERROR,
    REASON_MODEL_UNAVAILABLE,
    read_marker,
    read_runs,
    read_unfixable,
    record_run,
    runs_path,
    unrepairable_path,
    write_unfixable,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "conversations.db"


def test_sidecars_live_beside_the_database(db_path):
    assert runs_path(db_path) == db_path.with_name("conversations.db.runs.json")
    assert unrepairable_path(db_path) == db_path.with_name("conversations.db.unrepairable.json")


def test_successful_run_sets_last_and_last_ok(db_path):
    with record_run(db_path, KIND_SYNC):
        pass

    entry = read_runs(db_path)[KIND_SYNC]
    assert entry["last"]["outcome"] == OUTCOME_OK
    assert entry["last_ok"] == entry["last"]
    assert entry["last"]["started_at"] <= entry["last"]["finished_at"]


def test_failure_keeps_the_previous_success(db_path):
    with record_run(db_path, KIND_IMPORT):
        pass
    first_ok = read_runs(db_path)[KIND_IMPORT]["last_ok"]

    with record_run(db_path, KIND_IMPORT) as recorder:
        recorder.finish(OUTCOME_PARTIAL, REASON_DATABASE_BUSY)

    entry = read_runs(db_path)[KIND_IMPORT]
    assert entry["last"]["outcome"] == OUTCOME_PARTIAL
    assert entry["last"]["reason"] == REASON_DATABASE_BUSY
    assert entry["last_ok"] == first_ok


def test_scoped_success_never_advances_last_ok(db_path):
    with record_run(db_path, KIND_BACKFILL, advances_ok=False):
        pass

    entry = read_runs(db_path)[KIND_BACKFILL]
    assert entry["last"]["outcome"] == OUTCOME_OK
    assert entry["last_ok"] is None


def test_exception_records_failed_and_reraises(db_path):
    with pytest.raises(RuntimeError), record_run(db_path, KIND_SYNC):
        raise RuntimeError("boom")

    last = read_runs(db_path)[KIND_SYNC]["last"]
    assert (last["outcome"], last["reason"]) == (OUTCOME_FAILED, REASON_ERROR)


def test_exception_keeps_a_reason_the_run_set(db_path):
    def run() -> None:
        with record_run(db_path, KIND_BACKFILL) as recorder:
            recorder.reason = REASON_MODEL_UNAVAILABLE
            raise RuntimeError("model broke")

    with pytest.raises(RuntimeError):
        run()

    assert read_runs(db_path)[KIND_BACKFILL]["last"]["reason"] == REASON_MODEL_UNAVAILABLE


def test_lock_timeout_records_database_busy(db_path):
    with pytest.raises(sqlite3.OperationalError), record_run(db_path, KIND_IMPORT):
        raise locked_error()

    assert read_runs(db_path)[KIND_IMPORT]["last"]["reason"] == REASON_DATABASE_BUSY


def test_ctrl_c_records_nothing(db_path):
    with pytest.raises(KeyboardInterrupt), record_run(db_path, KIND_BACKFILL):
        raise KeyboardInterrupt

    assert not runs_path(db_path).exists()


def test_system_exit_records_the_outcome_the_run_set(db_path):
    def run() -> None:
        with record_run(db_path, KIND_SYNC) as recorder:
            recorder.finish(OUTCOME_FAILED, REASON_ERROR)
            raise SystemExit(0)

    with pytest.raises(SystemExit):
        run()

    assert read_runs(db_path)[KIND_SYNC]["last"]["outcome"] == OUTCOME_FAILED


def test_skipped_run_records_nothing(db_path):
    with record_run(db_path, KIND_SYNC) as recorder:
        recorder.skip()

    assert not runs_path(db_path).exists()


def test_each_kind_keeps_its_own_record(db_path):
    with record_run(db_path, KIND_SYNC):
        pass
    with record_run(db_path, KIND_BACKFILL):
        pass

    assert set(read_runs(db_path)) == {KIND_SYNC, KIND_BACKFILL}


def test_unparseable_file_is_rewritten_with_only_the_current_kind(db_path):
    runs_path(db_path).write_text("{not json")

    with record_run(db_path, KIND_IMPORT):
        pass

    assert set(read_runs(db_path)) == {KIND_IMPORT}


def test_recording_failure_never_fails_the_run(db_path, caplog):
    runs_path(db_path).mkdir()  # a directory where the file should be

    with record_run(db_path, KIND_SYNC):
        pass

    assert "could not record the run" in caplog.text


def test_runs_against_one_database_never_appear_in_another(tmp_path):
    with record_run(tmp_path / "a.db", KIND_SYNC):
        pass

    assert read_runs(tmp_path / "b.db") == {}


def test_completed_repair_replaces_the_unfixable_list(db_path):
    write_unfixable(db_path, {"s1": "fp1", "s2": "fp2"}, replace=True)
    write_unfixable(db_path, {"s3": "fp3"}, replace=True)

    assert read_unfixable(db_path) == {"s3": "fp3"}


def test_completed_repair_with_nothing_unfixable_removes_the_file(db_path):
    write_unfixable(db_path, {"s1": "fp1"}, replace=True)
    write_unfixable(db_path, {}, replace=True)

    assert not unrepairable_path(db_path).exists()


def test_aborted_repair_only_adds(db_path):
    write_unfixable(db_path, {"s1": "fp1"}, replace=True)
    write_unfixable(db_path, {"s2": "fp2"}, replace=False)

    assert read_unfixable(db_path) == {"s1": "fp1", "s2": "fp2"}


def test_malformed_unfixable_file_reads_as_empty(db_path):
    unrepairable_path(db_path).write_text(json.dumps({"sessions": ["not", "a", "map"]}))

    assert read_unfixable(db_path) == {}


class TestReadMarker:
    def marker(self):
        return pid_file_path(PID_KEY_BACKFILL_EMBEDDINGS)

    def test_no_marker(self):
        assert read_marker(KIND_BACKFILL) is None

    def test_live_ccrecall_holder_is_running(self, live_ccrecall_pid):
        self.marker().parent.mkdir(parents=True, exist_ok=True)
        self.marker().write_text(str(live_ccrecall_pid))

        state = read_marker(KIND_BACKFILL)

        assert state is not None
        assert (state.running, state.pid) == (True, live_ccrecall_pid)

    def test_dead_holder_is_killed_and_dated_by_the_marker(self):
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        self.marker().parent.mkdir(parents=True, exist_ok=True)
        self.marker().write_text(str(proc.pid))
        started = time.time() - 3600
        os.utime(self.marker(), (started, started))

        state = read_marker(KIND_BACKFILL)

        assert state is not None
        assert (state.running, state.pid) == (False, proc.pid)
        assert abs(state.started_at.timestamp() - started) < 2

    def test_reused_pid_running_something_else_reads_as_killed(self):
        # A live process that isn't ccrecall: the holder died and its PID was reused.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.marker().parent.mkdir(parents=True, exist_ok=True)
            self.marker().write_text(str(proc.pid))

            state = read_marker(KIND_BACKFILL)
        finally:
            proc.kill()
            proc.wait()

        assert state is not None
        assert state.running is False

    @pytest.mark.parametrize("content", ["", "garbage"])
    def test_empty_or_corrupt_marker_reads_as_no_marker(self, content):
        self.marker().parent.mkdir(parents=True, exist_ok=True)
        self.marker().write_text(content)

        assert read_marker(KIND_BACKFILL) is None


def test_module_imports_no_vector_or_embedding_modules():
    """Recording runs on the hook paths, so it must not pull in the vector stack."""
    code = (
        "import sys, ccrecall.run_history; "
        "bad = [m for m in ('sqlite_vec', 'fastembed', 'onnxruntime', 'numpy') if m in sys.modules]; "
        "print(','.join(bad))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_run_history_module_exposes_every_kind_with_a_marker():
    assert set(run_history.KIND_PID_KEYS) == {KIND_IMPORT, KIND_SYNC, KIND_BACKFILL}
