"""Tests for the tool_content backfill hook.

Backfill re-parses each eligible session's JSONL file to populate
messages.tool_content (UPDATE for existing rows, INSERT for previously-skipped
tool-only turns), rebuilds branches.aggregated_content, and resets
branches.embedding_version to NULL so `backfill embeddings` re-selects the
touched branch. Sessions whose JSONL file is missing are logged and skipped.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from ccrecall.hooks.backfill_query import BATCH_SIZE, EXIT_OK
from ccrecall.hooks.backfill_tool_content import run
from ccrecall.schema import SCHEMA

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _NoCloseConn:
    """Wrapper delegating to a sqlite3.Connection but making close() a no-op.

    Stands in for get_connection() (a @contextlib.contextmanager) via
    `patch(..., return_value=_NoCloseConn(conn))` so the test keeps access to
    the same connection (and its rows) after run() returns.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        pass  # intentional no-op

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _entry(uuid: str, parent_uuid: str | None, ts: str, role: str, content) -> dict:
    return {
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "type": role,
        "timestamp": ts,
        "message": {"role": role, "content": content},
    }


def _seed_session(
    conn: sqlite3.Connection,
    *,
    filepath: Path,
    existing_messages: list[tuple[str, str, str, str]],
    embedding_version: int | None = 5,
    ended_at: str = "2026-01-01T10:00:10Z",
) -> tuple[int, int]:
    """Seed a pre-migration-style session: sessions/messages/branches/import_log
    rows for a session whose only messages are those already known before this
    backfill exists (tool_content NULL). `existing_messages` is a list of
    (uuid, role, content, timestamp).

    Returns (session_id, branch_id).
    """
    session_uuid = filepath.stem
    conn.execute("INSERT INTO sessions (uuid) VALUES (?)", (session_uuid,))
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    msg_ids = []
    for uuid, role, content, ts in existing_messages:
        conn.execute(
            "INSERT INTO messages (session_id, uuid, role, content, timestamp, tool_content)"
            " VALUES (?, ?, ?, ?, ?, NULL)",
            (session_id, uuid, role, content, ts),
        )
        msg_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    conn.execute(
        "INSERT INTO branches (session_id, leaf_uuid, is_active, embedding_version, ended_at, aggregated_content)"
        " VALUES (?, ?, 1, ?, ?, ?)",
        (session_id, existing_messages[-1][0] if existing_messages else "leaf", embedding_version, ended_at, "stale"),
    )
    branch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for msg_id in msg_ids:
        conn.execute("INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (branch_id, msg_id))

    conn.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, ?, ?)",
        (str(filepath), "deadbeef", len(existing_messages)),
    )
    conn.commit()
    return session_id, branch_id


def _run_backfill(conn: sqlite3.Connection, *, days=None, limit=None, status=False, json_mode=False):
    with (
        patch("ccrecall.hooks.backfill_tool_content.get_connection", return_value=_NoCloseConn(conn)),
        patch("ccrecall.hooks.backfill_tool_content.load_settings", return_value={}),
        patch("ccrecall.hooks.backfill_tool_content.time.sleep"),
    ):
        return run(status=status, json_mode=json_mode, days=days, limit=limit)


# UPDATE path + INSERT path + aggregated_content + embedding_version reset


class TestBackfillCore:
    def test_update_existing_row_and_insert_tool_only_row(self, tmp_path):
        """A session with one text+tool assistant row (pre-existing, tool_content
        NULL) and one tool-only assistant row (never inserted, forward-sync
        skipped it) is fully backfilled in one pass."""
        filepath = tmp_path / "sess-a.jsonl"
        _write_jsonl(
            filepath,
            [
                _entry("u1", None, "2026-01-01T10:00:00Z", "user", "Please check the logs"),
                _entry(
                    "a1",
                    "u1",
                    "2026-01-01T10:00:05Z",
                    "assistant",
                    [
                        {"type": "text", "text": "Let me check"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "tail -f /var/log/app.log"}},
                    ],
                ),
                _entry(
                    "a2",
                    "a1",
                    "2026-01-01T10:00:10Z",
                    "assistant",
                    [{"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/app.log"}}],
                ),
            ],
        )

        conn = _make_conn()
        session_id, branch_id = _seed_session(
            conn,
            filepath=filepath,
            existing_messages=[
                ("u1", "user", "Please check the logs", "2026-01-01T10:00:00Z"),
                ("a1", "assistant", "Let me check", "2026-01-01T10:00:05Z"),
            ],
        )

        code = _run_backfill(conn)
        assert code == EXIT_OK

        row_a1 = conn.execute(
            "SELECT content, tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "a1")
        ).fetchone()
        assert row_a1[0] == "Let me check", "existing prose content must not change"
        assert row_a1[1] == "[Bash: tail -f /var/log/app.log]"

        row_a2 = conn.execute(
            "SELECT content, tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "a2")
        ).fetchone()
        assert row_a2 is not None, "tool-only turn must produce a new messages row"
        assert row_a2[0] == ""
        assert row_a2[1] == "[Read: /tmp/app.log]"

        # New row is linked to the branch via branch_messages.
        linked_uuids = {
            r[0]
            for r in conn.execute(
                """
                SELECT m.uuid FROM branch_messages bm JOIN messages m ON bm.message_id = m.id
                WHERE bm.branch_id = ?
                """,
                (branch_id,),
            ).fetchall()
        }
        assert "a2" in linked_uuids

        agg = conn.execute(
            "SELECT aggregated_content, embedding_version FROM branches WHERE id = ?", (branch_id,)
        ).fetchone()
        assert "__tools__" in agg[0]
        assert "[Bash: tail -f /var/log/app.log]" in agg[0]
        assert "[Read: /tmp/app.log]" in agg[0]
        assert agg[1] is None, "embedding_version must be reset to NULL so backfill embeddings re-selects this branch"

    def test_pure_text_message_gets_empty_tool_content_not_marker(self, tmp_path):
        """An existing row with no tool_use blocks gets tool_content = '' (not
        left NULL), so it drops out of the eligible set."""
        filepath = tmp_path / "sess-b.jsonl"
        _write_jsonl(
            filepath,
            [
                _entry("u1", None, "2026-01-01T10:00:00Z", "user", "hello"),
                _entry("a1", "u1", "2026-01-01T10:00:05Z", "assistant", [{"type": "text", "text": "hi there"}]),
            ],
        )
        conn = _make_conn()
        session_id, _ = _seed_session(
            conn,
            filepath=filepath,
            existing_messages=[
                ("u1", "user", "hello", "2026-01-01T10:00:00Z"),
                ("a1", "assistant", "hi there", "2026-01-01T10:00:05Z"),
            ],
        )

        _run_backfill(conn)

        row = conn.execute(
            "SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "a1")
        ).fetchone()
        assert row[0] == ""


class TestBackfillMetaExclusion:
    def test_untagged_meta_entry_does_not_become_a_messages_row(self, tmp_path):
        """An isMeta entry with no origin (untagged notification) must not be
        inserted as a new messages row by the backfill's INSERT pass -- the
        same exclusion parse_jsonl_file applies on the live sync path."""
        filepath = tmp_path / "sess-meta.jsonl"
        _write_jsonl(
            filepath,
            [
                _entry("u1", None, "2026-01-01T10:00:00Z", "user", "hello"),
                {
                    "uuid": "meta1",
                    "parentUuid": "u1",
                    "type": "user",
                    "timestamp": "2026-01-01T10:00:03Z",
                    "isMeta": True,
                    "message": {"role": "user", "content": "Caveat: ..."},
                },
                _entry(
                    "a1",
                    "meta1",
                    "2026-01-01T10:00:05Z",
                    "assistant",
                    [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                ),
            ],
        )

        conn = _make_conn()
        session_id, _ = _seed_session(
            conn,
            filepath=filepath,
            existing_messages=[("u1", "user", "hello", "2026-01-01T10:00:00Z")],
        )

        code = _run_backfill(conn)
        assert code == EXIT_OK

        meta_row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "meta1")
        ).fetchone()[0]
        assert meta_row == 0, "untagged isMeta entry must not become a messages row"

        # The tool-only assistant turn after it is still inserted normally.
        a1 = conn.execute(
            "SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "a1")
        ).fetchone()
        assert a1[0] == "[Bash: ls]"


# Missing JSONL: logged and skipped, doesn't crash the run


class TestBackfillMissingFile:
    def test_missing_file_skipped_other_session_still_processed(self, tmp_path):
        """A session whose JSONL was deleted is skipped; a second, healthy
        session in the same run is still fully backfilled."""
        missing_path = tmp_path / "sess-missing.jsonl"
        # Note: we deliberately never write this file.

        real_path = tmp_path / "sess-real.jsonl"
        _write_jsonl(
            real_path,
            [
                _entry("u1", None, "2026-01-01T10:00:00Z", "user", "run this"),
                _entry(
                    "a1",
                    "u1",
                    "2026-01-01T10:00:05Z",
                    "assistant",
                    [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                ),
            ],
        )

        conn = _make_conn()
        _seed_session(
            conn,
            filepath=missing_path,
            existing_messages=[("u1m", "user", "gone", "2026-01-01T09:00:00Z")],
        )
        real_session_id, _ = _seed_session(
            conn,
            filepath=real_path,
            existing_messages=[("u1", "user", "run this", "2026-01-01T10:00:00Z")],
        )

        code = _run_backfill(conn)

        assert code == EXIT_OK
        row = conn.execute(
            "SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (real_session_id, "u1")
        ).fetchone()
        assert row[0] == ""  # u1 is a plain user turn, no tool_use blocks

        # The tool-only assistant turn from the real session was inserted.
        a1 = conn.execute(
            "SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (real_session_id, "a1")
        ).fetchone()
        assert a1[0] == "[Bash: ls]"


# A present-but-unusable JSONL (parses to no entries/branch) must not be
# miscounted as backfilled — regression test for the total_updated overcount
# finding from code review.


class TestBackfillEmptyEntries:
    def test_no_usable_entries_not_counted_as_backfilled(self, tmp_path, capsys):
        """A JSONL file that exists on disk but parses to no uuid-bearing
        entries (e.g. truncated/corrupted) is a no-op: it must not increment
        `backfilled`, must be tracked separately as `skipped_empty`, and must
        still show as pending under --status (tool_content stays NULL)."""
        filepath = tmp_path / "sess-empty.jsonl"
        filepath.write_text("\n")

        conn = _make_conn()
        session_id, _ = _seed_session(
            conn,
            filepath=filepath,
            existing_messages=[("u1", "user", "hello", "2026-01-01T10:00:00Z")],
        )

        code = _run_backfill(conn, json_mode=True)
        assert code == EXIT_OK

        summary = json.loads(capsys.readouterr().out)
        assert summary["backfilled"] == 0, "no-op session must not count as backfilled"
        assert summary["skipped_empty"] == 1

        row = conn.execute(
            "SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "u1")
        ).fetchone()
        assert row[0] is None, "no-op session's row must stay untouched"

        with (
            patch("ccrecall.hooks.backfill_tool_content.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_tool_content.load_settings", return_value={}),
        ):
            run(status=True, json_mode=True)
        status = json.loads(capsys.readouterr().out)
        assert status["pending_sessions"] == 1, "--status must keep reporting this session as pending"
        assert status["done_sessions"] == 0


# --limit caps sessions processed per run


class TestBackfillLimit:
    def test_limit_caps_sessions_processed(self, tmp_path):
        conn = _make_conn()
        session_ids = []
        for i in range(3):
            filepath = tmp_path / f"sess-{i}.jsonl"
            _write_jsonl(
                filepath,
                [_entry(f"u{i}", None, f"2026-01-0{i + 1}T10:00:00Z", "user", "hello")],
            )
            sid, _ = _seed_session(
                conn,
                filepath=filepath,
                existing_messages=[(f"u{i}", "user", "hello", f"2026-01-0{i + 1}T10:00:00Z")],
            )
            session_ids.append(sid)

        _run_backfill(conn, limit=1)

        backfilled = [
            sid
            for sid in session_ids
            if conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND tool_content IS NULL", (sid,)
            ).fetchone()[0]
            == 0
        ]
        assert len(backfilled) == 1


# --days bounds by branch recency (ended_at)


class TestBackfillDays:
    def test_days_excludes_old_sessions(self, tmp_path):
        conn = _make_conn()

        recent_path = tmp_path / "sess-recent.jsonl"
        _write_jsonl(recent_path, [_entry("ur", None, "2026-01-01T10:00:00Z", "user", "hi")])
        recent_id, _ = _seed_session(
            conn,
            filepath=recent_path,
            existing_messages=[("ur", "user", "hi", "2026-01-01T10:00:00Z")],
        )
        conn.execute("UPDATE branches SET ended_at = datetime('now') WHERE session_id = ?", (recent_id,))

        old_path = tmp_path / "sess-old.jsonl"
        _write_jsonl(old_path, [_entry("uo", None, "2020-01-01T10:00:00Z", "user", "hi")])
        old_id, _ = _seed_session(
            conn,
            filepath=old_path,
            existing_messages=[("uo", "user", "hi", "2020-01-01T10:00:00Z")],
        )
        conn.execute("UPDATE branches SET ended_at = datetime('now', '-60 days') WHERE session_id = ?", (old_id,))
        conn.commit()

        _run_backfill(conn, days=30)

        recent_pending = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND tool_content IS NULL", (recent_id,)
        ).fetchone()[0]
        old_pending = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND tool_content IS NULL", (old_id,)
        ).fetchone()[0]
        assert recent_pending == 0
        assert old_pending == 1, "session outside the --days window must be left untouched"


# --status: read-only session-coverage progress reporter


class TestBackfillStatus:
    def test_json_counts(self, tmp_path, capsys):
        conn = _make_conn()
        filepath = tmp_path / "sess.jsonl"
        _write_jsonl(filepath, [_entry("u1", None, "2026-01-01T10:00:00Z", "user", "hi")])
        _seed_session(conn, filepath=filepath, existing_messages=[("u1", "user", "hi", "2026-01-01T10:00:00Z")])

        with (
            patch("ccrecall.hooks.backfill_tool_content.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_tool_content.load_settings", return_value={}),
        ):
            code = run(status=True, json_mode=True)
        assert code == EXIT_OK

        data = json.loads(capsys.readouterr().out)
        assert data["total_sessions"] == 1
        assert data["pending_sessions"] == 1
        assert data["done_sessions"] == 0

    def test_status_does_not_write(self, tmp_path):
        """--status is read-only: it must not backfill anything."""
        conn = _make_conn()
        filepath = tmp_path / "sess.jsonl"
        _write_jsonl(filepath, [_entry("u1", None, "2026-01-01T10:00:00Z", "user", "hi")])
        _seed_session(conn, filepath=filepath, existing_messages=[("u1", "user", "hi", "2026-01-01T10:00:00Z")])

        with (
            patch("ccrecall.hooks.backfill_tool_content.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_tool_content.load_settings", return_value={}),
        ):
            run(status=True, json_mode=True)

        row = conn.execute("SELECT tool_content FROM messages WHERE uuid = 'u1'").fetchone()
        assert row[0] is None

    def test_status_reflects_completed_backfill(self, tmp_path, capsys):
        conn = _make_conn()
        filepath = tmp_path / "sess.jsonl"
        _write_jsonl(filepath, [_entry("u1", None, "2026-01-01T10:00:00Z", "user", "hi")])
        _seed_session(conn, filepath=filepath, existing_messages=[("u1", "user", "hi", "2026-01-01T10:00:00Z")])

        _run_backfill(conn)

        with (
            patch("ccrecall.hooks.backfill_tool_content.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_tool_content.load_settings", return_value={}),
        ):
            run(status=True, json_mode=True)

        data = json.loads(capsys.readouterr().out)
        assert data["pending_sessions"] == 0
        assert data["done_sessions"] == 1


# Resume: a session already fully backfilled is not re-processed / does not stall


class TestBackfillResume:
    def test_second_run_is_a_no_op_for_already_backfilled_session(self, tmp_path):
        conn = _make_conn()
        filepath = tmp_path / "sess.jsonl"
        _write_jsonl(
            filepath,
            [
                _entry("u1", None, "2026-01-01T10:00:00Z", "user", "hi"),
                _entry(
                    "a1",
                    "u1",
                    "2026-01-01T10:00:05Z",
                    "assistant",
                    [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                ),
            ],
        )
        _seed_session(conn, filepath=filepath, existing_messages=[("u1", "user", "hi", "2026-01-01T10:00:00Z")])

        first_code = _run_backfill(conn)
        assert first_code == EXIT_OK

        # Second run: nothing left to do, must complete cleanly (not abort).
        second_code = _run_backfill(conn)
        assert second_code == EXIT_OK

    def test_no_progress_guard_does_not_fire_on_normal_multi_batch_run(self, tmp_path):
        """A run spanning more sessions than BATCH_SIZE still completes (guards
        against the exclude-set bookkeeping breaking the progress guard)."""
        conn = _make_conn()
        count = BATCH_SIZE + 3
        for i in range(count):
            filepath = tmp_path / f"sess-{i}.jsonl"
            _write_jsonl(filepath, [_entry(f"u{i}", None, "2026-01-01T10:00:00Z", "user", "hi")])
            _seed_session(conn, filepath=filepath, existing_messages=[(f"u{i}", "user", "hi", "2026-01-01T10:00:00Z")])

        code = _run_backfill(conn)
        assert code == EXIT_OK

        remaining = conn.execute("SELECT COUNT(*) FROM messages WHERE tool_content IS NULL").fetchone()[0]
        assert remaining == 0
