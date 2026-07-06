"""Tests for the shared session_ops module."""

import contextlib
import json
import logging
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ccrecall.db import _ensure_vec_schema, vec_available
from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.hooks.import_conversations import get_file_hash
from ccrecall.parsing import extract_session_uuid
from ccrecall.schema import SCHEMA
from ccrecall.session_ops import MAX_WRITE_PATH_EMBEDS_PER_SYNC, embed_branch_chunks, sync_session

FIXTURE_DIR = Path(__file__).parent / "fixtures"


class TestSyncSessionCreatesBranches:
    """Verify sync_session in session_ops produces the same result as the current implementation."""

    def test_sync_session_creates_branches(self, memory_db):
        """sync_session should create branches from a fixture with rewinding."""

        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            new_count = sync_session(memory_db, fixture_path, project_dir)

        assert new_count > 0, "Should have added messages"

        cursor = memory_db.cursor()
        cursor.execute("SELECT COUNT(*) FROM sessions")
        assert cursor.fetchone()[0] == 1

        cursor.execute("SELECT COUNT(*) FROM branches")
        branch_count = cursor.fetchone()[0]
        assert branch_count == 1, "Session-keyed identity: exactly one branch row per session"

        cursor.execute("SELECT COUNT(*) FROM branch_messages")
        assert cursor.fetchone()[0] > 0

        cursor.execute("SELECT COUNT(*) FROM branches WHERE is_active = 1")
        assert cursor.fetchone()[0] == 1

    def test_sync_session_populates_aggregated_content(self, memory_db):
        """Aggregated content should be populated."""

        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(memory_db, fixture_path, project_dir)

        cursor = memory_db.cursor()
        cursor.execute("SELECT aggregated_content FROM branches WHERE is_active = 1")
        row = cursor.fetchone()
        assert row is not None
        assert row[0], "Active branch should have aggregated content"

    def test_sync_session_populates_context_summary(self, memory_db):
        """Context summary should be set after sync."""

        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(memory_db, fixture_path, project_dir)
            memory_db.commit()

        cursor = memory_db.cursor()
        cursor.execute("SELECT context_summary, summary_version FROM branches WHERE is_active = 1")
        row = cursor.fetchone()
        assert row is not None
        assert row[0], "Context summary should be populated"
        assert row[1] == 4, "summary_version should be 4"

    def test_no_has_tool_use_in_insert(self, memory_db):
        """has_tool_use and tool_summary columns should NOT be in the INSERT column list in session_ops.

        The columns still exist in the schema (for backward compat with old data),
        but sync_session must not write them — they stay NULL or 0 by default.
        This test verifies the messages row has NULL has_tool_use and tool_summary
        after a sync, not a populated value from the INSERT.
        """

        fixture_path = FIXTURE_DIR / "tool_heavy.jsonl"
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(memory_db, fixture_path, project_dir)
            memory_db.commit()

        cursor = memory_db.cursor()
        # tool_heavy.jsonl has tool_use content — if we WERE writing has_tool_use,
        # some rows would be non-NULL/non-zero.  Since we don't write it, all rows
        # should have NULL or the schema default (0 for has_tool_use).
        cursor.execute("SELECT COUNT(*) FROM messages WHERE has_tool_use IS NOT NULL AND has_tool_use != 0")
        rows_with_tool_use = cursor.fetchone()[0]
        assert rows_with_tool_use == 0, "session_ops INSERT must not populate has_tool_use column"

        cursor.execute("SELECT COUNT(*) FROM messages WHERE tool_summary IS NOT NULL")
        rows_with_tool_summary = cursor.fetchone()[0]
        assert rows_with_tool_summary == 0, "session_ops INSERT must not populate tool_summary column"


class TestSyncSessionWritesNullHashImportLog:
    """Verify sync path writes import_log with file_hash = NULL."""

    def test_sync_session_writes_null_hash_import_log(self, memory_db):
        """When write_import_log=True and file_hash=None, import_log has NULL hash."""

        fixture_path = FIXTURE_DIR / "linear_3_exchange.jsonl"
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(
                memory_db,
                fixture_path,
                project_dir,
                write_import_log=True,
                file_hash=None,
            )
            memory_db.commit()

        cursor = memory_db.cursor()
        cursor.execute(
            "SELECT file_hash FROM import_log WHERE file_path = ?",
            (str(fixture_path),),
        )
        row = cursor.fetchone()
        assert row is not None, "import_log entry should exist"
        assert row[0] is None, "file_hash should be NULL for the sync path"

    def test_sync_session_no_import_log_by_default(self, memory_db):
        """Default call (write_import_log=False) must not write import_log."""

        fixture_path = FIXTURE_DIR / "linear_3_exchange.jsonl"
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(memory_db, fixture_path, project_dir)
            memory_db.commit()

        cursor = memory_db.cursor()
        cursor.execute("SELECT COUNT(*) FROM import_log")
        assert cursor.fetchone()[0] == 0, "import_log must not be written when write_import_log=False"


class TestImportSessionWritesRealHashImportLog:
    """Verify import path writes import_log with the real file hash."""

    def test_import_session_writes_real_hash_import_log(self, memory_db):
        """When write_import_log=True with a real hash, import_log stores that hash."""

        fixture_path = FIXTURE_DIR / "linear_3_exchange.jsonl"
        file_hash = get_file_hash(fixture_path)

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(
                memory_db,
                fixture_path,
                project_dir,
                write_import_log=True,
                file_hash=file_hash,
            )
            memory_db.commit()

        cursor = memory_db.cursor()
        cursor.execute(
            "SELECT file_hash FROM import_log WHERE file_path = ?",
            (str(fixture_path),),
        )
        row = cursor.fetchone()
        assert row is not None, "import_log entry should exist"
        assert row[0] == file_hash, "file_hash should match the real computed hash"


class TestImportSkipsNullHashEntry:
    """Verify that a NULL-hash import_log entry is treated as stale (not skipped)."""

    def test_import_skips_null_hash_entry(self, memory_db):
        """A NULL-hash import_log row means 'synced but not yet hashed'.

        The import path must NOT skip this file — it should re-import and update
        the row with the real hash.
        """

        fixture_path = FIXTURE_DIR / "linear_3_exchange.jsonl"

        # Simulate what sync does: write NULL-hash import_log entry
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(
                memory_db,
                fixture_path,
                project_dir,
                write_import_log=True,
                file_hash=None,
            )
            memory_db.commit()

        # Verify NULL hash was written; capture the row id to prove UPDATE (not INSERT) later
        cursor = memory_db.cursor()
        cursor.execute(
            "SELECT id, file_hash FROM import_log WHERE file_path = ?",
            (str(fixture_path),),
        )
        pre_row = cursor.fetchone()
        assert pre_row[1] is None, "Setup: hash should be NULL"
        log_row_id = pre_row[0]

        # Now simulate import path: compute real hash and call sync_session again
        real_hash = get_file_hash(fixture_path)

        with tempfile.TemporaryDirectory() as tmpdir2:
            project_dir2 = Path(tmpdir2)
            result = sync_session(
                memory_db,
                fixture_path,
                project_dir2,
                write_import_log=True,
                file_hash=real_hash,
            )
            memory_db.commit()

        # Should have processed (not skipped due to hash=NULL != real_hash)
        # The return value >= 0 (0 is acceptable if no NEW messages inserted)
        assert result >= 0, "Should not return -1 (skipped); NULL hash must trigger reimport"

        # Golden pin: the NULL-hash row must be UPDATED in place (same id) with the
        # exact resulting column values — not skipped, and not re-inserted as a new row.
        cursor.execute(
            "SELECT id, file_path, file_hash, imported_at, messages_imported FROM import_log WHERE file_path = ?",
            (str(fixture_path),),
        )
        rows = cursor.fetchall()
        assert len(rows) == 1, "Exactly one import_log row must exist (UPDATE, not new INSERT)"
        row_id, row_path, row_hash, row_imported_at, row_messages = rows[0]
        assert row_id == log_row_id, f"Row id must be unchanged (UPDATE path): expected {log_row_id}, got {row_id}"
        assert row_path == str(fixture_path), "file_path must be unchanged"
        assert row_hash == real_hash, "import_log hash should be updated to real hash"
        assert isinstance(row_imported_at, str), "imported_at must be a string after UPDATE"
        assert len(row_imported_at) >= 10, "imported_at must be a plausible non-empty timestamp after UPDATE"
        # messages_imported is written session-scoped (session_ops.py: COUNT WHERE session_id=?),
        # so pin it against the same session-scoped count, not a global count.
        session_uuid = extract_session_uuid(fixture_path)
        cursor.execute("SELECT id FROM sessions WHERE uuid = ?", (session_uuid,))
        session_id = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,))
        expected_msg_count = cursor.fetchone()[0]
        assert expected_msg_count > 0, "messages must have been written"
        assert row_messages == expected_msg_count, (
            f"messages_imported must equal total messages in session: expected {expected_msg_count}, got {row_messages}"
        )


class TestImportLogExactHashSkip:
    """DB-state golden pin for the exact-hash -1 skip path.

    When a prior import_log row has a non-NULL file_hash matching the provided
    file_hash, sync_session must return -1 immediately and write nothing new.
    This pin asserts the -1 return and that message/branch counts are unchanged.
    """

    def test_exact_hash_match_returns_minus_one_and_writes_nothing(self, memory_db):
        """Exact non-NULL hash match causes sync_session to return -1 without writing.

        Pre-seeds the import_log with a real file_hash row (mimicking a completed
        import), then calls sync_session again with the same hash. Asserts:
          - return value is exactly -1
          - message count is unchanged
          - branch count is unchanged
        """
        fixture_path = FIXTURE_DIR / "linear_3_exchange.jsonl"
        real_hash = get_file_hash(fixture_path)

        # Step 1: initial import — writes import_log with real hash
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            first_result = sync_session(
                memory_db,
                fixture_path,
                project_dir,
                write_import_log=True,
                file_hash=real_hash,
            )
            memory_db.commit()

        assert first_result >= 0, "First import must succeed (return >= 0)"

        cursor = memory_db.cursor()
        cursor.execute("SELECT COUNT(*) FROM messages")
        msg_count_before = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM branches")
        branch_count_before = cursor.fetchone()[0]

        assert msg_count_before > 0, "Setup: messages must exist after first import"

        # Verify the import_log has the real hash, and capture the full row so we can
        # prove the -1 skip writes nothing (not even a touched imported_at).
        cursor.execute(
            "SELECT id, file_hash, imported_at, messages_imported FROM import_log WHERE file_path = ?",
            (str(fixture_path),),
        )
        import_log_row_before = cursor.fetchone()
        assert import_log_row_before is not None, "Setup: import_log row must exist before second call"
        assert import_log_row_before[1] == real_hash, "Setup: import_log must have real hash before second call"

        # Step 2: second call with same hash — must return -1 and write nothing
        with tempfile.TemporaryDirectory() as tmpdir2:
            project_dir2 = Path(tmpdir2)
            skip_result = sync_session(
                memory_db,
                fixture_path,
                project_dir2,
                write_import_log=True,
                file_hash=real_hash,
            )
            memory_db.commit()

        # Must return exactly -1
        assert skip_result == -1, f"Exact hash match must return -1 (skip), got {skip_result}"

        # Message count must be unchanged
        cursor.execute("SELECT COUNT(*) FROM messages")
        msg_count_after = cursor.fetchone()[0]
        assert msg_count_after == msg_count_before, (
            f"Message count must be unchanged after -1 skip: before={msg_count_before}, after={msg_count_after}"
        )

        # Branch count must be unchanged
        cursor.execute("SELECT COUNT(*) FROM branches")
        branch_count_after = cursor.fetchone()[0]
        assert branch_count_after == branch_count_before, (
            f"Branch count must be unchanged after -1 skip: before={branch_count_before}, after={branch_count_after}"
        )

        # The import_log row itself must be untouched — a -1 skip writes nothing,
        # not even a refreshed imported_at or messages_imported.
        cursor.execute(
            "SELECT id, file_hash, imported_at, messages_imported FROM import_log WHERE file_path = ?",
            (str(fixture_path),),
        )
        import_log_row_after = cursor.fetchone()
        assert import_log_row_after == import_log_row_before, (
            f"import_log row must be unchanged after -1 skip: before={import_log_row_before}, after={import_log_row_after}"
        )


class TestSyncThenImportDedupIntegration:
    """Integration test: sync then import does not duplicate messages."""

    def test_sync_then_import_dedup_integration(self, memory_db):
        """End-to-end: sync a session file, then run import on the same file.

        Verifies:
        - import_log row is updated with real hash (not NULL)
        - No duplicate messages are created
        """

        fixture_path = FIXTURE_DIR / "linear_3_exchange.jsonl"

        # Step 1: sync path writes NULL-hash entry
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_count = sync_session(
                memory_db,
                fixture_path,
                project_dir,
                write_import_log=True,
                file_hash=None,
            )
            memory_db.commit()

        assert sync_count > 0, "Sync should have added messages"

        cursor = memory_db.cursor()
        cursor.execute("SELECT COUNT(*) FROM messages")
        messages_after_sync = cursor.fetchone()[0]
        assert messages_after_sync > 0

        # Step 2: import path computes hash and re-syncs
        real_hash = get_file_hash(fixture_path)

        with tempfile.TemporaryDirectory() as tmpdir2:
            project_dir2 = Path(tmpdir2)
            sync_session(
                memory_db,
                fixture_path,
                project_dir2,
                write_import_log=True,
                file_hash=real_hash,
            )
            memory_db.commit()

        # No duplicate messages
        cursor.execute("SELECT COUNT(*) FROM messages")
        messages_after_import = cursor.fetchone()[0]
        assert messages_after_import == messages_after_sync, "Import after sync must not create duplicate messages"

        # import_log row updated with real hash
        cursor.execute(
            "SELECT file_hash FROM import_log WHERE file_path = ?",
            (str(fixture_path),),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == real_hash, "import_log should have real hash after import"

        # No duplicate (session_id, uuid) pairs
        cursor.execute("""
            SELECT session_id, uuid, COUNT(*) AS cnt
            FROM messages
            GROUP BY session_id, uuid
            HAVING cnt > 1
        """)
        assert cursor.fetchall() == [], "No duplicate (session_id, uuid) pairs"


class TestAggregatedContentEnrichment:
    """Test that aggregated_content includes file paths and commit text for FTS."""

    def test_aggregated_content_includes_file_paths(self, memory_db):
        """File paths from files_modified should appear in aggregated_content."""
        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"

        # We need a fixture that produces files_modified; if single_rewind doesn't have them,
        # we'll verify the mechanism by checking what we know the fixture produces.
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(memory_db, fixture_path, project_dir)
            memory_db.commit()

        cursor = memory_db.cursor()
        cursor.execute("SELECT aggregated_content, files_modified FROM branches WHERE is_active = 1")
        row = cursor.fetchone()
        assert row is not None
        agg_content, files_json = row

        if files_json:
            files = json.loads(files_json)
            if files:
                # Full paths should appear in aggregated_content
                for path in files[:3]:
                    assert path in agg_content, f"Full file path '{path}' should be in aggregated_content"
                assert "__files__" in agg_content, "__files__ marker should separate message text from file paths"

    def test_aggregated_content_includes_commits(self, memory_db):
        """Commit text from commits should appear in aggregated_content."""
        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(memory_db, fixture_path, project_dir)
            memory_db.commit()

        cursor = memory_db.cursor()
        cursor.execute("SELECT aggregated_content, commits FROM branches WHERE is_active = 1")
        row = cursor.fetchone()
        assert row is not None
        agg_content, commits_json = row

        if commits_json:
            commits = json.loads(commits_json)
            if commits:
                for commit in commits[:3]:
                    assert commit in agg_content, f"Commit '{commit}' should be in aggregated_content"
                assert "__commits__" in agg_content, "__commits__ marker should separate file paths from commit text"

    def test_aggregated_content_set_semantics_on_resync(self, memory_db):
        """On resync, aggregated_content is recomputed (SET), not doubled (APPEND)."""
        fixture_path = FIXTURE_DIR / "linear_3_exchange.jsonl"

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            sync_session(memory_db, fixture_path, project_dir)
            memory_db.commit()

        cursor = memory_db.cursor()
        cursor.execute("SELECT aggregated_content FROM branches WHERE is_active = 1")
        content_after_first = cursor.fetchone()[0]

        # Sync again (same data)
        with tempfile.TemporaryDirectory() as tmpdir2:
            project_dir2 = Path(tmpdir2)
            sync_session(memory_db, fixture_path, project_dir2)
            memory_db.commit()

        cursor.execute("SELECT aggregated_content FROM branches WHERE is_active = 1")
        content_after_second = cursor.fetchone()[0]

        assert content_after_first == content_after_second, (
            "aggregated_content should be idempotent on resync (SET, not APPEND)"
        )


def _make_vec_conn(tmp_path: Path) -> sqlite3.Connection | None:
    """Create a load_vec=True connection with vec schema, or return None if unavailable."""
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.executescript(SCHEMA)
    conn.commit()
    if not vec_available(conn):
        conn.close()
        return None
    _ensure_vec_schema(conn)
    conn.commit()
    return conn


def _seed_branch(cursor: sqlite3.Cursor, is_active: int = 1, embedding_version: int = 0) -> int:
    """Seed a minimal project/session/branch row; return the branch id."""
    cursor.execute("INSERT INTO projects (path, key) VALUES (?, ?)", ("/test/proj", "test-proj"))
    project_id = cursor.lastrowid
    cursor.execute("INSERT INTO sessions (uuid, project_id) VALUES (?, ?)", ("test-session-uuid", project_id))
    session_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO branches (session_id, leaf_uuid, is_active, embedding_version) VALUES (?, ?, ?, ?)",
        (session_id, "test-leaf-uuid", is_active, embedding_version),
    )
    return cursor.lastrowid


def _make_msgs(*exchange_pairs: tuple[str, str]) -> list[dict]:
    """Return user/assistant message dicts for the given exchange-content tuples.

    Usage: _make_msgs(("user q1", "asst a1"), ("user q2", "asst a2"))
    Each pair is (user_content, assistant_content). Pass empty string to omit assistant.
    """
    msgs = []
    for i, (user_content, asst_content) in enumerate(exchange_pairs):
        msgs.append(
            {
                "role": "user",
                "content": user_content,
                "timestamp": f"2024-01-01T00:{i:02d}:00",
                "uuid": f"user-uuid-{i}",
            }
        )
        if asst_content:
            msgs.append(
                {
                    "role": "assistant",
                    "content": asst_content,
                    "timestamp": f"2024-01-01T00:{i:02d}:30",
                    "uuid": None,
                }
            )
    return msgs


class TestEmbedBranchChunks:
    """Tests for embed_branch_chunks: incremental write path per design.md §(2)."""

    def test_incremental_diff_embeds_only_new_exchange(self, tmp_path):
        """After embedding 2 exchanges, adding 1 more re-syncs only the new one."""
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")

        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        msgs_2 = _make_msgs(("Hello", "Hi there"), ("How are you?", "Fine thanks"))
        fake_vec = [0.1] * EMBEDDING_DIM

        with patch("ccrecall.session_ops.embed_text", return_value=fake_vec) as mock_embed:
            embed_branch_chunks(cursor, branch_id, msgs_2, is_active=True, vec_writable=True)

        assert mock_embed.call_count == 2, "first sync should embed 2 exchanges"

        cursor.execute("SELECT COUNT(*) FROM chunks WHERE branch_id = ?", (branch_id,))
        assert cursor.fetchone()[0] == 2

        # Now add a 3rd exchange
        msgs_3 = _make_msgs(("Hello", "Hi there"), ("How are you?", "Fine thanks"), ("New q?", "New a"))

        with patch("ccrecall.session_ops.embed_text", return_value=fake_vec) as mock_embed2:
            embed_branch_chunks(cursor, branch_id, msgs_3, is_active=True, vec_writable=True)

        assert mock_embed2.call_count == 1, "re-sync should embed only the 1 new exchange"

        cursor.execute("SELECT COUNT(*) FROM chunks WHERE branch_id = ?", (branch_id,))
        assert cursor.fetchone()[0] == 3

        cursor.execute("SELECT COUNT(*) FROM chunk_vec")
        assert cursor.fetchone()[0] == 3

        conn.close()

    def test_no_embed_on_unchanged_content(self, tmp_path):
        """Re-syncing with no content change calls embed_text zero times."""
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")

        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        msgs = _make_msgs(("Hello", "Hi"), ("What's up?", "Nothing much"))
        fake_vec = [0.1] * EMBEDDING_DIM

        with patch("ccrecall.session_ops.embed_text", return_value=fake_vec):
            embed_branch_chunks(cursor, branch_id, msgs, is_active=True, vec_writable=True)

        # Re-sync with exactly the same messages
        with patch("ccrecall.session_ops.embed_text", return_value=fake_vec) as mock_embed:
            embed_branch_chunks(cursor, branch_id, msgs, is_active=True, vec_writable=True)

        assert mock_embed.call_count == 0, "unchanged content must not trigger re-embedding"

        conn.close()

    def test_zero_exchange_branch_stamps_watermark(self, tmp_path):
        """An active branch with no embeddable exchange (all-assistant messages,
        as in a sub-agent/sidechain transcript) embeds nothing but advances its
        watermark to current, so the backfill stops re-selecting it and stalling."""
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")

        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)  # embedding_version defaults to 0
        conn.commit()

        # Assistant-only messages form no user->assistant exchange pair.
        msgs = [{"role": "assistant", "content": "sidechain output", "timestamp": "2024-01-01T00:00:30", "uuid": None}]

        with patch("ccrecall.session_ops.embed_text") as mock_embed:
            returned = embed_branch_chunks(cursor, branch_id, msgs, is_active=True, vec_writable=True)

        assert returned == 0
        assert mock_embed.call_count == 0, "no exchange means no embedding"
        ev = cursor.execute("SELECT embedding_version FROM branches WHERE id = ?", (branch_id,)).fetchone()[0]
        assert ev == EMBEDDING_VERSION, "zero-exchange branch must advance watermark to leave the eligible set"

        conn.close()

    def test_zero_exchange_inactive_branch_not_stamped(self):
        """An inactive branch must not have its watermark set by the zero-exchange
        path: the is_active guard early-returns before any stamping. Uses a plain
        connection — this path never reaches the vector layer, so it must run even
        where sqlite-vec is unavailable."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()

        cursor = conn.cursor()
        branch_id = _seed_branch(cursor, is_active=0)
        conn.commit()

        msgs = [{"role": "assistant", "content": "x", "timestamp": "2024-01-01T00:00:30", "uuid": None}]
        returned = embed_branch_chunks(cursor, branch_id, msgs, is_active=False, vec_writable=True)

        assert returned == 0
        ev = cursor.execute("SELECT embedding_version FROM branches WHERE id = ?", (branch_id,)).fetchone()[0]
        assert ev == 0, "inactive branch must not be stamped"

        conn.close()

    def test_prune_on_exchange_shrink(self, tmp_path):
        """Removing an exchange deletes its chunks and chunk_vec rows (cascade)."""
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")

        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        msgs_3 = _make_msgs(("q1", "a1"), ("q2", "a2"), ("q3", "a3"))
        fake_vec = [0.1] * EMBEDDING_DIM

        with patch("ccrecall.session_ops.embed_text", return_value=fake_vec):
            embed_branch_chunks(cursor, branch_id, msgs_3, is_active=True, vec_writable=True)

        cursor.execute("SELECT COUNT(*) FROM chunks WHERE branch_id = ?", (branch_id,))
        assert cursor.fetchone()[0] == 3

        # Shrink: remove the 3rd exchange
        msgs_2 = _make_msgs(("q1", "a1"), ("q2", "a2"))

        with patch("ccrecall.session_ops.embed_text", return_value=fake_vec):
            embed_branch_chunks(cursor, branch_id, msgs_2, is_active=True, vec_writable=True)

        cursor.execute("SELECT COUNT(*) FROM chunks WHERE branch_id = ?", (branch_id,))
        assert cursor.fetchone()[0] == 2, "pruned chunk row must be deleted"

        cursor.execute("SELECT COUNT(*) FROM chunk_vec")
        assert cursor.fetchone()[0] == 2, "cascade trigger must delete chunk_vec row"

        conn.close()

    def test_embed_error_leaves_watermark_cleared(self, memory_db):
        """embed_text raising leaves watermark cleared (< EMBEDDING_VERSION), not stale-but-true.

        Validates the clear-first protocol (step 5a): watermark is set to 0 before
        the embed loop, so a suppressed exception inside the loop never leaves the branch
        with a false EMBEDDING_VERSION watermark.
        """
        cursor = memory_db.cursor()
        branch_id = _seed_branch(cursor)
        memory_db.commit()

        msgs = _make_msgs(("Hello", "Hi"), ("Question?", "Answer"))

        # Simulate sync_branch's contextlib.suppress wrapper
        with (
            patch("ccrecall.session_ops.embed_text", side_effect=RuntimeError("embed failed")),
            contextlib.suppress(Exception),
        ):
            embed_branch_chunks(cursor, branch_id, msgs, is_active=True, vec_writable=True)

        cursor.execute("SELECT embedding_version FROM branches WHERE id = ?", (branch_id,))
        ev = cursor.fetchone()[0]
        assert ev < EMBEDDING_VERSION, (
            f"watermark must be cleared (< {EMBEDDING_VERSION}) after suppressed embed error, got {ev}"
        )

    def test_version_bump_cap_embeds_at_most_max(self, tmp_path):
        """After simulating many needing-embed exchanges, at most MAX_WRITE_PATH_EMBEDS_PER_SYNC
        chunk vectors are written per sync; the rest are left for backfill."""
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")

        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        # Create 2*MAX new exchanges — all need embedding (no existing chunks)
        n_exchanges = MAX_WRITE_PATH_EMBEDS_PER_SYNC * 2
        msgs = _make_msgs(*[(f"question {i}", f"answer {i}") for i in range(n_exchanges)])
        fake_vec = [0.1] * EMBEDDING_DIM

        with patch("ccrecall.session_ops.embed_text", return_value=fake_vec) as mock_embed:
            embed_branch_chunks(cursor, branch_id, msgs, is_active=True, vec_writable=True)

        assert mock_embed.call_count <= MAX_WRITE_PATH_EMBEDS_PER_SYNC, (
            f"write path must embed at most {MAX_WRITE_PATH_EMBEDS_PER_SYNC} per sync, got {mock_embed.call_count}"
        )

        cursor.execute("SELECT COUNT(*) FROM chunk_vec")
        vec_count = cursor.fetchone()[0]
        assert vec_count <= MAX_WRITE_PATH_EMBEDS_PER_SYNC, (
            f"at most {MAX_WRITE_PATH_EMBEDS_PER_SYNC} chunk_vec rows expected, got {vec_count}"
        )

        # Watermark must NOT be EMBEDDING_VERSION — not all exchanges have been embedded
        cursor.execute("SELECT embedding_version FROM branches WHERE id = ?", (branch_id,))
        ev = cursor.fetchone()[0]
        assert ev < EMBEDDING_VERSION, (
            f"watermark must stay stale (< {EMBEDDING_VERSION}) when exchanges remain unembedded, got {ev}"
        )

        conn.close()


class TestEmbedOnWriteModelUnavailable:
    """Embedding failure must not fail sync; embedding_version stays 0."""

    def test_embed_text_raises_leaves_embedding_version_zero(self, tmp_path):
        """When embed_text raises, sync completes and embedding_version stays 0."""
        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()

        with (
            patch("ccrecall.session_ops.embed_text", side_effect=RuntimeError("no model")),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            result = sync_session(conn, fixture_path, Path(tmpdir))
            conn.commit()

        # sync must succeed
        assert result >= 0

        cursor = conn.cursor()
        cursor.execute("SELECT embedding_version FROM branches")
        rows = cursor.fetchall()
        assert rows, "branches should exist"
        for (ev,) in rows:
            assert ev == 0 or ev is None, f"embedding_version should stay 0 when embed fails, got {ev}"
        conn.close()


class TestEmbedOnWriteOrderingInvariant:
    """Write order is load-bearing: vec upsert BEFORE version column update."""

    def test_vec_upsert_raises_leaves_embedding_version_zero(self, tmp_path):
        """When the vec upsert raises (after embed succeeds), version columns stay 0.

        Validates the ordering invariant directly: the chunk row is inserted with
        embedding_version=0, then write_chunk_embedding writes the vector FIRST and
        the bookkeeping LAST. If the vector write raises, the bookkeeping UPDATE never
        runs, so the chunk row stays at version 0 (eligible for backfill) — never
        marked done-without-vector. Exercised on a real vec-loaded connection with
        embed_branch_chunks called directly (vec_writable=True), so the path is
        genuinely reached rather than skipped by the vec-availability guard.
        """
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")

        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        msgs = _make_msgs(("Hello", "Hi"), ("Question?", "Answer"))
        fake_vec = [0.1] * EMBEDDING_DIM

        # embed_text succeeds, but the vec upsert (called first by write_chunk_embedding)
        # raises — simulating a vec-write failure after a successful embed.
        with (
            patch("ccrecall.session_ops.embed_text", return_value=fake_vec),
            patch("ccrecall.db.upsert_chunk_vec", side_effect=RuntimeError("vec write failed")),
            contextlib.suppress(Exception),
        ):
            embed_branch_chunks(cursor, branch_id, msgs, is_active=True, vec_writable=True)

        # The chunk row was inserted at version 0; the vector write raised before the
        # bookkeeping UPDATE, so it must remain 0 (not advanced to EMBEDDING_VERSION).
        cursor.execute("SELECT embedding_version FROM chunks WHERE branch_id = ?", (branch_id,))
        chunk_rows = cursor.fetchall()
        assert chunk_rows, "the chunk row must have been inserted before the vec write raised"
        for (ev,) in chunk_rows:
            assert ev == 0, f"chunk embedding_version must stay 0 when the vec upsert raises, got {ev}"

        # No vector persisted, and the branch watermark stays cleared (set at step 5a).
        assert cursor.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0] == 0
        bev = cursor.execute("SELECT embedding_version FROM branches WHERE id = ?", (branch_id,)).fetchone()[0]
        assert bev < EMBEDDING_VERSION, f"branch watermark must stay cleared, got {bev}"
        conn.close()


# Availability check evaluated once at collection time
try:
    _test_conn = sqlite3.connect(":memory:")
    _VEC_OK = vec_available(_test_conn)
    _test_conn.close()
except Exception:
    _VEC_OK = False


class TestEmbedOnWriteSuccess:
    """A sync on a vec-enabled connection produces chunk_vec rows for active branches."""

    @pytest.mark.skipif(not _VEC_OK, reason="sqlite-vec not available")
    def test_sync_writes_chunk_vec_rows(self, tmp_path):
        """After sync on a vec-enabled connection, the active branch has chunk_vec rows and
        embedding_version == EMBEDDING_VERSION."""
        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"

        conn = _make_vec_conn(tmp_path)
        assert conn is not None  # guarded by skipif

        fake_vec = [0.1] * EMBEDDING_DIM

        with (
            patch("ccrecall.session_ops.embed_text", return_value=fake_vec),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            result = sync_session(conn, fixture_path, Path(tmpdir))
            conn.commit()

        assert result >= 0

        cursor = conn.cursor()
        # Active leaf with a successful context_summary gets chunk-embedded
        cursor.execute(
            "SELECT b.id, b.embedding_version, b.embedding_model, b.is_active"
            " FROM branches b"
            " WHERE b.context_summary IS NOT NULL AND b.context_summary != ''"
        )
        rows = cursor.fetchall()
        assert rows, "should have at least one summarized branch"

        active_embedded = False
        for branch_id, ev, em, is_active in rows:
            cursor.execute("SELECT COUNT(*) FROM chunks WHERE branch_id = ?", (branch_id,))
            chunk_count = cursor.fetchone()[0]
            cursor.execute(
                "SELECT COUNT(*) FROM chunk_vec cv JOIN chunks c ON cv.chunk_id = c.id WHERE c.branch_id = ?",
                (branch_id,),
            )
            vec_count = cursor.fetchone()[0]
            if is_active:
                active_embedded = True
                assert ev == EMBEDDING_VERSION, f"branch {branch_id}: embedding_version={ev}, want {EMBEDDING_VERSION}"
                assert em == EMBEDDING_MODEL, f"branch {branch_id}: embedding_model={em!r}, want {EMBEDDING_MODEL!r}"
                assert chunk_count > 0, f"active branch {branch_id}: expected chunk rows, got 0"
                assert vec_count == chunk_count, (
                    f"active branch {branch_id}: chunk_vec count {vec_count} != chunk count {chunk_count}"
                )
            else:
                assert vec_count == 0, f"inactive branch {branch_id}: should have no vectors, got {vec_count}"

        assert active_embedded, "expected at least one embedded active leaf"
        conn.close()


class TestSummaryWriteExceptionHandling:
    """sync_session classifies summary-write failures (issue #10): content and
    infra errors skip the branch's summary so the import keeps going, but a
    genuine bug propagates instead of being masked as "no summary"."""

    def run_with_summary_error(self, conn, side_effect):
        """sync the single_rewind fixture with compute_context_summary forced to raise."""
        fixture_path = FIXTURE_DIR / "single_rewind.jsonl"
        with (
            patch("ccrecall.session_ops.compute_context_summary", side_effect=side_effect),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            return sync_session(conn, fixture_path, Path(tmpdir))

    def test_content_error_skips_summary_without_failing(self, memory_db):
        """A malformed-content error (ValueError) leaves the summary unset but sync succeeds."""
        result = self.run_with_summary_error(memory_db, ValueError("bad summary data"))

        assert result >= 0, "sync must complete despite the content error"
        cursor = memory_db.cursor()
        cursor.execute("SELECT context_summary FROM branches")
        rows = cursor.fetchall()
        assert rows, "branches should still be created"
        assert all(r[0] is None for r in rows), "summary stays unset on a content error"

    def test_infra_error_is_skipped_and_logged_not_aborted(self, memory_db, caplog):
        """A DB error (sqlite3.Error) is caught per-branch and logged — sync still completes.

        This pins the regression fix: a summary-write DB failure must not propagate
        and abort the whole import; it skips the branch and surfaces in the log.
        """
        with caplog.at_level(logging.ERROR, logger="ccrecall"):
            result = self.run_with_summary_error(memory_db, sqlite3.OperationalError("database is locked"))

        assert result >= 0, "sync must complete (per-branch skip, not import abort)"
        cursor = memory_db.cursor()
        cursor.execute("SELECT context_summary FROM branches")
        assert all(r[0] is None for r in cursor.fetchall()), "summary stays unset on an infra error"
        assert "summary write failed" in caplog.text, "infra failure must be observable in the log"

    def test_genuine_bug_propagates(self, memory_db):
        """An unexpected error (AttributeError) is NOT swallowed — it surfaces rather than masking."""
        with pytest.raises(AttributeError):
            self.run_with_summary_error(memory_db, AttributeError("real bug"))
