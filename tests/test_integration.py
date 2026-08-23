"""Integration tests for notification classification (task-notification + teammate-message)
and tool-content indexing, end-to-end against real SQLite databases.
"""

import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

from conftest import FIXTURE_DIR, VEC_SKIP, NoCloseConn, make_vec_conn
from conftest import make_jsonl_entry as _entry
from conftest import write_jsonl as _write_jsonl

from ccrecall.content import (
    extract_text_content,
    is_task_notification,
    is_teammate_message,
    is_tool_result,
)
from ccrecall.embed_ops import embed_branch_chunks
from ccrecall.embeddings import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    EMBEDDING_VERSION,
    MODEL_TOKEN_LIMIT,
    SYNC_PATH_TOKEN_LIMIT,
)
from ccrecall.hooks.backfill_query import build_selection
from ccrecall.hooks.backfill_tool_content import run as run_backfill_tool_content
from ccrecall.parsing import (
    build_aggregated_content,
    compute_branch_metadata,
    find_all_branches,
    parse_all_with_uuids,
    parse_jsonl_file,
)
from ccrecall.schema import SCHEMA
from ccrecall.search_conversations import search_messages
from ccrecall.session_ops import sync_session

NOTIF_FIXTURE = FIXTURE_DIR / "with_notifications.jsonl"


def _setup_db_and_import(filepath: Path) -> sqlite3.Connection:
    """Create in-memory DB and import a fixture file, flagging notifications."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.commit()
    cursor = conn.cursor()

    # Create project and session
    cursor.execute(
        "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
        ("/home/user/project", "-home-user-project", "project"),
    )
    project_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
        ("notif-test-session", project_id),
    )
    session_id = cursor.lastrowid

    # Parse and import messages
    all_entries = list(parse_all_with_uuids(filepath))
    messages = list(parse_jsonl_file(filepath))

    for entry in messages:
        entry_type = entry.get("type")
        if entry_type not in ("user", "assistant"):
            continue
        message = entry.get("message", {})
        content = message.get("content", "")
        if entry_type == "user" and is_tool_result(content):
            continue
        notification = (
            1 if (entry_type == "user" and (is_task_notification(content) or is_teammate_message(content))) else 0
        )
        text, has_tool_use, has_thinking, tool_summary, tool_content = extract_text_content(content)
        if not text and not tool_content:
            continue
        cursor.execute(
            """
            INSERT INTO messages (session_id, uuid, parent_uuid, timestamp, role, content,
                                  tool_summary, has_tool_use, has_thinking, is_notification, tool_content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, uuid) DO NOTHING
        """,
            (
                session_id,
                entry.get("uuid"),
                entry.get("parentUuid"),
                entry.get("timestamp"),
                entry_type,
                text,
                tool_summary,
                has_tool_use,
                has_thinking,
                notification,
                tool_content,
            ),
        )

    # Build branches
    branches = find_all_branches(all_entries)
    cursor.execute(
        "SELECT id, uuid FROM messages WHERE session_id = ? AND uuid IS NOT NULL",
        (session_id,),
    )
    uuid_to_msg_id = {row[1]: row[0] for row in cursor.fetchall()}

    for branch in branches:
        branch_msgs = [m for m in messages if m.get("uuid") in branch["uuids"]]
        branch_msgs.sort(key=lambda e: e.get("timestamp") or "")
        exchange_count, files, commits, _tool_counts = compute_branch_metadata(branch_msgs)

        cursor.execute(
            """
            INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count)
            VALUES (?, ?, ?, ?)
        """,
            (session_id, branch["leaf_uuid"], int(branch["is_active"]), exchange_count),
        )
        branch_db_id = cursor.lastrowid

        for uuid in branch["uuids"]:
            msg_id = uuid_to_msg_id.get(uuid)
            if msg_id:
                cursor.execute(
                    "INSERT OR IGNORE INTO branch_messages VALUES (?, ?)",
                    (branch_db_id, msg_id),
                )

        agg = build_aggregated_content(cursor, branch_db_id, files, commits)
        cursor.execute(
            "UPDATE branches SET aggregated_content = ? WHERE id = ?",
            (agg, branch_db_id),
        )

    conn.commit()
    return conn


class TestNotificationEndToEnd:
    def test_notifications_flagged(self):
        """Notification messages should have is_notification=1."""
        conn = _setup_db_and_import(NOTIF_FIXTURE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM messages WHERE is_notification = 1")
        assert cursor.fetchone()[0] == 2  # Two task-notification messages

        cursor.execute("SELECT COUNT(*) FROM messages WHERE is_notification = 0 AND role = 'user'")
        assert cursor.fetchone()[0] == 2  # Two real user messages
        conn.close()

    def test_aggregate_excludes_notifications(self):
        """Branch aggregated content should not contain notification text."""
        conn = _setup_db_and_import(NOTIF_FIXTURE)
        cursor = conn.cursor()
        cursor.execute("SELECT aggregated_content FROM branches WHERE is_active = 1")
        agg = cursor.fetchone()[0]
        assert "<task-notification>" not in agg
        assert "Research AI agent memory" in agg or "summarize the key takeaways" in agg
        conn.close()

    def test_exchange_count_correct(self):
        """Exchange count should reflect human exchanges only, not notifications."""
        conn = _setup_db_and_import(NOTIF_FIXTURE)
        cursor = conn.cursor()
        cursor.execute("SELECT exchange_count FROM branches WHERE is_active = 1")
        count = cursor.fetchone()[0]
        assert count == 2  # "Research AI agent memory" and "summarize the key takeaways"
        conn.close()

    def test_context_injection_query_excludes_notifications(self):
        """The query pattern used by memory-context.py should exclude notifications."""
        conn = _setup_db_and_import(NOTIF_FIXTURE)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM branches WHERE is_active = 1")
        branch_db_id = cursor.fetchone()[0]

        # This mirrors the query in memory-context.py
        cursor.execute(
            """
            SELECT m.role, m.content, m.timestamp
            FROM branch_messages bm
            JOIN messages m ON bm.message_id = m.id
            WHERE bm.branch_id = ?
              AND COALESCE(m.is_notification, 0) = 0
            ORDER BY m.timestamp ASC
        """,
            (branch_db_id,),
        )
        messages = cursor.fetchall()

        roles = [m[0] for m in messages]
        contents = [m[1] for m in messages]
        assert "user" in roles
        assert "assistant" in roles
        for content in contents:
            assert "<task-notification>" not in content
        conn.close()

    def test_include_notifications_query(self):
        """With notifications included, all messages should be returned."""
        conn = _setup_db_and_import(NOTIF_FIXTURE)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM branches WHERE is_active = 1")
        branch_db_id = cursor.fetchone()[0]

        # Without filter (include_notifications=True pattern)
        cursor.execute(
            """
            SELECT m.role, m.content, COALESCE(m.is_notification, 0) as is_notification
            FROM branch_messages bm
            JOIN messages m ON bm.message_id = m.id
            WHERE bm.branch_id = ?
            ORDER BY m.timestamp ASC
        """,
            (branch_db_id,),
        )
        messages = cursor.fetchall()

        notif_count = sum(1 for m in messages if m[2] == 1)
        assert notif_count == 2
        assert len(messages) > notif_count  # Should also have regular messages
        conn.close()


TEAMMATE_FIXTURE = FIXTURE_DIR / "with_teammate_messages.jsonl"


class TestTeammateMessageEndToEnd:
    def test_teammate_messages_flagged(self):
        """Teammate messages should have is_notification=1."""
        conn = _setup_db_and_import(TEAMMATE_FIXTURE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM messages WHERE is_notification = 1")
        assert cursor.fetchone()[0] == 2  # Two teammate messages (report + idle)

        cursor.execute("SELECT COUNT(*) FROM messages WHERE is_notification = 0 AND role = 'user'")
        assert cursor.fetchone()[0] == 2  # Two real user messages
        conn.close()

    def test_aggregate_excludes_teammate_messages(self):
        """Branch aggregated content should not contain teammate message text."""
        conn = _setup_db_and_import(TEAMMATE_FIXTURE)
        cursor = conn.cursor()
        cursor.execute("SELECT aggregated_content FROM branches WHERE is_active = 1")
        agg = cursor.fetchone()[0]
        assert "<teammate-message" not in agg
        assert "idle_notification" not in agg
        assert "Implement the security fixes" in agg or "commit and push" in agg
        conn.close()

    def test_exchange_count_excludes_teammate_messages(self):
        """Exchange count should reflect human exchanges only, not teammate messages."""
        conn = _setup_db_and_import(TEAMMATE_FIXTURE)
        cursor = conn.cursor()
        cursor.execute("SELECT exchange_count FROM branches WHERE is_active = 1")
        count = cursor.fetchone()[0]
        assert count == 2  # "Implement the security fixes" and "awesome, looks great. now commit"
        conn.close()

    def test_context_injection_query_excludes_teammate_messages(self):
        """The query pattern used by memory-context.py should exclude teammate messages."""
        conn = _setup_db_and_import(TEAMMATE_FIXTURE)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM branches WHERE is_active = 1")
        branch_db_id = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT m.role, m.content, m.timestamp
            FROM branch_messages bm
            JOIN messages m ON bm.message_id = m.id
            WHERE bm.branch_id = ?
              AND COALESCE(m.is_notification, 0) = 0
            ORDER BY m.timestamp ASC
        """,
            (branch_db_id,),
        )
        messages = cursor.fetchall()

        for _, content, _ in messages:
            assert "<teammate-message" not in content
            assert "idle_notification" not in content
        conn.close()


# Tool content indexing: full pipeline (sync -> FTS -> embedding -> search ->
# snippet display) against real sync_session/backfill/search entry points,
# rather than the hand-rolled import loop above (which predates tool_content
# and is scoped to notification classification).


class TestToolOnlyTurnPersistence:
    """Assistant turns with only tool_use blocks (no prose) still produce
    a messages row, with content='' and tool_content populated."""

    def test_tool_only_turn_produces_row(self, memory_db, tmp_path):
        filepath = tmp_path / "sess-toolonly.jsonl"
        _write_jsonl(
            filepath,
            [
                _entry("u1", None, "2026-01-01T10:00:00Z", "user", "Check disk usage on the box"),
                _entry(
                    "a1",
                    "u1",
                    "2026-01-01T10:00:05Z",
                    "assistant",
                    [{"type": "tool_use", "name": "Bash", "input": {"command": "df -h"}}],
                ),
            ],
        )

        sync_session(memory_db, filepath, tmp_path)

        cursor = memory_db.cursor()
        cursor.execute("SELECT content, tool_content FROM messages WHERE tool_content != ''")
        rows = cursor.fetchall()
        assert rows, "expected at least one row with populated tool_content"

        tool_only_rows = [r for r in rows if r[0] == ""]
        assert tool_only_rows, "tool-only assistant turn should produce a row with content=''"
        assert any("[Bash: df -h]" in r[1] for r in tool_only_rows)


class TestFTSIncludesToolContent:
    """Aggregated_content includes a __tools__ section with tool markers
    after sync."""

    def test_aggregated_content_contains_tool_markers(self, memory_db, tmp_path):
        filepath = tmp_path / "sess-toolfts.jsonl"
        _write_jsonl(
            filepath,
            [
                _entry("u1", None, "2026-01-01T10:00:00Z", "user", "Check disk usage on the box"),
                _entry(
                    "a1",
                    "u1",
                    "2026-01-01T10:00:05Z",
                    "assistant",
                    [
                        {"type": "text", "text": "Let me check"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "df -h"}},
                    ],
                ),
            ],
        )

        sync_session(memory_db, filepath, tmp_path)

        cursor = memory_db.cursor()
        cursor.execute("SELECT aggregated_content FROM branches WHERE is_active = 1")
        agg = cursor.fetchone()[0]
        assert "__tools__" in agg
        assert "[Bash: df -h]" in agg


def _run_backfill(conn: sqlite3.Connection, **kwargs):
    with (
        patch("ccrecall.hooks.backfill_tool_content.get_connection", return_value=NoCloseConn(conn)),
        patch("ccrecall.hooks.backfill_tool_content.load_settings_for_db", return_value={}),
        patch("ccrecall.hooks.backfill_tool_content.time.sleep"),
    ):
        return run_backfill_tool_content(**kwargs)


class TestBackfillPopulatesToolContent:
    """Backfill re-parses JSONL to populate tool_content on rows that
    predate the feature, rebuilds aggregated_content, and resets
    embedding_version so backfill embeddings re-selects the branch."""

    def test_backfill_populates_and_resets_embedding_version(self, memory_db, tmp_path):
        filepath = tmp_path / "sess-backfillme.jsonl"
        _write_jsonl(
            filepath,
            [
                _entry("u1", None, "2026-01-01T10:00:00Z", "user", "Check disk usage on the box"),
                _entry(
                    "a1",
                    "u1",
                    "2026-01-01T10:00:05Z",
                    "assistant",
                    [{"type": "tool_use", "name": "Bash", "input": {"command": "df -h"}}],
                ),
            ],
        )

        sync_session(memory_db, filepath, tmp_path)

        cursor = memory_db.cursor()
        cursor.execute("SELECT id FROM sessions WHERE uuid = ?", (filepath.stem,))
        session_id = cursor.fetchone()[0]

        # Simulate the OLD (pre-tool-content) shape: tool_content wiped back to
        # NULL, aggregated_content stale, and the branch already embedded (a
        # non-NULL watermark) so we can prove the backfill resets it.
        cursor.execute("UPDATE messages SET tool_content = NULL WHERE session_id = ? AND uuid = ?", (session_id, "a1"))
        cursor.execute(
            "UPDATE branches SET aggregated_content = 'stale', embedding_version = 99 WHERE session_id = ?",
            (session_id,),
        )
        memory_db.commit()

        cursor.execute("SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "a1"))
        assert cursor.fetchone()[0] is None, "precondition: tool_content should be NULL before backfill"

        code = _run_backfill(memory_db)
        assert code == 0

        cursor.execute("SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "a1"))
        assert cursor.fetchone()[0] == "[Bash: df -h]"

        cursor.execute("SELECT aggregated_content, embedding_version FROM branches WHERE session_id = ?", (session_id,))
        agg, embedding_version = cursor.fetchone()
        assert "[Bash: df -h]" in agg
        assert embedding_version is None, "embedding_version must reset to NULL so backfill embeddings re-selects it"


class TestBackfillSkipsMissingJsonl:
    """A session whose JSONL file no longer exists on disk is skipped
    with a logged warning, and the run completes without raising."""

    def test_missing_jsonl_logged_and_skipped(self, memory_db, tmp_path, caplog):
        filepath = tmp_path / "sess-vanished.jsonl"
        _write_jsonl(
            filepath,
            [
                _entry("u1", None, "2026-01-01T10:00:00Z", "user", "Check disk usage on the box"),
                _entry(
                    "a1",
                    "u1",
                    "2026-01-01T10:00:05Z",
                    "assistant",
                    [{"type": "tool_use", "name": "Bash", "input": {"command": "df -h"}}],
                ),
            ],
        )

        sync_session(memory_db, filepath, tmp_path)

        cursor = memory_db.cursor()
        cursor.execute("SELECT id FROM sessions WHERE uuid = ?", (filepath.stem,))
        session_id = cursor.fetchone()[0]
        cursor.execute("UPDATE messages SET tool_content = NULL WHERE session_id = ? AND uuid = ?", (session_id, "a1"))
        memory_db.commit()

        filepath.unlink()  # simulate the JSONL vanishing since import

        with caplog.at_level(logging.WARNING, logger="ccrecall"):
            code = _run_backfill(memory_db)

        assert code == 0, "a missing file must not abort the whole run"
        assert any("missing" in r.message.lower() or "no on-disk" in r.message.lower() for r in caplog.records)

        # Untouched: still NULL, since the session was skipped rather than backfilled.
        cursor.execute("SELECT tool_content FROM messages WHERE session_id = ? AND uuid = ?", (session_id, "a1"))
        assert cursor.fetchone()[0] is None


class TestSearchMessagesToolContentMatch:
    """Central motivating scenario — an AskUserQuestion about retrying a task
    is synced, embedded, and found by search-messages, with the tool content
    visible in the returned snippet."""

    @VEC_SKIP
    def test_search_messages_surfaces_ask_user_question(self, tmp_path):
        conn = make_vec_conn()
        filepath = tmp_path / "sess-retry.jsonl"
        _write_jsonl(
            filepath,
            [
                _entry("u1", None, "2026-01-01T10:00:00Z", "user", "The task failed, what should we do?"),
                _entry(
                    "a1",
                    "u1",
                    "2026-01-01T10:00:05Z",
                    "assistant",
                    [
                        {
                            "type": "tool_use",
                            "name": "AskUserQuestion",
                            "input": {
                                "questions": [
                                    {
                                        "question": "The task failed with an error.",
                                        "options": [
                                            {
                                                "label": "Retry",
                                                "description": "retry task now with the fix applied",
                                            },
                                            {"label": "Abandon", "description": "stop working on this task"},
                                        ],
                                    }
                                ]
                            },
                        }
                    ],
                ),
            ],
        )

        fake_vec = [1.0 / EMBEDDING_DIM**0.5] * EMBEDDING_DIM
        with patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kwargs: [fake_vec] * len(texts)):
            sync_session(conn, filepath, tmp_path)

        with (
            patch("ccrecall.search_conversations.model_available", return_value=True),
            patch("ccrecall.search_conversations.embed_text", return_value=fake_vec),
        ):
            snippets, ranked = search_messages(conn, "retry task", max_results=5)

        assert ranked is True
        assert snippets, "expected at least one snippet"
        assert any("retry task" in (s.get("assistant") or "").lower() for s in snippets), (
            f"expected the AskUserQuestion tool content in a snippet's assistant text, got: {snippets}"
        )
        conn.close()


# Sync-current memory fix (design/specs/015-sync-memory-fix): the sync path
# caps embeddings at SYNC_PATH_TOKEN_LIMIT (4096) for memory safety while
# backfill caps at MODEL_TOKEN_LIMIT (8192). These integration tests exercise
# the assembled behavior across embed_branch_chunks, _diff_exchanges,
# _should_stamp_watermark, and build_selection with real (unmocked)
# cap_for_embedding/tokenization -- only embed_batch (the actual onnxruntime
# inference call) is mocked out.

# ~31k chars / ~5.7k tokens for the sentence below repeated 300 times, combined
# with a short assistant reply: over SYNC_PATH_TOKEN_LIMIT (4096) but under
# both MODEL_TOKEN_LIMIT (8192) and EMBED_CHAR_BUDGET (32000 chars) -- so the
# exchange is truncated at the sync cap but fits untouched at the backfill cap.
_LONG_EXCHANGE_SENTENCE = (
    "The quick brown fox jumps over the lazy dog and continues running through the forest looking for food. "
)
_LONG_EXCHANGE_REPEATS = 300
_LONG_EXCHANGE_ASSISTANT_REPLY = "Understood, here is my response to that long message."


def _long_exchange_msgs() -> list[dict]:
    """One user/assistant exchange whose combined text is >4096 and <8192 tokens."""
    return [
        {
            "role": "user",
            "content": _LONG_EXCHANGE_SENTENCE * _LONG_EXCHANGE_REPEATS,
            "timestamp": "2026-01-01T00:00:00",
            "uuid": "long-user-uuid",
        },
        {
            "role": "assistant",
            "content": _LONG_EXCHANGE_ASSISTANT_REPLY,
            "timestamp": "2026-01-01T00:00:30",
            "uuid": None,
        },
    ]


def _seed_embeddable_branch(cursor: sqlite3.Cursor) -> int:
    """Seed project/session/branch/message rows so the branch satisfies
    CHUNK_EMBEDDABLE_BRANCH_FILTER (is_active=1, >=1 branch_messages row) --
    required for build_selection to consider it a candidate at all."""
    cursor.execute(
        "INSERT INTO projects (path, key) VALUES (?, ?)",
        ("/test/lifecycle-proj", "lifecycle-proj"),
    )
    project_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
        ("lifecycle-session", project_id),
    )
    session_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO branches (session_id, leaf_uuid, is_active, embedding_version) VALUES (?, ?, 1, 0)",
        (session_id, "lifecycle-leaf"),
    )
    branch_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, 'user', 'seed', ?)",
        (session_id, "seed-msg", "2026-01-01T00:00:00"),
    )
    message_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
        (branch_id, message_id),
    )
    return branch_id


class TestEmbeddingLifecycle:
    """Integration tests for the sync -> backfill -> sync embedding lifecycle
    (design/specs/015-sync-memory-fix): no ping-pong between cap tiers (AC#4)
    and the backfill watermark/selection handoff (AC#9)."""

    @VEC_SKIP
    def test_no_ping_pong_across_sync_and_backfill_cap_tiers(self):
        """AC#4: sync-cap embed (4096) -> backfill upgrade (8192) -> sync-cap
        embed again must NOT re-embed the now-full-quality chunk."""
        conn = make_vec_conn()
        cursor = conn.cursor()
        branch_id = _seed_embeddable_branch(cursor)
        conn.commit()

        msgs = _long_exchange_msgs()
        fake_vec = [0.1] * EMBEDDING_DIM

        with patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kw: [fake_vec] * len(texts)):
            # Step 1: sync-path embed, capped at SYNC_PATH_TOKEN_LIMIT (4096).
            count1 = embed_branch_chunks(
                cursor, branch_id, msgs, is_active=True, vec_writable=True, cap_limit=SYNC_PATH_TOKEN_LIMIT
            )
            assert count1 == 1, "sync-path embed must embed the over-cap exchange"
            cursor.execute("SELECT cap_tokens FROM chunks WHERE branch_id = ?", (branch_id,))
            assert cursor.fetchone()[0] == SYNC_PATH_TOKEN_LIMIT

            # Step 2: backfill upgrades to MODEL_TOKEN_LIMIT (8192). The text fits
            # without truncation at 8192, so cap_tokens becomes NULL (full quality).
            count2 = embed_branch_chunks(
                cursor,
                branch_id,
                msgs,
                is_active=True,
                vec_writable=True,
                cap_limit=MODEL_TOKEN_LIMIT,
                max_embeds=None,
            )
            assert count2 == 1, "backfill must re-embed the draft-quality chunk"
            cursor.execute("SELECT cap_tokens FROM chunks WHERE branch_id = ?", (branch_id,))
            assert cursor.fetchone()[0] is None, "untruncated text at 8192 must store cap_tokens=NULL"

            # Step 3: sync-path embed again -- content_hash is unchanged (raw text
            # never changed) and cap_tokens IS NULL (>= SYNC_PATH_TOKEN_LIMIT), so
            # no ping-pong re-embed.
            count3 = embed_branch_chunks(
                cursor, branch_id, msgs, is_active=True, vec_writable=True, cap_limit=SYNC_PATH_TOKEN_LIMIT
            )
            assert count3 == 0, "no-ping-pong: sync path must not re-embed a full-quality chunk"

        conn.close()

    @VEC_SKIP
    def test_backfill_lifecycle_watermark_and_selection(self):
        """AC#9: sync-cap embed withholds the watermark and build_selection
        includes the branch; backfill upgrade stamps the watermark and
        build_selection excludes the branch."""
        conn = make_vec_conn()
        cursor = conn.cursor()
        branch_id = _seed_embeddable_branch(cursor)
        conn.commit()

        msgs = _long_exchange_msgs()
        fake_vec = [0.1] * EMBEDDING_DIM

        with patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kw: [fake_vec] * len(texts)):
            # Step 1: sync-path embed at SYNC_PATH_TOKEN_LIMIT (4096).
            embed_branch_chunks(
                cursor, branch_id, msgs, is_active=True, vec_writable=True, cap_limit=SYNC_PATH_TOKEN_LIMIT
            )
            conn.commit()

            cursor.execute("SELECT embedding_version FROM branches WHERE id = ?", (branch_id,))
            assert cursor.fetchone()[0] == 0, "watermark must be withheld for a draft-quality chunk"

            where, params = build_selection(None)
            cursor.execute(f"SELECT id FROM branches {where}", params)
            selected_ids = {row[0] for row in cursor.fetchall()}
            assert branch_id in selected_ids, "build_selection must select the draft-quality branch"

            # Step 2: backfill upgrades to MODEL_TOKEN_LIMIT (8192).
            embed_branch_chunks(
                cursor,
                branch_id,
                msgs,
                is_active=True,
                vec_writable=True,
                cap_limit=MODEL_TOKEN_LIMIT,
                max_embeds=None,
            )
            conn.commit()

            cursor.execute("SELECT embedding_version, embedding_model FROM branches WHERE id = ?", (branch_id,))
            stamped_version, stamped_model = cursor.fetchone()
            assert stamped_version == EMBEDDING_VERSION, "watermark must be stamped once every chunk is full quality"
            assert stamped_model == EMBEDDING_MODEL

            where, params = build_selection(None)
            cursor.execute(f"SELECT id FROM branches {where}", params)
            selected_ids = {row[0] for row in cursor.fetchall()}
            assert branch_id not in selected_ids, "build_selection must exclude the now full-quality branch"

        conn.close()
