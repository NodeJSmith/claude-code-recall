"""Tests for ccrecall.db_vec.branch_embedding_coverage's three-state return.

Vec-free (branch_embedding_coverage reads only branches/chunks, per its
docstring), so these use the plain `memory_db` fixture rather than
`make_vec_conn`/`VEC_SKIP`.
"""

from ccrecall.db import CONTENT_ERROR_VERSION
from ccrecall.db_vec import branch_embedding_coverage, find_stamped_branches_missing_chunks
from ccrecall.embeddings import EMBEDDING_MODEL, EMBEDDING_VERSION, MODEL_TOKEN_LIMIT


def _seed_branch(
    conn, i: int, *, embedding_version: int | None = None, embedding_model: str | None = None, is_active: int = 1
) -> int:
    """Insert an active, CHUNK_EMBEDDABLE branch (has a message). Returns branch_id."""
    conn.execute("INSERT INTO sessions (uuid) VALUES (?)", (f"sess-{i}",))
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO branches (session_id, leaf_uuid, is_active, embedding_version, embedding_model) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            session_id,
            f"leaf-{i}",
            is_active,
            embedding_version if embedding_version is not None else 0,
            embedding_model,
        ),
    )
    branch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, 'user', 'hi', '2024-01-01T00:00:00Z')",
        (session_id, f"m-{i}"),
    )
    msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (branch_id, msg_id))
    conn.commit()
    return branch_id


def _add_chunk(conn, branch_id: int, *, cap_tokens: int | None) -> None:
    conn.execute(
        "INSERT INTO chunks (branch_id, exchange_index, content_hash, cap_tokens) VALUES (?, 0, 'hash', ?)",
        (branch_id, cap_tokens),
    )
    conn.commit()


class TestBranchEmbeddingCoverageThreeState:
    def test_full_quality_branch_counts_as_embedded_full(self, memory_db):
        """Current watermark → embedded_full=1, embedded_draft=0."""
        _seed_branch(memory_db, 1, embedding_version=EMBEDDING_VERSION, embedding_model=EMBEDDING_MODEL)
        assert branch_embedding_coverage(memory_db) == (1, 0, 1)

    def test_stale_watermark_with_draft_chunk_counts_as_embedded_draft(self, memory_db):
        """Stale watermark + a chunk capped below MODEL_TOKEN_LIMIT → embedded_draft=1."""
        branch_id = _seed_branch(memory_db, 1)
        _add_chunk(memory_db, branch_id, cap_tokens=4096)
        assert branch_embedding_coverage(memory_db) == (0, 1, 1)

    def test_stale_watermark_no_chunk_counts_as_not_embedded(self, memory_db):
        """Stale watermark, no chunks at all → neither full nor draft."""
        _seed_branch(memory_db, 1)
        assert branch_embedding_coverage(memory_db) == (0, 0, 1)

    def test_stale_watermark_full_quality_chunk_not_counted_as_draft(self, memory_db):
        """Stale watermark with only an untruncated (NULL cap_tokens) chunk isn't
        draft-quality — e.g. a mid-backfill crash before the watermark stamp."""
        branch_id = _seed_branch(memory_db, 1)
        _add_chunk(memory_db, branch_id, cap_tokens=None)
        assert branch_embedding_coverage(memory_db) == (0, 0, 1)

    def test_draft_threshold_uses_model_token_limit(self, memory_db):
        """cap_tokens exactly at MODEL_TOKEN_LIMIT is NOT draft-quality (strict <)."""
        branch_id = _seed_branch(memory_db, 1)
        _add_chunk(memory_db, branch_id, cap_tokens=MODEL_TOKEN_LIMIT)
        assert branch_embedding_coverage(memory_db) == (0, 0, 1)

    def test_mixed_branches(self, memory_db):
        """One full, one draft, one unembedded → (1, 1, 3)."""
        full = _seed_branch(memory_db, 1, embedding_version=EMBEDDING_VERSION, embedding_model=EMBEDDING_MODEL)
        _add_chunk(memory_db, full, cap_tokens=None)
        draft_branch = _seed_branch(memory_db, 2)
        _add_chunk(memory_db, draft_branch, cap_tokens=2048)
        _seed_branch(memory_db, 3)

        assert branch_embedding_coverage(memory_db) == (1, 1, 3)

    def test_inactive_branch_excluded_from_total(self, memory_db):
        """is_active=0 branches don't count toward any of the three states."""
        _seed_branch(memory_db, 1, is_active=0)
        assert branch_embedding_coverage(memory_db) == (0, 0, 0)

    def test_zero_branches_returns_all_zero(self, memory_db):
        assert branch_embedding_coverage(memory_db) == (0, 0, 0)

    def test_pre_v8_schema_without_cap_tokens(self, memory_db):
        """On a pre-v8 DB (no cap_tokens column), embedded_draft is 0."""
        memory_db.execute("ALTER TABLE chunks DROP COLUMN cap_tokens")
        memory_db.commit()
        _seed_branch(memory_db, 1, embedding_version=EMBEDDING_VERSION, embedding_model=EMBEDDING_MODEL)
        assert branch_embedding_coverage(memory_db) == (1, 0, 1)

    def test_pre_v8_schema_stale_watermark(self, memory_db):
        """On a pre-v8 DB, stale watermark with no cap_tokens → (0, 0, 1)."""
        memory_db.execute("ALTER TABLE chunks DROP COLUMN cap_tokens")
        memory_db.commit()
        _seed_branch(memory_db, 1)
        assert branch_embedding_coverage(memory_db) == (0, 0, 1)


class TestFindStampedBranchesMissingChunks:
    def _stamped(self, conn, i: int = 1) -> int:
        return _seed_branch(conn, i, embedding_version=EMBEDDING_VERSION, embedding_model=EMBEDDING_MODEL)

    def _add_user_message(self, conn, branch_id: int) -> None:
        session_id = conn.execute("SELECT session_id FROM branches WHERE id = ?", (branch_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO messages (session_id, uuid, role, content, timestamp) "
            "VALUES (?, 'newer', 'user', 'again', '2024-01-02T00:00:00Z')",
            (session_id,),
        )
        msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (branch_id, msg_id))
        conn.commit()

    def test_stamped_branch_with_no_chunks_is_reported(self, memory_db):
        branch_id = self._stamped(memory_db)
        assert find_stamped_branches_missing_chunks(memory_db.cursor()) == {branch_id: 1}

    def test_stamped_branch_missing_newest_exchange_is_reported(self, memory_db):
        branch_id = self._stamped(memory_db)
        _add_chunk(memory_db, branch_id, cap_tokens=None)
        self._add_user_message(memory_db, branch_id)
        assert find_stamped_branches_missing_chunks(memory_db.cursor()) == {branch_id: 1}

    def test_stamped_branch_with_every_chunk_is_not_reported(self, memory_db):
        branch_id = self._stamped(memory_db)
        _add_chunk(memory_db, branch_id, cap_tokens=None)
        assert find_stamped_branches_missing_chunks(memory_db.cursor()) == {}

    def test_branch_with_no_exchanges_is_not_reported(self, memory_db):
        self._stamped(memory_db)
        memory_db.execute("UPDATE messages SET role = 'assistant'")
        memory_db.commit()
        assert find_stamped_branches_missing_chunks(memory_db.cursor()) == {}

    def test_notification_turns_are_not_expected_exchanges(self, memory_db):
        self._stamped(memory_db)
        memory_db.execute("UPDATE messages SET is_notification = 1")
        memory_db.commit()
        assert find_stamped_branches_missing_chunks(memory_db.cursor()) == {}

    def test_branches_without_a_current_stamp_are_not_reported(self, memory_db):
        _seed_branch(memory_db, 1)
        _seed_branch(memory_db, 2, embedding_version=CONTENT_ERROR_VERSION, embedding_model=EMBEDDING_MODEL)
        _seed_branch(memory_db, 3, embedding_version=EMBEDDING_VERSION, embedding_model="other-model")
        _seed_branch(memory_db, 4, embedding_version=EMBEDDING_VERSION, embedding_model=EMBEDDING_MODEL, is_active=0)
        assert find_stamped_branches_missing_chunks(memory_db.cursor()) == {}

    def test_writes_nothing(self, memory_db):
        self._stamped(memory_db)
        before = memory_db.total_changes
        find_stamped_branches_missing_chunks(memory_db.cursor())
        assert memory_db.total_changes == before
