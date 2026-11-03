"""The job lock: a kernel-held flock plus the record of the run holding it (design 020, D4)."""

import json
import os
import subprocess
import sys

import pytest
from conftest import job_record, leave_killed_lock
from whenever import Instant

from ccrecall import config, job_lock

KEY = "ccrecall-test-job"
OTHER_KEY = "ccrecall-test-other-job"

# Stands in for a spawned job: adopts the handed-over lock through the real
# acquire, reports, then either releases and exits or sleeps until killed.
_CHILD_SCRIPT = """
import sys, time
from pathlib import Path
from ccrecall import config, job_lock
config.RUNTIME_DIR = Path(sys.argv[1])
lock = job_lock.acquire(sys.argv[2], scope={"repair_gaps": True})
print("adopted" if lock is not None else "skipped", flush=True)
if sys.argv[3] == "release":
    lock.release()
else:
    time.sleep(120)
"""

# Spawned as KEY's job: takes another key's lock first, then its own.
_TWO_LOCKS_CHILD_SCRIPT = """
import sys
from pathlib import Path
from ccrecall import config, job_lock
config.RUNTIME_DIR = Path(sys.argv[1])
other = job_lock.acquire(sys.argv[2])
own = job_lock.acquire(sys.argv[3])
print("other:" + ("held" if other is not None else "skipped"), flush=True)
print("own:" + ("adopted" if own is not None else "skipped"), flush=True)
"""

# Spawns argv[3:] as argv[2]'s job through job_lock.spawn and waits for it, so its output reaches ours.
_SPAWN_PARENT_SCRIPT = """
import sys
from pathlib import Path
from ccrecall import config, job_lock
config.RUNTIME_DIR = Path(sys.argv[1])
child = []
real_popen = job_lock.subprocess.Popen
job_lock.subprocess.Popen = lambda *a, **k: child.append(real_popen(*a, **k))
assert job_lock.spawn(sys.argv[3:], sys.argv[2])
child[0].wait(timeout=60)
"""

running_as_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def record_in_file() -> dict | None:
    content = job_lock.lock_path(KEY).read_text()
    return json.loads(content) if content else None


@pytest.fixture
def spawned_children():
    procs: list[subprocess.Popen] = []
    yield procs
    for proc in procs:
        proc.kill()
        proc.wait()


def spawn_child(mode: str, procs: list[subprocess.Popen]) -> subprocess.Popen:
    """Spawn the child script through job_lock.spawn and return its process."""
    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        started.append(proc)
        return proc

    argv = [sys.executable, "-c", _CHILD_SCRIPT, str(config.RUNTIME_DIR), KEY, mode]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(job_lock.subprocess, "Popen", recording_popen)
        assert job_lock.spawn(argv, KEY, {"repair_gaps": True}, stdout=subprocess.PIPE, text=True) is True
    procs.extend(started)
    return started[0]


class TestProbe:
    def test_never_run(self):
        assert job_lock.probe(KEY) is None

    def test_empty_file_is_finished(self):
        job_lock.lock_path(KEY).parent.mkdir(parents=True)
        job_lock.lock_path(KEY).write_text("")
        assert job_lock.probe(KEY) is None

    def test_held_lock_is_running_with_the_record(self, hold_job_lock):
        pid = hold_job_lock(KEY, record=job_record(KEY, age_seconds=60, scope={"days": 3}))
        state = job_lock.probe(KEY)
        assert state is not None
        assert (state.running, state.pid, state.scope) == (True, pid, {"days": 3})
        assert state.started_at is not None
        assert 50 < (Instant.now() - state.started_at).total("seconds") < 120

    def test_held_lock_with_unparseable_record_is_running_without_details(self, hold_job_lock):
        hold_job_lock(KEY, raw="not json")
        assert job_lock.probe(KEY) == job_lock.JobState(running=True, pid=None, started_at=None, scope=None)

    def test_free_lock_with_a_record_is_killed(self):
        leave_killed_lock(KEY, pid=4242, age_seconds=600, scope={"repair_gaps": True})
        state = job_lock.probe(KEY)
        assert state is not None
        assert (state.running, state.pid, state.scope) == (False, 4242, {"repair_gaps": True})
        assert state.started_at is not None

    def test_deeply_nested_record_never_raises(self, hold_job_lock):
        hold_job_lock(KEY, raw="[" * 100_000)
        assert job_lock.probe(KEY) == job_lock.JobState(running=True, pid=None, started_at=None, scope=None)

    def test_free_lock_with_unparseable_record_is_no_run(self):
        job_lock.lock_path(KEY).parent.mkdir(parents=True)
        job_lock.lock_path(KEY).write_text('{"pid": 12}')  # no started_at
        assert job_lock.probe(KEY) is None

    def test_probe_releases_its_shared_lock(self):
        leave_killed_lock(KEY)
        job_lock.probe(KEY)
        lock = job_lock.acquire(KEY)
        assert lock is not None
        lock.release()

    def test_probe_never_creates_the_file(self):
        job_lock.probe(KEY)
        assert not job_lock.lock_path(KEY).exists()

    def test_lock_path_that_is_a_directory_reads_as_no_run(self):
        job_lock.lock_path(KEY).mkdir(parents=True)
        assert job_lock.probe(KEY) is None

    @running_as_root
    def test_unreadable_lock_file_reads_as_no_run(self):
        leave_killed_lock(KEY)
        job_lock.lock_path(KEY).chmod(0)
        assert job_lock.probe(KEY) is None


class TestAcquire:
    def test_writes_its_pid_key_and_scope(self):
        lock = job_lock.acquire(KEY, scope={"days": 7, "limit": None})
        assert lock is not None
        try:
            record = record_in_file()
            assert record is not None
            assert (record["pid"], record["key"], record["scope"]) == (os.getpid(), KEY, {"days": 7, "limit": None})
            Instant.parse_iso(record["started_at"])
        finally:
            lock.release()

    def test_lock_file_is_private(self):
        with job_lock.acquire(KEY):
            assert job_lock.lock_path(KEY).stat().st_mode & 0o777 == 0o600

    def test_second_acquirer_in_this_process_is_refused(self):
        with job_lock.acquire(KEY):
            assert job_lock.acquire(KEY) is None

    def test_lock_held_by_another_process_is_refused(self, hold_job_lock):
        pid = hold_job_lock(KEY)
        assert job_lock.acquire(KEY) is None
        state = job_lock.probe(KEY)
        assert state is not None
        assert state.pid == pid

    def test_killed_record_is_taken_over(self):
        leave_killed_lock(KEY, pid=4242)
        with job_lock.acquire(KEY):
            record = record_in_file()
            assert record is not None
            assert record["pid"] == os.getpid()

    def test_release_empties_the_record_and_frees_the_lock(self):
        lock = job_lock.acquire(KEY)
        assert lock is not None
        lock.release()
        lock.release()  # idempotent
        assert job_lock.lock_path(KEY).read_text() == ""
        assert job_lock.probe(KEY) is None
        assert job_lock.lock_path(KEY).exists()  # never deleted

    @pytest.mark.parametrize("exc", [RuntimeError, KeyboardInterrupt])
    def test_leaving_with_block_on_an_error_releases(self, exc):
        with pytest.raises(exc), job_lock.acquire(KEY):
            raise exc
        assert job_lock.probe(KEY) is None
        lock = job_lock.acquire(KEY)
        assert lock is not None
        lock.release()

    @running_as_root
    def test_unopenable_lock_file_is_recreated(self):
        leave_killed_lock(KEY)
        job_lock.lock_path(KEY).chmod(0)
        with job_lock.acquire(KEY) as lock:
            assert lock is not None
            assert job_lock.lock_path(KEY).stat().st_mode & 0o777 == 0o600

    def test_removes_the_legacy_pid_marker(self):
        legacy = config.RUNTIME_DIR / f".pid-{KEY}"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("12345")
        with job_lock.acquire(KEY):
            assert not legacy.exists()

    def test_handover_variable_for_another_file_falls_back_and_is_left_for_its_key(self, tmp_path, monkeypatch):
        other = tmp_path / "other"
        other.write_text("")
        fd = os.open(other, os.O_RDONLY)
        try:
            monkeypatch.setenv(job_lock.LOCK_FD_ENV, str(fd))
            with job_lock.acquire(KEY) as lock:
                assert lock is not None
                assert lock.fd != fd
            assert os.environ[job_lock.LOCK_FD_ENV] == str(fd)
        finally:
            os.close(fd)

    def test_handover_variable_naming_a_closed_descriptor_is_consumed(self, tmp_path, monkeypatch):
        fd = os.open(tmp_path / "gone", os.O_RDWR | os.O_CREAT)
        os.close(fd)
        monkeypatch.setenv(job_lock.LOCK_FD_ENV, str(fd))
        with job_lock.acquire(KEY) as lock:
            assert lock is not None
        assert job_lock.LOCK_FD_ENV not in os.environ

    def test_unusable_handover_variable_falls_back(self, monkeypatch):
        monkeypatch.setenv(job_lock.LOCK_FD_ENV, "not-a-number")
        with job_lock.acquire(KEY) as lock:
            assert lock is not None
        assert job_lock.LOCK_FD_ENV not in os.environ


class TestScope:
    def test_import_scope_records_the_resolved_database(self, tmp_path):
        assert job_lock.import_scope(repair_gaps=True, db=tmp_path / "a" / ".." / "x.db") == {
            "repair_gaps": True,
            "db": str((tmp_path / "x.db").resolve()),
        }

    def test_backfill_scope_records_days_limit_and_database(self, tmp_path):
        assert job_lock.backfill_scope(days=3, limit=None, db=tmp_path / "x.db") == {
            "days": 3,
            "limit": None,
            "db": str((tmp_path / "x.db").resolve()),
        }

    def test_readers_count_only_a_running_job_on_this_database(self, tmp_path):
        db = tmp_path / "x.db"
        running = job_lock.JobState(True, 1, Instant.now(), job_lock.import_scope(repair_gaps=True, db=db))
        assert job_lock.repairs_gaps(running, db)
        assert not job_lock.repairs_gaps(running, tmp_path / "other.db")
        assert not job_lock.repairs_gaps(job_lock.JobState(False, 1, Instant.now(), running.scope), db)
        assert not job_lock.repairs_gaps(job_lock.JobState(True, None, None, None), db)
        assert not job_lock.repairs_gaps(None, db)

    @pytest.mark.parametrize(("days", "limit", "drains"), [(None, None, True), (7, None, False), (None, 5, False)])
    def test_drains_backlog_only_when_unscoped(self, tmp_path, days, limit, drains):
        db = tmp_path / "x.db"
        state = job_lock.JobState(True, 1, Instant.now(), job_lock.backfill_scope(days=days, limit=limit, db=db))
        assert job_lock.drains_backlog(state, db) is drains


class TestSpawn:
    def test_hands_the_lock_to_the_child_without_a_gap(self, spawned_children):
        child = spawn_child("sleep", spawned_children)
        # Held from the moment spawn returns, whether or not the child has adopted yet.
        state = job_lock.probe(KEY)
        assert state is not None
        assert state.running
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "adopted"
        state = job_lock.probe(KEY)
        assert state is not None
        assert (state.running, state.pid, state.scope) == (True, child.pid, {"repair_gaps": True})
        assert job_lock.acquire(KEY) is None

    def test_parent_keeps_no_copy_of_the_lock(self, spawned_children):
        child = spawn_child("release", spawned_children)
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "adopted"
        child.wait(timeout=30)
        assert job_lock.probe(KEY) is None
        with job_lock.acquire(KEY) as lock:
            assert lock is not None

    def test_child_killed_leaves_a_killed_record(self, spawned_children):
        child = spawn_child("sleep", spawned_children)
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "adopted"
        child.kill()
        child.wait()
        state = job_lock.probe(KEY)
        assert state is not None
        assert (state.running, state.pid) == (False, child.pid)

    def test_spawn_record_names_no_pid_before_the_child_adopts(self, monkeypatch):
        seen: list[dict | None] = []
        monkeypatch.setattr(job_lock.subprocess, "Popen", lambda *_a, **_k: seen.append(record_in_file()))
        assert job_lock.spawn(["true"], KEY, {"repair_gaps": True}) is True
        assert seen[0] is not None
        assert (seen[0]["pid"], seen[0]["scope"]) == (None, {"repair_gaps": True})

    def test_skips_when_the_job_is_running(self, hold_job_lock, monkeypatch):
        hold_job_lock(KEY)
        popen_calls: list = []
        monkeypatch.setattr(job_lock.subprocess, "Popen", lambda *a, **k: popen_calls.append(a))
        assert job_lock.spawn(["true"], KEY) is False
        assert popen_calls == []

    def test_child_that_takes_another_lock_first_still_adopts_its_own(self):
        child = [sys.executable, "-c", _TWO_LOCKS_CHILD_SCRIPT, str(config.RUNTIME_DIR), OTHER_KEY, KEY]
        result = subprocess.run(
            [sys.executable, "-c", _SPAWN_PARENT_SCRIPT, str(config.RUNTIME_DIR), KEY, *child],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        assert result.stdout.split() == ["other:held", "own:adopted"]

    def test_failed_spawn_leaves_no_record(self):
        with pytest.raises(FileNotFoundError):
            job_lock.spawn(["/nonexistent/ccrecall-no-such-binary"], KEY)
        assert job_lock.probe(KEY) is None
        with job_lock.acquire(KEY) as lock:
            assert lock is not None
