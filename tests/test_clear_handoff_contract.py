"""
Tests for the SessionEnd handoff contract between clear-handoff.py and
_find_cleared_from_session_uuid in session_selection.py.

Contract:
  1. clear-handoff.py only writes when end_reason == "clear"
  2. clear-handoff.py only writes when both session_id and cwd are present
  3. Handoff file contains session_id, cwd, timestamp (no transcript_path)
  4. _find_cleared_from_session_uuid returns None if file missing
  5. _find_cleared_from_session_uuid returns None if cwd doesn't match
  6. _find_cleared_from_session_uuid returns None if timestamp is stale (>30s)
  7. File is deleted ONLY after validation passes (not on cwd mismatch)
  8. Corrupt/unreadable files are deleted immediately
"""

import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import ccrecall.hooks.clear_handoff as _clear_handoff
import ccrecall.hooks.session_selection as _session_selection
from ccrecall.hooks.session_selection import HANDOFF_STALE_SECONDS

_find_cleared_from_session_uuid = _session_selection._find_cleared_from_session_uuid


# Helpers


def _run_handoff_main(tmp_path: Path, payload: dict | str) -> tuple[Path, str]:
    """
    Run clear-handoff.main() with a fake db_path under tmp_path and the given
    payload piped through stdin. Returns (handoff_path, captured_stdout).
    """
    fake_db = tmp_path / "conversations.db"
    handoff_path = tmp_path / "clear-handoff.json"

    fake_settings = {"db_path": str(fake_db)}

    stdin_data = payload if isinstance(payload, str) else json.dumps(payload)
    stdout_capture = io.StringIO()

    with (
        patch.object(sys, "stdin", io.StringIO(stdin_data)),
        patch.object(sys, "stdout", stdout_capture),
        patch.object(_clear_handoff, "load_settings", return_value=fake_settings),
        patch.object(_clear_handoff, "get_db_path", return_value=fake_db),
    ):
        _clear_handoff.main()

    return handoff_path, stdout_capture.getvalue()


# clear-handoff.py contract tests


class TestClearHandoffWriter:
    def test_writes_file_on_clear(self, tmp_path):
        """Contract 1+2+3: writes file with correct keys when end_reason=clear."""
        hp, _ = _run_handoff_main(
            tmp_path,
            {
                "end_reason": "clear",
                "session_id": "abc-123",
                "cwd": "/some/project",
            },
        )
        assert hp.exists(), "Handoff file should be written for end_reason=clear"
        data = json.loads(hp.read_text())
        assert data["session_id"] == "abc-123"
        assert data["cwd"] == "/some/project"
        assert "timestamp" in data
        assert "transcript_path" not in data

    def test_does_not_write_missing_session_id(self, tmp_path):
        """Contract 2: skips write when session_id is absent."""
        hp, _ = _run_handoff_main(tmp_path, {"end_reason": "clear", "cwd": "/some/project"})
        assert not hp.exists()

    def test_does_not_write_missing_cwd(self, tmp_path):
        """Contract 2: skips write when cwd is absent."""
        hp, _ = _run_handoff_main(tmp_path, {"end_reason": "clear", "session_id": "abc-123"})
        assert not hp.exists()

    @pytest.mark.parametrize("end_reason", ["interrupt", "crash", "CLEAR", None])
    def test_does_not_write_for_other_reasons(self, tmp_path, end_reason):
        """Contract 1: only end_reason='clear' (exact, case-sensitive) triggers write."""
        hp, _ = _run_handoff_main(
            tmp_path,
            {
                "end_reason": end_reason,
                "session_id": "abc-123",
                "cwd": "/some/project",
            },
        )
        assert not hp.exists(), f"Should not write for end_reason={end_reason!r}"

    def test_does_not_write_on_invalid_json(self, tmp_path):
        """Gracefully ignores malformed stdin."""
        hp, _ = _run_handoff_main(tmp_path, "not-json")
        assert not hp.exists()


class TestClearHandoffAtomicity:
    """The handoff is written atomically, and creates its directory on a first run."""

    def test_writes_when_the_runtime_directory_does_not_exist_yet(self, tmp_path):
        """A fresh machine has no ~/.ccrecall yet; the write must create it.

        Without the mkdir this raises into main()'s broad except, which logs and
        prints an empty object, so the handoff vanishes with no visible failure.
        """
        fresh = tmp_path / "not-created-yet"
        handoff_path, stdout = _run_handoff_main(
            fresh, {"end_reason": "clear", "session_id": "sid-fresh", "cwd": "/my/project"}
        )

        assert handoff_path.exists(), "handoff was lost because its directory did not exist"
        assert json.loads(handoff_path.read_text())["session_id"] == "sid-fresh"
        assert stdout.strip() == "{}"

    def test_does_not_publish_through_a_shared_temporary_path(self, tmp_path):
        """Staging must be per-writer, so concurrent clears cannot clobber each other.

        A guard rather than a regression test: the previous implementation wrote
        the target directly and used no temp file, so this would have passed
        against it too. It exists because the obvious way to add atomicity is a
        fixed `.tmp` name beside the target, and two sessions clearing at once
        then truncate each other's staged bytes — a real bug that was written
        and shipped on the abandoned recap branch before being caught.
        """
        decoy = tmp_path / ".clear-handoff.json.tmp"
        decoy.write_text("another writer's in-flight payload", encoding="utf-8")

        handoff_path, _ = _run_handoff_main(
            tmp_path, {"end_reason": "clear", "session_id": "sid-concurrent", "cwd": "/my/project"}
        )

        assert decoy.read_text(encoding="utf-8") == "another writer's in-flight payload"
        assert json.loads(handoff_path.read_text())["session_id"] == "sid-concurrent"

    def test_leaves_no_temporary_file_behind(self, tmp_path):
        """No leftover .tmp staging files from atomic_write_json.

        Cannot assert exact directory contents any more: clear_handoff.py now
        calls setup_logging() (added by this task's fix so the malformed-input
        warning actually reaches a log file), which writes
        ccrecall-clear-handoff.log into the same runtime directory the handoff
        lives in — matching production, where RUNTIME_DIR doubles as both the
        DB directory and the log directory for every ccrecall hook.
        """
        handoff_path, _ = _run_handoff_main(
            tmp_path, {"end_reason": "clear", "session_id": "sid-clean", "cwd": "/my/project"}
        )

        leftover_tmp = [p.name for p in handoff_path.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftover_tmp == [], f"atomic_write_json left temp file(s) behind: {leftover_tmp}"
        assert handoff_path.exists()

    def test_setup_logging_failure_does_not_lose_the_handoff(self, tmp_path):
        """A bad log path (setup_logging() raising) must not cost the user their handoff write."""
        fake_db = tmp_path / "conversations.db"
        handoff_path = tmp_path / "clear-handoff.json"
        stdin_data = json.dumps({"end_reason": "clear", "session_id": "sid-logging-fail", "cwd": "/my/project"})
        stdout_capture = io.StringIO()

        def raise_bad_log_path(*_a, **_k):
            raise OSError("bad log path")

        with (
            patch.object(sys, "stdin", io.StringIO(stdin_data)),
            patch.object(sys, "stdout", stdout_capture),
            patch.object(_clear_handoff, "load_settings", return_value={"db_path": str(fake_db)}),
            patch.object(_clear_handoff, "get_db_path", return_value=fake_db),
            patch.object(_clear_handoff, "setup_logging", raise_bad_log_path),
        ):
            _clear_handoff.main()

        assert handoff_path.exists(), "setup_logging() failure lost the handoff write"
        assert json.loads(handoff_path.read_text())["session_id"] == "sid-logging-fail"
        assert stdout_capture.getvalue().strip() == "{}"


class TestClearHandoffStdout:
    """Hook must always print valid JSON to stdout for the harness."""

    def test_stdout_on_successful_write(self, tmp_path):
        _, stdout = _run_handoff_main(
            tmp_path,
            {"end_reason": "clear", "session_id": "abc-123", "cwd": "/some/project"},
        )
        assert json.loads(stdout) == {}

    def test_stdout_on_non_clear_reason(self, tmp_path):
        _, stdout = _run_handoff_main(
            tmp_path,
            {"end_reason": "interrupt", "session_id": "abc-123", "cwd": "/some/project"},
        )
        assert json.loads(stdout) == {}

    def test_stdout_on_invalid_json(self, tmp_path):
        _, stdout = _run_handoff_main(tmp_path, "not-json")
        assert json.loads(stdout) == {}

    def test_stdout_on_empty_stdin(self, tmp_path):
        _, stdout = _run_handoff_main(tmp_path, "")
        assert json.loads(stdout) == {}

    def test_stdout_on_missing_fields(self, tmp_path):
        _, stdout = _run_handoff_main(tmp_path, {"end_reason": "clear"})
        assert json.loads(stdout) == {}


# _find_cleared_from_session_uuid contract tests


def _write_handoff(tmp_path: Path, data: dict) -> Path:
    """Write a handoff JSON file relative to a fake db_path."""
    hp = tmp_path / "clear-handoff.json"
    hp.write_text(json.dumps(data))
    return tmp_path / "conversations.db"  # db_path; handoff is db_path.parent / "clear-handoff.json"


class TestFindClearedFromSessionUuid:
    def test_returns_none_when_file_missing(self, tmp_path):
        """Contract 4: no handoff file → None."""
        db_path = tmp_path / "conversations.db"
        result = _find_cleared_from_session_uuid(db_path, "/some/project")
        assert result is None

    def test_returns_session_id_on_valid_handoff(self, tmp_path):
        """Happy path: valid file, matching cwd, fresh timestamp → session_id returned."""
        db_path = _write_handoff(
            tmp_path,
            {
                "session_id": "sid-valid",
                "cwd": "/my/project",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        result = _find_cleared_from_session_uuid(db_path, "/my/project")
        assert result == "sid-valid"

    def test_returns_none_on_cwd_mismatch(self, tmp_path):
        """Contract 5: cwd mismatch → None."""
        db_path = _write_handoff(
            tmp_path,
            {
                "session_id": "sid-xyz",
                "cwd": "/other/project",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        result = _find_cleared_from_session_uuid(db_path, "/my/project")
        assert result is None

    def test_file_not_deleted_on_cwd_mismatch(self, tmp_path):
        """Contract 7 (key fix): file must survive a cwd mismatch so another process can claim it."""
        db_path = _write_handoff(
            tmp_path,
            {
                "session_id": "sid-xyz",
                "cwd": "/other/project",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        handoff_path = tmp_path / "clear-handoff.json"
        _find_cleared_from_session_uuid(db_path, "/my/project")
        assert handoff_path.exists(), "Handoff file should NOT be deleted on cwd mismatch"

    def test_file_deleted_after_valid_consumption(self, tmp_path):
        """Contract 7: file IS deleted after validation passes."""
        db_path = _write_handoff(
            tmp_path,
            {
                "session_id": "sid-valid",
                "cwd": "/my/project",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        handoff_path = tmp_path / "clear-handoff.json"
        _find_cleared_from_session_uuid(db_path, "/my/project")
        assert not handoff_path.exists(), "Handoff file should be deleted after valid consumption"

    def test_returns_none_on_stale_timestamp(self, tmp_path):
        """Contract 6: timestamp older than 30s → None."""
        stale = (datetime.now(timezone.utc) - timedelta(seconds=HANDOFF_STALE_SECONDS + 1)).isoformat()
        db_path = _write_handoff(
            tmp_path,
            {
                "session_id": "sid-stale",
                "cwd": "/my/project",
                "timestamp": stale,
            },
        )
        result = _find_cleared_from_session_uuid(db_path, "/my/project")
        assert result is None

    def test_stale_file_is_deleted_on_rejection(self, tmp_path):
        """Stale handoff file must be deleted so it doesn't block future clears."""
        stale = (datetime.now(timezone.utc) - timedelta(seconds=HANDOFF_STALE_SECONDS + 1)).isoformat()
        db_path = _write_handoff(
            tmp_path,
            {
                "session_id": "sid-stale",
                "cwd": "/my/project",
                "timestamp": stale,
            },
        )
        handoff_path = tmp_path / "clear-handoff.json"
        _find_cleared_from_session_uuid(db_path, "/my/project")
        assert not handoff_path.exists(), "Stale handoff file should be deleted on rejection"

    def test_returns_session_id_on_fresh_timestamp_boundary(self, tmp_path):
        """Timestamp exactly at boundary (29s) is still accepted."""
        fresh = (datetime.now(timezone.utc) - timedelta(seconds=HANDOFF_STALE_SECONDS - 1)).isoformat()
        db_path = _write_handoff(
            tmp_path,
            {
                "session_id": "sid-fresh",
                "cwd": "/my/project",
                "timestamp": fresh,
            },
        )
        result = _find_cleared_from_session_uuid(db_path, "/my/project")
        assert result == "sid-fresh"

    def test_deletes_corrupt_file_immediately(self, tmp_path):
        """Contract 8: unreadable/corrupt JSON → file deleted, None returned."""
        handoff_path = tmp_path / "clear-handoff.json"
        handoff_path.write_text("{{not valid json{{")
        db_path = tmp_path / "conversations.db"
        result = _find_cleared_from_session_uuid(db_path, "/my/project")
        assert result is None
        assert not handoff_path.exists(), "Corrupt handoff file should be deleted immediately"
