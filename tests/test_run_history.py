"""Tests for the per-database run history and unfixable-sessions sidecars."""

import json
import sqlite3
import subprocess
import sys

import pytest
from conftest import job_record, leave_killed_lock, locked_error
from whenever import Instant

from ccrecall import job_lock, run_history
from ccrecall.config import JOB_KEY_BACKFILL_EMBEDDINGS
from ccrecall.run_history import (
    KIND_BACKFILL,
    KIND_IMPORT,
    KIND_JOB_KEYS,
    KIND_SYNC,
    OUTCOME_FAILED,
    OUTCOME_OK,
    OUTCOME_PARTIAL,
    REASON_DATABASE_BUSY,
    REASON_ERROR,
    REASON_MODEL_UNAVAILABLE,
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
    with record_run(db_path, KIND_SYNC) as recorder:
        recorder.finish(OUTCOME_OK)

    entry = read_runs(db_path)[KIND_SYNC]
    assert entry["last"]["outcome"] == OUTCOME_OK
    assert entry["last_ok"] == entry["last"]
    assert entry["last"]["started_at"] <= entry["last"]["finished_at"]


def test_failure_keeps_the_previous_success(db_path):
    with record_run(db_path, KIND_IMPORT) as recorder:
        recorder.finish(OUTCOME_OK)
    first_ok = read_runs(db_path)[KIND_IMPORT]["last_ok"]

    with record_run(db_path, KIND_IMPORT) as recorder:
        recorder.finish(OUTCOME_PARTIAL, REASON_DATABASE_BUSY)

    entry = read_runs(db_path)[KIND_IMPORT]
    assert entry["last"]["outcome"] == OUTCOME_PARTIAL
    assert entry["last"]["reason"] == REASON_DATABASE_BUSY
    assert entry["last_ok"] == first_ok


def test_scoped_success_never_advances_last_ok(db_path):
    with record_run(db_path, KIND_BACKFILL, advances_ok=False) as recorder:
        recorder.finish(OUTCOME_OK)

    entry = read_runs(db_path)[KIND_BACKFILL]
    assert entry["last"]["outcome"] == OUTCOME_OK
    assert entry["last_ok"] is None


def test_run_that_never_sets_an_outcome_records_failed(db_path):
    with record_run(db_path, KIND_SYNC):
        pass

    entry = read_runs(db_path)[KIND_SYNC]
    assert entry["last"]["outcome"] == OUTCOME_FAILED
    assert entry["last"]["reason"] == REASON_ERROR
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


def test_each_kind_keeps_its_own_record(db_path):
    with record_run(db_path, KIND_SYNC) as recorder:
        recorder.finish(OUTCOME_OK)
    with record_run(db_path, KIND_BACKFILL) as recorder:
        recorder.finish(OUTCOME_OK)

    assert set(read_runs(db_path)) == {KIND_SYNC, KIND_BACKFILL}


def test_unparseable_file_is_rewritten_with_only_the_current_kind(db_path):
    runs_path(db_path).write_text("{not json")

    with record_run(db_path, KIND_IMPORT) as recorder:
        recorder.finish(OUTCOME_OK)

    assert set(read_runs(db_path)) == {KIND_IMPORT}


def test_recording_failure_never_fails_the_run(db_path, caplog):
    runs_path(db_path).mkdir()  # a directory where the file should be

    with record_run(db_path, KIND_SYNC) as recorder:
        recorder.finish(OUTCOME_OK)

    assert "could not record the run" in caplog.text


def test_runs_against_one_database_never_appear_in_another(tmp_path):
    with record_run(tmp_path / "a.db", KIND_SYNC) as recorder:
        recorder.finish(OUTCOME_OK)

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


class TestKindJobKeys:
    """Each kind's unfinished run is read from its own job lock (design 020, D4)."""

    def test_held_backfill_lock_reads_as_a_running_backfill(self, hold_job_lock):
        pid = hold_job_lock(
            JOB_KEY_BACKFILL_EMBEDDINGS,
            record=job_record(JOB_KEY_BACKFILL_EMBEDDINGS, age_seconds=60, scope={"days": None, "limit": 5}),
        )

        state = job_lock.probe(KIND_JOB_KEYS[KIND_BACKFILL])

        assert state is not None
        assert (state.running, state.pid, state.scope) == (True, pid, {"days": None, "limit": 5})

    def test_record_left_in_a_free_lock_is_killed_and_dated_by_the_record(self):
        leave_killed_lock(JOB_KEY_BACKFILL_EMBEDDINGS, pid=4242, age_seconds=3600)

        state = job_lock.probe(KIND_JOB_KEYS[KIND_BACKFILL])

        assert state is not None
        assert (state.running, state.pid) == (False, 4242)
        assert state.started_at is not None
        assert abs((Instant.now() - state.started_at).total("seconds") - 3600) < 5


def test_module_imports_no_vector_or_embedding_modules():
    """Recording runs on the hook paths, so it must not pull in the vector stack."""
    code = (
        "import sys, ccrecall.run_history; "
        "bad = [m for m in ('sqlite_vec', 'fastembed', 'onnxruntime', 'numpy') if m in sys.modules]; "
        "print(','.join(bad))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_run_history_module_exposes_every_kind_with_a_job_lock():
    assert set(run_history.KIND_JOB_KEYS) == {KIND_IMPORT, KIND_SYNC, KIND_BACKFILL}
