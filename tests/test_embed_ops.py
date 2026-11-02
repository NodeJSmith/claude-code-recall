"""Tests for embed_ops.py: content-hash derivation, cap-tokens tracking, and cap
parameterization.
"""

import hashlib
import logging
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import VEC_SKIP, embed_messages, make_vec_conn, set_branch_messages

from ccrecall.config import PID_KEY_BACKFILL_EMBEDDINGS, pid_file_path
from ccrecall.db_base import is_database_locked
from ccrecall.db_vec import _ensure_vec_schema, vec_available
from ccrecall.embed_ops import (
    _content_hash,
    _diff_exchanges,
    _prepare_exchange_data,
    _should_stamp_watermark,
    _write_embedded_chunks,
    embed_branch_chunks,
    resolve_sync_cap,
)
from ccrecall.embeddings import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    EMBEDDING_VERSION,
    MODEL_TOKEN_LIMIT,
    SYNC_PATH_TOKEN_LIMIT,
)
from ccrecall.schema import SCHEMA


def _exchange(
    index: int = 0,
    user: str = "hello",
    assistant: str = "hi there",
    timestamp: str = "2024-01-01T00:00:00",
    first_message_uuid: str = "u1",
) -> dict:
    return {
        "index": index,
        "user": user,
        "assistant": assistant,
        "timestamp": timestamp,
        "first_message_uuid": first_message_uuid,
    }


def _msgs(*exchange_pairs: tuple[str, str]) -> list[dict]:
    """Return user/assistant message dicts for the given exchange-content tuples."""
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
        msgs.append(
            {
                "role": "assistant",
                "content": asst_content,
                "timestamp": f"2024-01-01T00:{i:02d}:30",
                "uuid": None,
            }
        )
    return msgs


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


class TestPrepareExchangeDataContentHash:
    """content_hash is derived from raw text, independent of the cap."""

    def test_hash_derived_from_raw_text_not_capped_output(self, monkeypatch):
        """The stored content_hash matches hashing the raw combined text, even when
        cap_for_embedding returns a heavily truncated form — proving the hash is
        computed BEFORE capping, not from the capped output."""
        combined = "hello\n\nhi there"
        expected_hash = hashlib.sha256(combined.encode()).hexdigest()

        # cap_for_embedding always returns an unrelated truncated placeholder. If the
        # hash were derived from its output (the old, buggy behavior), it would not
        # match expected_hash.
        monkeypatch.setattr(
            "ccrecall.embed_ops.cap_for_embedding",
            lambda text, max_tokens=None: ("TRUNCATED-PLACEHOLDER", True),
        )

        result = _prepare_exchange_data([_exchange(user="hello", assistant="hi there")])

        assert result[0]["content_hash"] == expected_hash
        assert result[0]["text"] == "TRUNCATED-PLACEHOLDER"

    def test_same_raw_text_different_cap_limit_same_hash(self, monkeypatch):
        """Identical raw text hashes identically regardless of
        cap_limit — this is what prevents sync (4096) and backfill (8192) from
        ping-ponging re-embeds of the same unchanged exchange."""
        monkeypatch.setattr(
            "ccrecall.embed_ops.cap_for_embedding",
            lambda text, max_tokens=None: (text[: (max_tokens or len(text))], True),
        )

        result_sync = _prepare_exchange_data([_exchange()], cap_limit=SYNC_PATH_TOKEN_LIMIT)
        result_backfill = _prepare_exchange_data([_exchange()], cap_limit=MODEL_TOKEN_LIMIT)

        assert result_sync[0]["content_hash"] == result_backfill[0]["content_hash"]

    def test_texts_differing_past_cap_produce_different_hashes(self, monkeypatch):
        """Two exchange texts differing only past the cap mark
        produce DIFFERENT content_hash values — the hash reflects the full raw
        content, not just the portion that survives capping."""
        # cap_for_embedding truncates everything to the first 10 chars, simulating
        # two exchanges that would be identical post-cap but differ in raw content.
        monkeypatch.setattr(
            "ccrecall.embed_ops.cap_for_embedding",
            lambda text, max_tokens=None: (text[:10], True),
        )

        short_exchange = _exchange(index=0, user="0123456789", assistant="")
        long_exchange = _exchange(index=1, user="0123456789-tail-content-that-differs", assistant="")

        result = _prepare_exchange_data([short_exchange, long_exchange])

        assert result[0]["content_hash"] != result[1]["content_hash"]
        # Sanity: the capped *text* would have been identical had hashing happened
        # post-cap (both start with "0123456789") — the hashes differ anyway.
        assert result[0]["text"] == result[1]["text"] == "0123456789"


class TestPrepareExchangeDataCapTokens:
    """cap_tokens is cap_limit when truncated, else NULL."""

    def test_was_capped_true_stores_cap_limit(self, monkeypatch):
        monkeypatch.setattr(
            "ccrecall.embed_ops.cap_for_embedding",
            lambda text, max_tokens=None: (text, True),
        )
        result = _prepare_exchange_data([_exchange()], cap_limit=4096)
        assert result[0]["cap_tokens"] == 4096

    def test_was_capped_false_stores_none(self, monkeypatch):
        monkeypatch.setattr(
            "ccrecall.embed_ops.cap_for_embedding",
            lambda text, max_tokens=None: (text, False),
        )
        result = _prepare_exchange_data([_exchange()], cap_limit=4096)
        assert result[0]["cap_tokens"] is None


class TestPrepareExchangeDataCapLimitThreading:
    """cap_limit is threaded to every cap_for_embedding call."""

    def test_cap_limit_passed_as_max_tokens_to_combined_and_turn_caps(self, monkeypatch):
        captured: list[int | None] = []

        def fake_cap(text, max_tokens=None):
            captured.append(max_tokens)
            return text, False

        monkeypatch.setattr("ccrecall.embed_ops.cap_for_embedding", fake_cap)

        _prepare_exchange_data([_exchange(user="hello", assistant="hi there")], cap_limit=4096)

        # Three calls per exchange: combined, user_text, assistant_text — all at the
        # same cap_limit.
        assert captured == [4096, 4096, 4096]

    def test_default_cap_limit_is_model_token_limit(self, monkeypatch):
        captured: list[int | None] = []

        def fake_cap(text, max_tokens=None):
            captured.append(max_tokens)
            return text, False

        monkeypatch.setattr("ccrecall.embed_ops.cap_for_embedding", fake_cap)

        _prepare_exchange_data([_exchange()])

        assert captured == [MODEL_TOKEN_LIMIT, MODEL_TOKEN_LIMIT, MODEL_TOKEN_LIMIT]


class TestWriteEmbeddedChunksCapTokens:
    """_write_embedded_chunks persists cap_tokens to the chunks row."""

    def test_cap_tokens_written_to_chunks_row(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        conn.commit()
        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        needing_embed = [
            {
                "index": 0,
                "content_hash": "hash0",
                "first_message_uuid": "u0",
                "timestamp": "2024-01-01T00:00:00",
                "user_text": "u",
                "assistant_text": "a",
                "cap_tokens": 4096,
            },
            {
                "index": 1,
                "content_hash": "hash1",
                "first_message_uuid": "u1",
                "timestamp": "2024-01-01T00:01:00",
                "user_text": "u2",
                "assistant_text": "a2",
                "cap_tokens": None,
            },
        ]
        fake_vec = [0.1] * EMBEDDING_DIM

        with patch("ccrecall.embed_ops.write_chunk_embedding"):
            _write_embedded_chunks(cursor, branch_id, needing_embed, [fake_vec, fake_vec])

        cursor.execute(
            "SELECT exchange_index, cap_tokens FROM chunks WHERE branch_id = ? ORDER BY exchange_index",
            (branch_id,),
        )
        rows = cursor.fetchall()
        assert rows == [(0, 4096), (1, None)]
        conn.close()


class TestEmbedBranchChunksCapLimit:
    """embed_branch_chunks threads cap_limit to cap_for_embedding
    and embed_batch (via _prepare_exchange_data / embed_batch's max_token_cap)."""

    def test_default_cap_limit_caps_at_model_token_limit(self, tmp_path):
        """Default embed_branch_chunks call caps at MODEL_TOKEN_LIMIT."""
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")
        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        captured_cap_calls: list[int | None] = []
        real_texts = _msgs(("Hello", "Hi there"))
        fake_vec = [0.1] * EMBEDDING_DIM

        def fake_cap(text, max_tokens=None):
            captured_cap_calls.append(max_tokens)
            return text, False

        with (
            patch("ccrecall.embed_ops.cap_for_embedding", side_effect=fake_cap),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kw: [fake_vec] * len(texts)),
        ):
            embed_messages(conn, branch_id, real_texts)

        assert captured_cap_calls
        assert all(c == MODEL_TOKEN_LIMIT for c in captured_cap_calls)

        cursor.execute("SELECT cap_tokens FROM chunks WHERE branch_id = ?", (branch_id,))
        assert cursor.fetchone()[0] is None, "untruncated exchange must store cap_tokens=NULL"
        conn.close()

    def test_default_cap_limit_stores_8192_for_truncated_text(self, tmp_path):
        """embed_branch_chunks with default cap_limit produces cap_tokens=8192
        for exchanges that were actually truncated (was_capped=True)."""
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")
        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        real_texts = _msgs(("Hello", "Hi there"))
        fake_vec = [0.1] * EMBEDDING_DIM

        with (
            patch("ccrecall.embed_ops.cap_for_embedding", side_effect=lambda text, max_tokens=None: (text, True)),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kw: [fake_vec] * len(texts)),
        ):
            embed_messages(conn, branch_id, real_texts)

        cursor.execute("SELECT cap_tokens FROM chunks WHERE branch_id = ?", (branch_id,))
        assert cursor.fetchone()[0] == MODEL_TOKEN_LIMIT
        conn.close()

    def test_explicit_cap_limit_caps_at_4096(self, tmp_path):
        """embed_branch_chunks(cap_limit=4096) caps at 4096."""
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")
        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        captured_cap_calls: list[int | None] = []
        real_texts = _msgs(("Hello", "Hi there"))
        fake_vec = [0.1] * EMBEDDING_DIM

        def fake_cap(text, max_tokens=None):
            captured_cap_calls.append(max_tokens)
            return text, True  # simulate truncation to exercise cap_tokens storage

        with (
            patch("ccrecall.embed_ops.cap_for_embedding", side_effect=fake_cap),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kw: [fake_vec] * len(texts)),
        ):
            embed_messages(conn, branch_id, real_texts, cap_limit=4096)

        assert captured_cap_calls
        assert all(c == 4096 for c in captured_cap_calls)

        cursor.execute("SELECT cap_tokens FROM chunks WHERE branch_id = ?", (branch_id,))
        assert cursor.fetchone()[0] == 4096
        conn.close()

    def test_cap_limit_threaded_to_embed_batch_max_token_cap(self, tmp_path):
        """embed_branch_chunks passes cap_limit to embed_batch as max_token_cap."""
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")
        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        real_texts = _msgs(("Hello", "Hi there"))
        fake_vec = [0.1] * EMBEDDING_DIM

        with patch("ccrecall.embed_ops.embed_batch", return_value=[fake_vec]) as mock_embed_batch:
            embed_messages(conn, branch_id, real_texts, cap_limit=4096)

        mock_embed_batch.assert_called_once()
        _, kwargs = mock_embed_batch.call_args
        assert kwargs.get("max_token_cap") == 4096
        conn.close()


class TestEmbedBranchChunksCapExceededWarning:
    """embed_branch_chunks emits a path-aware WARNING for exchanges the
    caller's cap_limit actually truncated (was_capped=True)."""

    def test_warns_when_exchange_was_capped(self, tmp_path, caplog):
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")
        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        real_texts = _msgs(("Hello", "Hi there"))
        fake_vec = [0.1] * EMBEDDING_DIM

        with (
            patch("ccrecall.embed_ops.cap_for_embedding", side_effect=lambda text, max_tokens=None: (text, True)),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kw: [fake_vec] * len(texts)),
            caplog.at_level(logging.WARNING, logger="ccrecall"),
        ):
            embed_messages(conn, branch_id, real_texts, cap_limit=4096)

        warnings = [r for r in caplog.records if r.message == "exchange truncated for embedding"]
        assert warnings, "was_capped=True exchange must emit a WARNING"
        assert warnings[0].levelno == logging.WARNING
        assert warnings[0].cap == 4096
        conn.close()

    def test_no_warning_when_not_capped(self, tmp_path, caplog):
        conn = _make_vec_conn(tmp_path)
        if conn is None:
            pytest.skip("sqlite-vec not available")
        cursor = conn.cursor()
        branch_id = _seed_branch(cursor)
        conn.commit()

        real_texts = _msgs(("Hello", "Hi there"))
        fake_vec = [0.1] * EMBEDDING_DIM

        with (
            patch("ccrecall.embed_ops.cap_for_embedding", side_effect=lambda text, max_tokens=None: (text, False)),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kw: [fake_vec] * len(texts)),
            caplog.at_level(logging.WARNING, logger="ccrecall"),
        ):
            embed_messages(conn, branch_id, real_texts, cap_limit=4096)

        warnings = [r for r in caplog.records if r.message == "exchange truncated for embedding"]
        assert not warnings, "untruncated exchange must not emit the cap-exceeded WARNING"
        conn.close()


class TestDiffExchangesCapTokensUpgrade:
    """_diff_exchanges flags cap-tokens upgrades even when
    content_hash matches, and is NULL-safe. See design/specs/015-sync-memory-fix."""

    def test_flags_chunk_when_cap_tokens_below_target(self):
        """A chunk with cap_tokens=4096 is flagged when target_cap=MODEL_TOKEN_LIMIT
        (backfill context), even though content_hash matches."""
        exchange_data = [{"index": 0, "content_hash": "hash0"}]
        existing_chunks = {0: {"content_hash": "hash0", "cap_tokens": 4096}}

        needing_embed = _diff_exchanges(exchange_data, existing_chunks, target_cap=MODEL_TOKEN_LIMIT)

        assert needing_embed == exchange_data

    def test_does_not_flag_chunk_when_cap_tokens_meets_target(self):
        """After upgrading to MODEL_TOKEN_LIMIT, the sync-context
        target_cap (SYNC_PATH_TOKEN_LIMIT) does not flag it again — no ping-pong."""
        exchange_data = [{"index": 0, "content_hash": "hash0"}]
        existing_chunks = {0: {"content_hash": "hash0", "cap_tokens": MODEL_TOKEN_LIMIT}}

        needing_embed = _diff_exchanges(exchange_data, existing_chunks, target_cap=SYNC_PATH_TOKEN_LIMIT)

        assert needing_embed == []

    def test_null_cap_tokens_treated_as_full_quality(self):
        """cap_tokens=NULL (pre-migration/untruncated) does not raise and is
        treated as full-quality via effective_cap_tokens — not flagged."""
        exchange_data = [{"index": 0, "content_hash": "hash0"}]
        existing_chunks = {0: {"content_hash": "hash0", "cap_tokens": None}}

        needing_embed = _diff_exchanges(exchange_data, existing_chunks, target_cap=MODEL_TOKEN_LIMIT)

        assert needing_embed == []

    def test_default_target_cap_is_model_token_limit(self):
        exchange_data = [{"index": 0, "content_hash": "hash0"}]
        existing_chunks = {0: {"content_hash": "hash0", "cap_tokens": 4096}}

        needing_embed = _diff_exchanges(exchange_data, existing_chunks)

        assert needing_embed == exchange_data

    def test_hash_mismatch_still_flags_independent_of_cap_tokens(self):
        """The existing hash-mismatch condition is additive, not replaced by the
        cap-tokens check."""
        exchange_data = [{"index": 0, "content_hash": "new-hash"}]
        existing_chunks = {0: {"content_hash": "old-hash", "cap_tokens": None}}

        needing_embed = _diff_exchanges(exchange_data, existing_chunks, target_cap=MODEL_TOKEN_LIMIT)

        assert needing_embed == exchange_data


def _chunk(content_hash: str = "hash0", cap_tokens: int | None = None, **overrides) -> dict:
    return {
        "content_hash": content_hash,
        "embedding_version": EMBEDDING_VERSION,
        "embedding_model": EMBEDDING_MODEL,
        "cap_tokens": cap_tokens,
        "has_vector": True,
        **overrides,
    }


class TestDiffExchangesReplaceStale:
    """Only backfill selects chunks that aren't current; sync leaves them to it."""

    @pytest.mark.parametrize(
        "stale",
        [
            {"embedding_version": 0},
            {"embedding_version": None},
            {"embedding_model": "old/model"},
            {"has_vector": False},
        ],
    )
    def test_stale_chunk_selected_only_with_replace_stale(self, stale):
        exchange_data = [{"index": 0, "content_hash": "hash0"}]
        existing = {0: _chunk(**stale)}

        assert _diff_exchanges(exchange_data, existing, target_cap=SYNC_PATH_TOKEN_LIMIT) == []
        assert _diff_exchanges(exchange_data, existing, replace_stale=True) == exchange_data

    def test_current_chunk_not_selected_with_replace_stale(self):
        exchange_data = [{"index": 0, "content_hash": "hash0"}]

        assert _diff_exchanges(exchange_data, {0: _chunk()}, replace_stale=True) == []


class TestShouldStampWatermark:
    """The stamp requires every exchange's chunk current at full quality, judged on stored state."""

    def test_current_full_quality_chunks_stamp(self):
        assert _should_stamp_watermark({0: "hash0"}, {0: _chunk()}) is True

    def test_no_exchanges_stamps(self):
        assert _should_stamp_watermark({}, {}) is True

    @pytest.mark.parametrize(
        "chunk",
        [
            _chunk(cap_tokens=4096),
            _chunk(content_hash="other"),
            _chunk(embedding_version=0),
            _chunk(embedding_model="old/model"),
            _chunk(has_vector=False),
        ],
    )
    def test_chunk_not_current_at_full_quality_withholds(self, chunk):
        assert _should_stamp_watermark({0: "hash0"}, {0: chunk}) is False

    def test_exchange_without_chunk_withholds(self):
        assert _should_stamp_watermark({0: "hash0", 1: "hash1"}, {0: _chunk()}) is False

    def test_cap_at_model_limit_is_full_quality(self):
        assert _should_stamp_watermark({0: "hash0"}, {0: _chunk(cap_tokens=MODEL_TOKEN_LIMIT)}) is True


class TestResolveSyncCap:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, SYNC_PATH_TOKEN_LIMIT),
            (0, 1),
            (-5, 1),
            (2048, 2048),
            (20000, MODEL_TOKEN_LIMIT),
            ("4096", SYNC_PATH_TOKEN_LIMIT),
            (True, SYNC_PATH_TOKEN_LIMIT),
        ],
    )
    def test_clamps_raw_setting(self, raw, expected):
        assert resolve_sync_cap(raw) == expected


_FAKE_VEC = [0.1] * EMBEDDING_DIM


def _fake_cap(text, max_tokens=None):
    """Report truncation below the model limit, so a sync-capped chunk is draft quality."""
    return text, max_tokens is not None and max_tokens < MODEL_TOKEN_LIMIT


class _FileDb:
    """A file database with two connections: one embeds, the other plays a concurrent process."""

    def __init__(self, tmp_path: Path):
        path = str(tmp_path / "concurrent.db")
        self.conn = make_vec_conn(path)
        self.other = make_vec_conn(path)
        self.other.execute("PRAGMA busy_timeout = 0")
        self.branch_id = _seed_branch(self.conn.cursor())
        self.conn.commit()

    def set_messages(self, *pairs: tuple[str, str]) -> None:
        set_branch_messages(self.conn, self.branch_id, _msgs(*pairs))

    def watermark(self):
        return self.other.execute("SELECT embedding_version FROM branches WHERE id = ?", (self.branch_id,)).fetchone()[
            0
        ]

    def chunks(self) -> dict[int, tuple]:
        rows = self.conn.execute(
            """
            SELECT c.exchange_index, c.content_hash, c.cap_tokens, c.embedding_version,
                   EXISTS (SELECT 1 FROM chunk_vec WHERE chunk_id = c.id)
            FROM chunks c WHERE c.branch_id = ?
            """,
            (self.branch_id,),
        ).fetchall()
        return {row[0]: row[1:] for row in rows}

    def embed(self, during_inference=None, **kwargs):
        """Run the embed with fake inference, calling ``during_inference`` once mid-inference."""
        pending = [during_inference] if during_inference else []

        def fake_embed(texts, **_kwargs):
            if pending:
                pending.pop()()
            return [_FAKE_VEC] * len(texts)

        with (
            patch("ccrecall.embed_ops.cap_for_embedding", side_effect=_fake_cap),
            patch("ccrecall.embed_ops.embed_batch", side_effect=fake_embed),
        ):
            return embed_branch_chunks(self.conn, self.branch_id, **kwargs)


@pytest.fixture
def file_db(tmp_path):
    db = _FileDb(tmp_path)
    yield db
    db.conn.close()
    db.other.close()


@VEC_SKIP
class TestNoLockDuringInference:
    def test_another_writer_gets_the_lock_during_inference(self, file_db):
        file_db.set_messages(("q", "a"))

        def write_elsewhere():
            assert file_db.conn.in_transaction is False
            file_db.other.execute("BEGIN IMMEDIATE")
            file_db.other.commit()

        assert file_db.embed(during_inference=write_elsewhere, max_embeds=None).stamped is True

    def test_tokenizer_runs_with_no_transaction_open(self, file_db):
        file_db.set_messages(("q", "a"))
        seen = []

        def cap(text, max_tokens=None):
            seen.append(file_db.conn.in_transaction)
            return text, False

        with (
            patch("ccrecall.embed_ops.cap_for_embedding", side_effect=cap),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kw: [_FAKE_VEC] * len(texts)),
        ):
            embed_branch_chunks(file_db.conn, file_db.branch_id)

        assert seen
        assert not any(seen)

    def test_watermark_clear_is_committed_before_inference(self, file_db):
        """A crash during inference must leave the branch selected by backfill."""
        file_db.conn.execute(
            "UPDATE branches SET embedding_version = ? WHERE id = ?", (EMBEDDING_VERSION, file_db.branch_id)
        )
        file_db.set_messages(("q", "a"))
        seen = []

        file_db.embed(during_inference=lambda: seen.append(file_db.watermark()))

        assert seen == [0]

    def test_failed_inference_leaves_existing_chunks_intact(self, file_db):
        file_db.set_messages(("q", "a"))
        file_db.embed()
        before = file_db.chunks()
        file_db.set_messages(("q", "a"), ("q2", "a2"))

        with (
            patch("ccrecall.embed_ops.cap_for_embedding", side_effect=_fake_cap),
            patch("ccrecall.embed_ops.embed_batch", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            embed_branch_chunks(file_db.conn, file_db.branch_id)

        assert file_db.chunks() == before
        assert file_db.watermark() == 0


@VEC_SKIP
class TestWriteStepRecheck:
    """The branch can change during inference; the write must judge the state at commit."""

    def test_content_changed_with_no_link_change_discards_only_that_vector(self, file_db):
        file_db.set_messages(("q0", "a0"), ("q1", "a1"))

        def repair_tool_content():
            file_db.other.execute(
                "UPDATE messages SET tool_content = '[Read: x]' WHERE content = 'a1' AND role = 'assistant'"
            )
            file_db.other.commit()

        outcome = file_db.embed(during_inference=repair_tool_content)

        assert outcome.embedded == 1
        assert outcome.stamped is False
        assert set(file_db.chunks()) == {0}
        assert file_db.watermark() == 0

    def test_exchange_added_during_inference_keeps_vectors_and_withholds_stamp(self, file_db):
        file_db.set_messages(("q0", "a0"))

        def append_turn():
            session_id = file_db.other.execute("SELECT session_id FROM branches").fetchone()[0]
            cursor = file_db.other.execute(
                "INSERT INTO messages (session_id, uuid, role, content, timestamp) "
                "VALUES (?, 'later', 'user', 'later', '2024-01-01T01:00:00')",
                (session_id,),
            )
            file_db.other.execute(
                "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
                (file_db.branch_id, cursor.lastrowid),
            )
            file_db.other.commit()

        outcome = file_db.embed(during_inference=append_turn)

        assert outcome == (1, False)
        assert set(file_db.chunks()) == {0}
        assert file_db.watermark() == 0

    def test_branch_deleted_during_inference_writes_nothing_and_does_not_raise(self, file_db):
        file_db.set_messages(("q0", "a0"))

        def delete_branch():
            file_db.other.execute("DELETE FROM branch_messages WHERE branch_id = ?", (file_db.branch_id,))
            file_db.other.execute("DELETE FROM branches WHERE id = ?", (file_db.branch_id,))
            file_db.other.commit()

        outcome = file_db.embed(during_inference=delete_branch)

        assert outcome == (0, False)
        assert file_db.chunks() == {}

    def test_pruning_uses_the_fresh_exchange_set(self, file_db):
        file_db.set_messages(("q0", "a0"), ("q1", "a1"))
        file_db.embed()
        file_db.conn.execute("UPDATE branches SET embedding_version = 0 WHERE id = ?", (file_db.branch_id,))
        file_db.conn.commit()
        session_id = file_db.conn.execute("SELECT session_id FROM branches").fetchone()[0]
        # Exchange 0's text changes so an embed runs, then exchange 1 vanishes during it.
        file_db.conn.execute("UPDATE messages SET content = 'q0 edited' WHERE content = 'q0'")
        file_db.conn.commit()

        def shrink():
            file_db.other.execute(
                "DELETE FROM branch_messages WHERE message_id IN "
                "(SELECT id FROM messages WHERE session_id = ? AND content IN ('q1', 'a1'))",
                (session_id,),
            )
            file_db.other.commit()

        outcome = file_db.embed(during_inference=shrink, max_embeds=None)

        assert set(file_db.chunks()) == {0}
        assert outcome.stamped is True

    def test_sync_vector_does_not_downgrade_a_chunk_backfill_wrote_during_inference(self, file_db):
        """A sync at 4096 finishing after a backfill at 8192 keeps the 8192 chunk and its stamp."""
        file_db.set_messages(("q0", "a0"))

        def backfill_elsewhere():
            embed_branch_chunks(
                file_db.other, file_db.branch_id, max_embeds=None, cap_limit=MODEL_TOKEN_LIMIT, replace_stale=True
            )

        outcome = file_db.embed(during_inference=backfill_elsewhere, cap_limit=SYNC_PATH_TOKEN_LIMIT)

        assert outcome == (0, True)
        assert file_db.chunks()[0][1] is None
        assert file_db.watermark() == EMBEDDING_VERSION

    def test_draft_chunk_is_replaced_by_a_better_vector(self, file_db):
        file_db.set_messages(("q0", "a0"))
        file_db.embed(cap_limit=SYNC_PATH_TOKEN_LIMIT)
        assert file_db.chunks()[0][1] == SYNC_PATH_TOKEN_LIMIT

        outcome = file_db.embed(cap_limit=MODEL_TOKEN_LIMIT, max_embeds=None, replace_stale=True)

        assert outcome == (1, True)
        assert file_db.chunks()[0][1] is None


@VEC_SKIP
class TestLockContention:
    def test_clear_timing_out_runs_no_inference(self, file_db):
        file_db.set_messages(("q", "a"))
        file_db.conn.execute("PRAGMA busy_timeout = 0")
        file_db.other.execute("BEGIN IMMEDIATE")

        with (
            patch("ccrecall.embed_ops.embed_batch") as mock_embed,
            pytest.raises(sqlite3.OperationalError) as exc_info,
        ):
            embed_branch_chunks(file_db.conn, file_db.branch_id)

        assert is_database_locked(exc_info.value)
        assert mock_embed.call_count == 0
        assert file_db.conn.in_transaction is False

    def test_write_timing_out_loses_the_inference_and_leaves_connection_usable(self, file_db):
        file_db.set_messages(("q", "a"))
        file_db.conn.execute("PRAGMA busy_timeout = 0")

        with pytest.raises(sqlite3.OperationalError) as exc_info:
            file_db.embed(during_inference=lambda: file_db.other.execute("BEGIN IMMEDIATE"))

        assert is_database_locked(exc_info.value)
        assert file_db.conn.in_transaction is False
        file_db.other.rollback()
        assert file_db.chunks() == {}
        assert file_db.watermark() == 0

        assert file_db.embed().stamped is True


@VEC_SKIP
class TestYieldToBackfill:
    def test_live_backfill_skips_inference_after_committing_the_clear(self, file_db):
        file_db.conn.execute(
            "UPDATE branches SET embedding_version = ? WHERE id = ?", (EMBEDDING_VERSION, file_db.branch_id)
        )
        file_db.set_messages(("q", "a"))
        pid_file_path(PID_KEY_BACKFILL_EMBEDDINGS).parent.mkdir(parents=True, exist_ok=True)
        pid_file_path(PID_KEY_BACKFILL_EMBEDDINGS).write_text(str(os.getppid()))

        with patch("ccrecall.embed_ops.embed_batch") as mock_embed:
            outcome = embed_branch_chunks(file_db.conn, file_db.branch_id, yield_to_backfill=True)

        assert outcome == (0, False)
        assert mock_embed.call_count == 0
        assert file_db.watermark() == 0

    def test_dead_backfill_marker_does_not_skip_and_is_left_in_place(self, file_db):
        file_db.set_messages(("q", "a"))
        marker = pid_file_path(PID_KEY_BACKFILL_EMBEDDINGS)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("99999999")

        outcome = file_db.embed(yield_to_backfill=True)

        assert outcome.embedded == 1
        assert marker.read_text() == "99999999"

    def test_backfill_itself_does_not_yield_to_its_own_marker(self, file_db):
        file_db.set_messages(("q", "a"))
        marker = pid_file_path(PID_KEY_BACKFILL_EMBEDDINGS)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(os.getpid()))

        assert file_db.embed(max_embeds=None, replace_stale=True).embedded == 1


@VEC_SKIP
class TestPathsWithoutInference:
    """Nothing to infer still goes through the write step, and needs no model."""

    def _embed_without_model(self, file_db):
        with (
            patch("ccrecall.embed_ops.cap_for_embedding", side_effect=AssertionError("tokenizer used")),
            patch("ccrecall.embed_ops.embed_batch", side_effect=AssertionError("model used")),
        ):
            return embed_branch_chunks(file_db.conn, file_db.branch_id)

    def test_watermark_repair_stamps_current_chunks(self, file_db):
        file_db.set_messages(("q", "a"))
        file_db.embed()
        file_db.conn.execute("UPDATE branches SET embedding_version = 0 WHERE id = ?", (file_db.branch_id,))
        file_db.conn.commit()

        assert self._embed_without_model(file_db) == (0, True)
        assert file_db.watermark() == EMBEDDING_VERSION

    def test_repair_withholds_stamp_when_a_vector_is_missing(self, file_db):
        file_db.set_messages(("q", "a"))
        file_db.embed()
        file_db.conn.execute("UPDATE branches SET embedding_version = 0 WHERE id = ?", (file_db.branch_id,))
        file_db.conn.execute("DELETE FROM chunk_vec")
        file_db.conn.commit()

        assert self._embed_without_model(file_db) == (0, False)
        assert file_db.watermark() == 0

    def test_prune_only_does_not_clear_the_watermark(self, file_db):
        file_db.set_messages(("q0", "a0"), ("q1", "a1"))
        file_db.embed()
        assert file_db.watermark() == EMBEDDING_VERSION
        file_db.set_messages(("q0", "a0"))
        seen = []
        real_write = file_db.conn

        def snapshot(*_args):
            seen.append(file_db.watermark())

        real_write.set_trace_callback(lambda sql: snapshot() if sql.startswith("BEGIN IMMEDIATE") else None)
        outcome = self._embed_without_model(file_db)
        real_write.set_trace_callback(None)

        assert seen == [EMBEDDING_VERSION]
        assert outcome == (0, True)
        assert set(file_db.chunks()) == {0}

    def test_zero_exchange_branch_stamps_and_loses_stale_chunks(self, file_db):
        file_db.set_messages(("q", "a"))
        file_db.embed()
        set_branch_messages(
            file_db.conn,
            file_db.branch_id,
            [{"role": "assistant", "content": "x", "timestamp": "2024-01-01T00:00:00", "uuid": None}],
        )

        assert self._embed_without_model(file_db) == (0, True)
        assert file_db.chunks() == {}


def test_content_hash_needs_no_tokenizer():
    with patch("ccrecall.embed_ops.cap_for_embedding", side_effect=AssertionError("tokenizer used")):
        expected = hashlib.sha256(b"hello\n\nhi there").hexdigest()
        assert _content_hash(_exchange()) == expected
