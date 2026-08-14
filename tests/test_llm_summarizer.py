import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ccrecall import llm_summarizer
from ccrecall.llm_summarizer import (
    STATUS_CLAUDE_UNAVAILABLE,
    STATUS_CLEANUP_FAILED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PLATFORM_UNSUPPORTED,
    STATUS_TIMEOUT,
    build_claude_argv,
    invoke_claude,
    write_packet,
)
from ccrecall.recap_state import (
    acknowledge_cleanup,
    bind_attempt_packet,
    cancel_attempt_before_launch,
    claim_job,
    complete_attempt,
    quarantine_admission,
    quarantine_attempt,
    reserve_attempt,
    upsert_job,
)

SETTINGS = {
    "llm_summary_model": "sonnet",
    "llm_summary_effort": "medium",
    "llm_summary_max_budget_usd": 1.0,
    "llm_summary_timeout_seconds": 0.1,
}


def _wait_for_process_exit(pid: int) -> bool:
    for _ in range(20):
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()[0]
        except FileNotFoundError:
            return True
        if state == "Z":
            return True
        time.sleep(0.02)
    return False


def test_argv_reads_only_the_single_canonical_db_packet(tmp_path):
    packet = tmp_path / "packet" / "input.json"

    argv = build_claude_argv(packet, SETTINGS)

    assert argv[:3] == ["claude", "-p", "--safe-mode"]
    assert argv[argv.index("--add-dir") + 1] == str(packet.parent)
    assert "--no-session-persistence" in argv
    assert "transcript" not in " ".join(argv).lower()
    assert "citation" not in " ".join(argv).lower()


def test_packet_is_owner_only_and_contains_exact_supplied_bytes(tmp_path):
    packet = tmp_path / "owner" / "input.json"
    payload = b'{"ordered_messages":[]}'

    write_packet(packet, payload)

    assert packet.read_bytes() == payload
    assert packet.stat().st_mode & 0o777 == 0o600
    assert packet.parent.stat().st_mode & 0o777 == 0o700


def test_packet_write_failure_cancels_a_cleaned_reservation(memory_db, tmp_path, monkeypatch):
    now = "2026-08-12T10:00:00Z"
    packet = tmp_path / "owner" / "input.json"
    memory_db.execute("INSERT INTO projects (path, key, name) VALUES ('/tmp', 'p', 'p')")
    memory_db.execute("INSERT INTO sessions (uuid, project_id) VALUES ('session', 1)")
    upsert_job(memory_db, 1, "input", "test", now)
    token = claim_job(memory_db, 1, now, 60)
    attempt = reserve_attempt(memory_db, 1, token, "input", "test", now)
    assert bind_attempt_packet(memory_db, attempt, token, str(packet), "nonce")
    monkeypatch.setattr("ccrecall.llm_summarizer.os.fchmod", lambda *_args: (_ for _ in ()).throw(OSError()))

    def persist_failure(state, _metadata):
        assert state == "verified_removed"
        assert acknowledge_cleanup(memory_db, attempt, token, state, now)
        assert cancel_attempt_before_launch(memory_db, attempt, token, "input", 0, now)

    assert not write_packet(packet, b"{}", persist_write_failure=persist_failure, packet_nonce="nonce")

    assert not packet.exists()
    assert memory_db.execute("SELECT state FROM session_recap_attempts WHERE id = ?", (attempt,)).fetchone() == (
        "cancelled_before_launch",
    )
    assert memory_db.execute("SELECT state, active_attempt_id FROM session_recap_jobs").fetchone() == ("pending", None)


def test_packet_write_failure_quarantines_when_cleanup_cannot_be_proved(memory_db, tmp_path, monkeypatch):
    now = "2026-08-12T10:00:00Z"
    packet = tmp_path / "owner" / "input.json"
    memory_db.execute("INSERT INTO projects (path, key, name) VALUES ('/tmp', 'p', 'p')")
    memory_db.execute("INSERT INTO sessions (uuid, project_id) VALUES ('session', 1)")
    upsert_job(memory_db, 1, "input", "test", now)
    token = claim_job(memory_db, 1, now, 60)
    attempt = reserve_attempt(memory_db, 1, token, "input", "test", now)
    assert bind_attempt_packet(memory_db, attempt, token, str(packet), "nonce")
    monkeypatch.setattr("ccrecall.llm_summarizer.os.fchmod", lambda *_args: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr("ccrecall.llm_summarizer.remove_packet", lambda _path: False)

    def persist_failure(state, metadata):
        assert state == "uncertain"
        assert quarantine_attempt(memory_db, attempt, token, metadata["byte_size"], state, now)
        assert complete_attempt(memory_db, attempt, token, STATUS_CLEANUP_FAILED, now)

    assert not write_packet(packet, b"{}", persist_write_failure=persist_failure, packet_nonce="nonce")

    assert memory_db.execute("SELECT state FROM session_recap_attempts WHERE id = ?", (attempt,)).fetchone() == (
        STATUS_CLEANUP_FAILED,
    )
    assert memory_db.execute(
        "SELECT cleanup_state FROM session_recap_quarantine WHERE attempt_id = ?", (attempt,)
    ).fetchone() == ("uncertain",)


def test_unsupported_platform_does_not_create_a_provider_process(tmp_path):
    called = False

    def popen(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider must not run")

    result = invoke_claude(
        tmp_path / "input.json",
        SETTINGS,
        persist_launch=lambda *_args: True,
        persist_cleanup=lambda *_args: None,
        popen=popen,
        platform_supported=False,
    )

    assert result.status == STATUS_PLATFORM_UNSUPPORTED
    assert not called


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc process groups are required")
def test_timeout_kills_the_real_provider_process_group_and_grandchild(tmp_path):
    packet = tmp_path / "owner" / "input.json"
    pids = tmp_path / "pids.json"
    write_packet(packet, b"{}")
    fixture = Path(__file__).parent / "fixtures" / "process_tree_child.py"

    def popen(*_args, **_kwargs):
        return subprocess.Popen(
            [sys.executable, str(fixture), str(pids)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    result = invoke_claude(
        packet,
        SETTINGS,
        persist_launch=lambda *_args: True,
        persist_cleanup=lambda *_args: None,
        popen=popen,
        grace_seconds=0.1,
    )

    assert result.status == STATUS_TIMEOUT
    child, grandchild = json.loads(pids.read_text(encoding="utf-8"))
    for pid in (child, grandchild):
        assert _wait_for_process_exit(pid), f"process {pid} survived group cleanup"
    assert not packet.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc process groups are required")
def test_normal_completion_kills_a_live_descendant_after_leader_exits(tmp_path):
    packet = tmp_path / "owner" / "input.json"
    pids = tmp_path / "pids.json"
    write_packet(packet, b"{}")
    fixture = Path(__file__).parent / "fixtures" / "process_tree_child.py"

    def popen(*_args, **_kwargs):
        return subprocess.Popen(
            [sys.executable, str(fixture), str(pids), "normal-exit"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    result = invoke_claude(
        packet,
        SETTINGS,
        persist_launch=lambda *_args: True,
        persist_cleanup=lambda *_args: None,
        popen=popen,
        grace_seconds=0.1,
    )

    assert result.status == STATUS_OK
    _leader, child = json.loads(pids.read_text(encoding="utf-8"))
    assert _wait_for_process_exit(child), "descendant survived normal-completion group cleanup"
    assert not packet.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc process groups are required")
def test_ambiguous_launch_blocks_normal_completion_and_retains_packet(tmp_path):
    packet = tmp_path / "owner" / "input.json"
    write_packet(packet, b"{}")

    def popen(*_args, **_kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    cleanup = []
    result = invoke_claude(
        packet,
        SETTINGS,
        persist_launch=lambda *_args: False,
        persist_cleanup=lambda state, _metadata: cleanup.append(state),
        popen=popen,
        grace_seconds=0.1,
    )

    assert result.status == STATUS_CLEANUP_FAILED
    assert cleanup == ["reaped"]
    assert packet.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc process groups are required")
def test_unproven_termination_after_a_spawn_is_distinguishable_from_never_spawning(tmp_path, monkeypatch):
    """A live process with no recorded identity must not report a clearable state.

    Recovery is allowed to clear a plain 'uncertain' attempt, because that is
    what a never-spawned one reports. This path spawned a real process and could
    not prove it died, so it has to say something recovery will refuse.
    """
    packet = tmp_path / "owner" / "input.json"
    write_packet(packet, b"{}")

    def popen(*_args, **_kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    monkeypatch.setattr(llm_summarizer, "_terminate_group", lambda *_args, **_kwargs: False)
    cleanup = []
    result = invoke_claude(
        packet,
        SETTINGS,
        persist_launch=lambda *_args: False,
        persist_cleanup=lambda state, _metadata: cleanup.append(state),
        popen=popen,
        grace_seconds=0.1,
    )

    assert result.status == STATUS_CLEANUP_FAILED
    assert cleanup == [llm_summarizer.CLEANUP_UNCERTAIN_SPAWNED]
    assert cleanup != ["uncertain"], "reported the state a never-spawned attempt reports"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc process groups are required")
def test_launch_persistence_exception_cleans_up_and_is_not_propagated(tmp_path):
    packet = tmp_path / "owner" / "input.json"
    write_packet(packet, b"{}")

    def popen(*_args, **_kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    cleanup = []
    result = invoke_claude(
        packet,
        SETTINGS,
        persist_launch=lambda *_args: (_ for _ in ()).throw(RuntimeError("database unavailable")),
        persist_cleanup=lambda state, metadata: cleanup.append((state, metadata)),
        popen=popen,
        packet_nonce="nonce",
        grace_seconds=0.1,
    )

    assert result.status == STATUS_CLEANUP_FAILED
    assert cleanup[0][0] == "reaped"
    assert cleanup[0][1]["packet_path"] == str(packet)
    assert cleanup[0][1]["packet_nonce"] == "nonce"
    assert cleanup[0][1]["byte_size"] == 2
    assert cleanup[0][1]["process_group_id"] == cleanup[0][1]["process_id"]
    assert cleanup[0][1]["process_started_at"] is not None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc process groups are required")
def test_packet_delete_failure_is_cleanup_failure(tmp_path, monkeypatch):
    packet = tmp_path / "owner" / "input.json"
    write_packet(packet, b"{}")

    def popen(*_args, **_kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", 'print(\'{\\"summary\\": \\"ok\\"}\')'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    monkeypatch.setattr("ccrecall.llm_summarizer.remove_packet", lambda _path: False)
    result = invoke_claude(
        packet,
        SETTINGS,
        persist_launch=lambda *_args: True,
        persist_cleanup=lambda *_args: None,
        popen=popen,
    )

    assert result.status == STATUS_CLEANUP_FAILED


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc process groups are required")
def test_success_returns_normalized_body_without_retaining_the_packet(tmp_path):
    packet = tmp_path / "owner" / "input.json"
    write_packet(packet, b"{}")

    result = invoke_claude(
        packet,
        SETTINGS,
        persist_launch=lambda *_args: True,
        persist_cleanup=lambda *_args: None,
        popen=lambda *_args, **_kwargs: subprocess.Popen(
            [sys.executable, "-c", 'print(\'{"summary":"A useful recap."}\')'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ),
    )

    assert result.status == STATUS_OK
    assert result.response_body == {"summary": "A useful recap."}
    assert not packet.exists()


def test_admission_blocks_packet_write_and_provider_spawn(tmp_path):
    packet = tmp_path / "owner" / "input.json"
    calls = []

    assert not write_packet(packet, b"{}", admit_launch=lambda: False)
    result = invoke_claude(
        packet,
        SETTINGS,
        persist_launch=lambda *_args: True,
        persist_cleanup=lambda *_args: None,
        admit_launch=lambda: False,
        popen=lambda *_args, **_kwargs: calls.append(True),
        platform_supported=True,
    )

    assert result.status == STATUS_CLEANUP_FAILED
    assert not packet.exists()
    assert not calls


def test_initial_packet_admission_denial_uses_content_free_cleanup_seam(tmp_path):
    packet = tmp_path / "owner" / "input.json"
    cleanup = []

    assert not write_packet(
        packet,
        b"sensitive",
        admit_launch=lambda: False,
        persist_write_failure=lambda state, metadata: cleanup.append((state, metadata)),
        packet_nonce="nonce",
    )

    assert cleanup == [("verified_removed", {"packet_path": str(packet), "packet_nonce": "nonce", "byte_size": None})]
    assert not packet.exists()


@pytest.mark.parametrize(
    ("error", "status"),
    [(FileNotFoundError(), STATUS_CLAUDE_UNAVAILABLE), (OSError(), STATUS_ERROR)],
)
def test_spawn_failure_removes_packet_and_reports_cleanup(tmp_path, error, status):
    packet = tmp_path / "owner" / "input.json"
    write_packet(packet, b"sensitive")
    cleanup = []

    result = invoke_claude(
        packet,
        SETTINGS,
        persist_launch=lambda *_args: True,
        persist_cleanup=lambda state, metadata: cleanup.append((state, metadata)),
        popen=lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
        platform_supported=True,
        packet_nonce="nonce",
    )

    assert result.status == status
    assert cleanup == [("verified_removed", {"packet_path": str(packet), "packet_nonce": "nonce", "byte_size": None})]
    assert not packet.exists()


def test_quarantine_capacity_blocks_count_and_bytes(memory_db):
    memory_db.execute("PRAGMA foreign_keys = OFF")
    memory_db.execute(
        "INSERT INTO session_recap_quarantine VALUES (1, '/owner/1', 'n1', 5, NULL, NULL, 'uncertain', '2026-01-01')"
    )
    memory_db.execute("PRAGMA foreign_keys = ON")
    admitted, count, bytes_used, oldest = quarantine_admission(memory_db, max_count=1, max_bytes=10)
    assert (admitted, count, bytes_used, oldest) == (False, 1, 5, "2026-01-01")
    admitted, *_ = quarantine_admission(memory_db, max_count=2, max_bytes=5)
    assert not admitted
