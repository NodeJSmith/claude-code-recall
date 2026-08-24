"""Tests for embed_ops.py: content-hash derivation, cap-tokens tracking, and cap
parameterization.
"""

import hashlib
import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from ccrecall.db_vec import _ensure_vec_schema, vec_available
from ccrecall.embed_ops import (
    _diff_exchanges,
    _prepare_exchange_data,
    _should_stamp_watermark,
    _write_embedded_chunks,
    embed_branch_chunks,
)
from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_VERSION, MODEL_TOKEN_LIMIT, SYNC_PATH_TOKEN_LIMIT
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
            embed_branch_chunks(cursor, branch_id, real_texts, is_active=True, vec_writable=True)

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
            embed_branch_chunks(cursor, branch_id, real_texts, is_active=True, vec_writable=True)

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
            embed_branch_chunks(cursor, branch_id, real_texts, is_active=True, vec_writable=True, cap_limit=4096)

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
            embed_branch_chunks(cursor, branch_id, real_texts, is_active=True, vec_writable=True, cap_limit=4096)

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
            embed_branch_chunks(cursor, branch_id, real_texts, is_active=True, vec_writable=True, cap_limit=4096)

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
            embed_branch_chunks(cursor, branch_id, real_texts, is_active=True, vec_writable=True, cap_limit=4096)

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

        needing_embed, indices_to_prune = _diff_exchanges(exchange_data, existing_chunks, target_cap=MODEL_TOKEN_LIMIT)

        assert needing_embed == exchange_data
        assert indices_to_prune == set()

    def test_does_not_flag_chunk_when_cap_tokens_meets_target(self):
        """After upgrading to MODEL_TOKEN_LIMIT, the sync-context
        target_cap (SYNC_PATH_TOKEN_LIMIT) does not flag it again — no ping-pong."""
        exchange_data = [{"index": 0, "content_hash": "hash0"}]
        existing_chunks = {0: {"content_hash": "hash0", "cap_tokens": MODEL_TOKEN_LIMIT}}

        needing_embed, indices_to_prune = _diff_exchanges(
            exchange_data, existing_chunks, target_cap=SYNC_PATH_TOKEN_LIMIT
        )

        assert needing_embed == []
        assert indices_to_prune == set()

    def test_null_cap_tokens_treated_as_full_quality(self):
        """cap_tokens=NULL (pre-migration/untruncated) does not raise and is
        treated as full-quality via effective_cap_tokens — not flagged."""
        exchange_data = [{"index": 0, "content_hash": "hash0"}]
        existing_chunks = {0: {"content_hash": "hash0", "cap_tokens": None}}

        needing_embed, _ = _diff_exchanges(exchange_data, existing_chunks, target_cap=MODEL_TOKEN_LIMIT)

        assert needing_embed == []

    def test_default_target_cap_is_model_token_limit(self):
        exchange_data = [{"index": 0, "content_hash": "hash0"}]
        existing_chunks = {0: {"content_hash": "hash0", "cap_tokens": 4096}}

        needing_embed, _ = _diff_exchanges(exchange_data, existing_chunks)

        assert needing_embed == exchange_data

    def test_hash_mismatch_still_flags_independent_of_cap_tokens(self):
        """The existing hash-mismatch condition is additive, not replaced by the
        cap-tokens check."""
        exchange_data = [{"index": 0, "content_hash": "new-hash"}]
        existing_chunks = {0: {"content_hash": "old-hash", "cap_tokens": None}}

        needing_embed, _ = _diff_exchanges(exchange_data, existing_chunks, target_cap=MODEL_TOKEN_LIMIT)

        assert needing_embed == exchange_data

    def test_cap_tokens_upgrade_does_not_add_to_prune_set(self):
        """Cap-tokens upgrades are additive to needing_embed only — pruning stays
        reserved for exchange indices that no longer exist in exchange_data."""
        exchange_data = [{"index": 0, "content_hash": "hash0"}]
        existing_chunks = {
            0: {"content_hash": "hash0", "cap_tokens": 4096},
            1: {"content_hash": "hash1", "cap_tokens": None},  # no longer in exchange_data
        }

        needing_embed, indices_to_prune = _diff_exchanges(exchange_data, existing_chunks, target_cap=MODEL_TOKEN_LIMIT)

        assert needing_embed == exchange_data
        assert indices_to_prune == {1}


class TestShouldStampWatermarkCapTokens:
    """Watermark withheld while any chunk (existing or
    freshly-embedded) is draft quality; stamped once every chunk is full quality."""

    def test_existing_chunk_below_full_quality_withholds_watermark(self):
        """An existing chunk with cap_tokens=4096 withholds
        the watermark even though embedding_version and content_hash both match."""
        exchange_data = [{"index": 0, "content_hash": "hash0", "cap_tokens": None}]
        existing_chunks = {0: {"content_hash": "hash0", "embedding_version": EMBEDDING_VERSION, "cap_tokens": 4096}}

        result = _should_stamp_watermark(exchange_data, embedded_indices=set(), existing_chunks=existing_chunks)

        assert result is False

    def test_existing_chunk_null_cap_tokens_does_not_withhold(self):
        """NULL cap_tokens on an existing chunk is full-quality and does not
        raise TypeError."""
        exchange_data = [{"index": 0, "content_hash": "hash0", "cap_tokens": None}]
        existing_chunks = {0: {"content_hash": "hash0", "embedding_version": EMBEDDING_VERSION, "cap_tokens": None}}

        result = _should_stamp_watermark(exchange_data, embedded_indices=set(), existing_chunks=existing_chunks)

        assert result is True

    def test_freshly_embedded_draft_quality_withholds_watermark(self):
        """A freshly-embedded chunk (in embedded_indices) with per-exchange
        cap_tokens=4096 (< MODEL_TOKEN_LIMIT) still withholds the watermark — the
        `idx in embedded_indices` shortcut must not bypass this check."""
        exchange_data = [{"index": 0, "content_hash": "hash0", "cap_tokens": 4096}]

        result = _should_stamp_watermark(exchange_data, embedded_indices={0}, existing_chunks={})

        assert result is False

    def test_freshly_embedded_full_quality_stamps_watermark(self):
        """Freshly-embedded chunks all with cap_tokens=NULL (untruncated)
        still stamp the watermark."""
        exchange_data = [{"index": 0, "content_hash": "hash0", "cap_tokens": None}]

        result = _should_stamp_watermark(exchange_data, embedded_indices={0}, existing_chunks={})

        assert result is True
