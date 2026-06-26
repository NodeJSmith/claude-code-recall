"""Tests for search_conversations.py and recent_chats.py — search and retrieval."""

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import sqlite_vec
from conftest import make_vec_conn

from ccrecall.db import (
    upsert_chunk_vec,
    vec_available,
    write_chunk_embedding,
)
from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.formatting import apply_scores, format_snippet_json, format_snippet_markdown
from ccrecall.recent_chats import get_recent_sessions
from ccrecall.schema import SCHEMA, SCHEMA_CORE, detect_fts_support
from ccrecall.search_conversations import (
    _dedup_by_session,
    _get_vec_chunk_ids,
    _hydrate_cards,
    print_status,
    run,
    run_messages,
    search_messages,
    search_sessions,
)


@pytest.fixture
def search_db():
    """In-memory DB with schema, seeded with searchable sessions."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.commit()

    cursor = conn.cursor()

    # Create two projects
    cursor.execute(
        "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
        ("/home/user/alpha", "-home-user-alpha", "alpha"),
    )
    alpha_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
        ("/home/user/beta", "-home-user-beta", "beta"),
    )
    beta_id = cursor.lastrowid

    # Session 1 in alpha: talks about "pytest fixtures" (base repo)
    cursor.execute(
        "INSERT INTO sessions (uuid, project_id, cwd) VALUES (?, ?, ?)",
        ("sess-alpha-1", alpha_id, "/home/user/alpha"),
    )
    s1_id = cursor.lastrowid
    cursor.execute(
        """
        INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count, aggregated_content)
        VALUES (?, ?, 1, 2, ?)
    """,
        (
            s1_id,
            "leaf-a1",
            "How do pytest fixtures work? They provide reusable test setup.",
        ),
    )
    b1_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (s1_id, "m1", "user", "How do pytest fixtures work?", "2025-01-15T14:00:00Z"),
    )
    m1_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (
            s1_id,
            "m2",
            "assistant",
            "They provide reusable test setup.",
            "2025-01-15T14:01:00Z",
        ),
    )
    m2_id = cursor.lastrowid
    cursor.execute("INSERT INTO branch_messages VALUES (?, ?)", (b1_id, m1_id))
    cursor.execute("INSERT INTO branch_messages VALUES (?, ?)", (b1_id, m2_id))

    # Session 2 in alpha: talks about "database migration" (worktree)
    cursor.execute(
        "INSERT INTO sessions (uuid, project_id, cwd) VALUES (?, ?, ?)",
        ("sess-alpha-2", alpha_id, "/home/user/alpha/.claude/worktrees/ui-decomp"),
    )
    s2_id = cursor.lastrowid
    cursor.execute(
        """
        INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count, aggregated_content)
        VALUES (?, ?, 1, 3, ?)
    """,
        (
            s2_id,
            "leaf-a2",
            "How do I migrate the database? Use alembic for schema migrations.",
        ),
    )
    b2_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (s2_id, "m3", "user", "How do I migrate the database?", "2025-01-15T15:00:00Z"),
    )
    m3_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (
            s2_id,
            "m4",
            "assistant",
            "Use alembic for schema migrations.",
            "2025-01-15T15:01:00Z",
        ),
    )
    m4_id = cursor.lastrowid
    cursor.execute("INSERT INTO branch_messages VALUES (?, ?)", (b2_id, m3_id))
    cursor.execute("INSERT INTO branch_messages VALUES (?, ?)", (b2_id, m4_id))

    # Session 3 in beta: talks about "pytest mocking" (base repo)
    cursor.execute(
        "INSERT INTO sessions (uuid, project_id, cwd) VALUES (?, ?, ?)",
        ("sess-beta-1", beta_id, "/home/user/beta"),
    )
    s3_id = cursor.lastrowid
    cursor.execute(
        """
        INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count, aggregated_content)
        VALUES (?, ?, 1, 2, ?)
    """,
        (
            s3_id,
            "leaf-b1",
            "How do I mock in pytest? Use unittest.mock or pytest-mock.",
        ),
    )
    b3_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (s3_id, "m5", "user", "How do I mock in pytest?", "2025-01-15T16:00:00Z"),
    )
    m5_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (
            s3_id,
            "m6",
            "assistant",
            "Use unittest.mock or pytest-mock.",
            "2025-01-15T16:01:00Z",
        ),
    )
    m6_id = cursor.lastrowid
    cursor.execute("INSERT INTO branch_messages VALUES (?, ?)", (b3_id, m5_id))
    cursor.execute("INSERT INTO branch_messages VALUES (?, ?)", (b3_id, m6_id))

    conn.commit()
    yield conn
    conn.close()


class TestSearchSessionsFTS:
    """Test search with FTS5 (default on most SQLite builds)."""

    def test_search_returns_matching_sessions(self, search_db):
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        results, _ranked = search_sessions(search_db, "pytest", fts_level, max_results=10)
        assert len(results) >= 2, "Should match sessions mentioning 'pytest'"
        uuids = {r["session_uuid"] for r in results}
        assert "sess-alpha-1" in uuids
        assert "sess-beta-1" in uuids

    def test_search_database_specific(self, search_db):
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        results, _ranked = search_sessions(search_db, "database migration", fts_level, max_results=10)
        assert len(results) >= 1
        assert any(r["session_uuid"] == "sess-alpha-2" for r in results)

    def test_empty_query_returns_empty(self, search_db):
        fts_level = detect_fts_support(search_db)
        results, _ranked = search_sessions(search_db, "", fts_level)
        assert results == []

    def test_max_results_respected(self, search_db):
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        results, _ranked = search_sessions(search_db, "pytest", fts_level, max_results=1)
        assert len(results) <= 1

    def test_project_filter(self, search_db):
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        results, _ranked = search_sessions(search_db, "pytest", fts_level, max_results=10, projects=["alpha"])
        assert all(r["project"] == "alpha" for r in results), "Should only return alpha project"
        assert len(results) >= 1

    def test_cards_have_no_full_transcript(self, search_db):
        """FR#12: Track A cards must not include full message lists — summary data only."""
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        results, _ranked = search_sessions(search_db, "pytest fixtures", fts_level, max_results=5)
        matching = [r for r in results if r["session_uuid"] == "sess-alpha-1"]
        assert len(matching) == 1
        card = matching[0]
        assert "messages" not in card, "A-path cards must not include a full message list (FR#12)"
        assert "session_uuid" in card
        assert "handle" in card
        assert "exchange_count" in card

    def test_session_filter(self, search_db):
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        results, _ranked = search_sessions(search_db, "pytest", fts_level, max_results=10, session_id="sess-alpha-1")
        assert len(results) == 1
        assert results[0]["session_uuid"] == "sess-alpha-1"

    def test_session_filter_prefix_match(self, search_db):
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        results, _ranked = search_sessions(search_db, "pytest", fts_level, max_results=10, session_id="sess")
        uuids = {r["session_uuid"] for r in results}
        assert len(results) == 2
        assert "sess-alpha-1" in uuids
        assert "sess-beta-1" in uuids

    def test_session_filter_no_match(self, search_db):
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        results, _ranked = search_sessions(search_db, "pytest", fts_level, max_results=10, session_id="nonexistent")
        assert len(results) == 0


class TestSearchSessionsLIKE:
    """Test LIKE fallback when FTS is not available."""

    def test_like_search_returns_results(self, search_db):
        results, _ranked = search_sessions(search_db, "pytest", fts_level=None, max_results=10)
        assert len(results) >= 2
        uuids = {r["session_uuid"] for r in results}
        assert "sess-alpha-1" in uuids
        assert "sess-beta-1" in uuids

    def test_like_multiple_terms_and_logic(self, search_db):
        # LIKE fallback uses AND between terms — only sess-alpha-1 contains both
        results, _ranked = search_sessions(search_db, "pytest fixtures", fts_level=None, max_results=10)
        uuids = {r["session_uuid"] for r in results}
        assert "sess-alpha-1" in uuids  # has both "pytest" and "fixtures"
        assert "sess-alpha-2" not in uuids  # has neither
        assert "sess-beta-1" not in uuids  # has "pytest" but not "fixtures"

    def test_like_project_filter(self, search_db):
        results, _ranked = search_sessions(search_db, "pytest", fts_level=None, max_results=10, projects=["beta"])
        assert all(r["project"] == "beta" for r in results)

    def test_like_empty_query(self, search_db):
        results, _ranked = search_sessions(search_db, "", fts_level=None)
        assert results == []

    def test_like_max_results(self, search_db):
        results, _ranked = search_sessions(search_db, "pytest", fts_level=None, max_results=1)
        assert len(results) <= 1

    def test_like_session_filter(self, search_db):
        results, _ranked = search_sessions(search_db, "pytest", fts_level=None, max_results=10, session_id="sess-beta")
        assert len(results) == 1
        assert results[0]["session_uuid"] == "sess-beta-1"


class TestFtsSearchFindsFilePath:
    """Test that FTS search finds sessions by file path in aggregated_content."""

    @pytest.fixture
    def file_path_db(self):
        """DB with a branch whose aggregated_content includes file paths via __files__ marker."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()

        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/home/user/myproject", "-home-user-myproject", "myproject"),
        )
        proj_id = cursor.lastrowid

        # Session where we edited summarizer.py
        cursor.execute(
            "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
            ("sess-file-1", proj_id),
        )
        s1_id = cursor.lastrowid

        # aggregated_content includes the __files__ marker with full paths
        agg_content = (
            "How do I fix the summarizer? Let me look at it.\n"
            "__files__\n"
            "/home/user/myproject/src/ccrecall/summarizer.py\n"
            "/home/user/myproject/src/ccrecall/parsing.py"
        )
        cursor.execute(
            """
            INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count,
                                  aggregated_content, files_modified)
            VALUES (?, ?, 1, 2, ?, ?)
            """,
            (
                s1_id,
                "leaf-file-1",
                agg_content,
                '["/home/user/myproject/src/ccrecall/summarizer.py", "/home/user/myproject/src/ccrecall/parsing.py"]',
            ),
        )
        b1_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (
                s1_id,
                "msg-f1",
                "user",
                "How do I fix the summarizer?",
                "2025-01-15T14:00:00Z",
            ),
        )
        m1_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (
                s1_id,
                "msg-f2",
                "assistant",
                "Let me look at it.",
                "2025-01-15T14:01:00Z",
            ),
        )
        m2_id = cursor.lastrowid
        cursor.execute("INSERT INTO branch_messages VALUES (?, ?)", (b1_id, m1_id))
        cursor.execute("INSERT INTO branch_messages VALUES (?, ?)", (b1_id, m2_id))

        conn.commit()
        yield conn
        conn.close()

    def test_fts_search_finds_file_path(self, file_path_db):
        """Insert a branch with files_modified containing a path; search for filename finds it."""
        fts_level = detect_fts_support(file_path_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        # Search for the filename — should match via aggregated_content's __files__ section
        results, _ranked = search_sessions(file_path_db, "summarizer", fts_level, max_results=10)
        uuids = {r["session_uuid"] for r in results}
        assert "sess-file-1" in uuids, "FTS search for 'summarizer' should find the session that edited summarizer.py"

    def test_like_search_finds_file_path(self, file_path_db):
        """LIKE fallback also finds sessions by filename in aggregated_content."""
        results, _ranked = search_sessions(file_path_db, "summarizer", fts_level=None, max_results=10)
        uuids = {r["session_uuid"] for r in results}
        assert "sess-file-1" in uuids, "LIKE search for 'summarizer' should find the session that edited summarizer.py"


class TestRecentChatsSessionFilter:
    """Test --session filter on get_recent_sessions."""

    def test_session_filter_exact(self, search_db):
        results = get_recent_sessions(search_db, n=10, session_id="sess-alpha-1")
        assert len(results) == 1
        assert results[0]["uuid"] == "sess-alpha-1"

    def test_session_filter_prefix(self, search_db):
        results = get_recent_sessions(search_db, n=10, session_id="sess-alpha")
        assert len(results) == 2
        uuids = {r["uuid"] for r in results}
        assert uuids == {"sess-alpha-1", "sess-alpha-2"}

    def test_session_filter_no_match(self, search_db):
        results = get_recent_sessions(search_db, n=10, session_id="nonexistent")
        assert len(results) == 0

    def test_session_filter_short_prefix(self, search_db):
        results = get_recent_sessions(search_db, n=10, session_id="sess")
        assert len(results) == 3


class TestPathFilter:
    """Test --path filter on both get_recent_sessions and search_sessions."""

    def test_recent_path_worktree_name(self, search_db):
        results = get_recent_sessions(search_db, n=10, path="ui-decomp")
        assert len(results) == 1
        assert results[0]["uuid"] == "sess-alpha-2"

    def test_recent_path_no_match(self, search_db):
        results = get_recent_sessions(search_db, n=10, path="nonexistent-worktree")
        assert len(results) == 0

    def test_recent_path_substring_matches_base_and_worktrees(self, search_db):
        """Substring match on a repo path includes worktrees under it."""
        results = get_recent_sessions(search_db, n=10, path="/home/user/alpha")
        assert len(results) == 2
        uuids = {r["uuid"] for r in results}
        assert uuids == {"sess-alpha-1", "sess-alpha-2"}

    def test_recent_path_combined_with_project(self, search_db):
        results = get_recent_sessions(search_db, n=10, projects=["alpha"], path="ui-decomp")
        assert len(results) == 1
        assert results[0]["uuid"] == "sess-alpha-2"

    def test_recent_path_project_mismatch(self, search_db):
        results = get_recent_sessions(search_db, n=10, projects=["beta"], path="ui-decomp")
        assert len(results) == 0

    def test_search_path_fts(self, search_db):
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        results, _ranked = search_sessions(search_db, "database", fts_level, max_results=10, path="ui-decomp")
        assert len(results) == 1
        assert results[0]["session_uuid"] == "sess-alpha-2"

    def test_search_path_like_fallback(self, search_db):
        results, _ranked = search_sessions(search_db, "database", fts_level=None, max_results=10, path="ui-decomp")
        assert len(results) == 1
        assert results[0]["session_uuid"] == "sess-alpha-2"

    def test_search_path_narrows_results(self, search_db):
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        all_pytest, _r1 = search_sessions(search_db, "pytest", fts_level, max_results=10)
        with_path, _r2 = search_sessions(search_db, "pytest", fts_level, max_results=10, path="/home/user/beta")
        assert len(with_path) < len(all_pytest)
        assert all(r["session_uuid"] == "sess-beta-1" for r in with_path)


# Helpers shared by new vec/fusion tests


def _seed_branch(conn: sqlite3.Connection, uuid: str, content: str, summary: str) -> tuple[int, int]:
    """Seed one project/session/branch; returns (session_id, branch_id)."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO projects (path, key, name) VALUES (?, ?, ?)",
        ("/home/user/proj", "-home-user-proj", "proj"),
    )
    cursor.execute("SELECT id FROM projects WHERE key = ?", ("-home-user-proj",))
    proj_id = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO sessions (uuid, project_id, cwd) VALUES (?, ?, ?)",
        (uuid, proj_id, "/home/user/proj"),
    )
    sess_id = cursor.lastrowid
    cursor.execute(
        """
        INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count,
                               aggregated_content, context_summary,
                               embedding_version, embedding_model)
        VALUES (?, ?, 1, 1, ?, ?, ?, ?)
        """,
        (sess_id, f"leaf-{uuid}", content, summary, EMBEDDING_VERSION, EMBEDDING_MODEL),
    )
    branch_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (sess_id, f"m-{uuid}", "user", content, "2025-01-01T00:00:00Z"),
    )
    msg_id = cursor.lastrowid
    cursor.execute("INSERT INTO branch_messages VALUES (?, ?)", (branch_id, msg_id))
    conn.commit()
    return sess_id, branch_id


# degrade to keyword when model/extension unavailable


class TestDegradation:
    """search_sessions must not raise and must return FTS results when vec/model unavailable."""

    def test_no_model_returns_fts_results(self, search_db):
        """model_available() == False → keyword path, no raise."""
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        with patch("ccrecall.search_conversations.model_available", return_value=False):
            results, ranked = search_sessions(search_db, "pytest", fts_level, max_results=10)
        assert len(results) >= 2
        uuids = {r["session_uuid"] for r in results}
        assert "sess-alpha-1" in uuids
        assert "sess-beta-1" in uuids
        assert ranked is False  # keyword path → not ranked

    def test_attribute_error_on_extension_does_not_raise(self, search_db):
        """AttributeError from extension load → keyword path, no raise."""
        fts_level = detect_fts_support(search_db)

        def _raise(*_a, **_kw):
            raise AttributeError("no load_extension")

        with (
            patch("ccrecall.search_conversations.model_available", return_value=True),
            patch("ccrecall.search_conversations.embed_text", side_effect=_raise),
        ):
            results, ranked = search_sessions(search_db, "pytest", fts_level, max_results=10)

        # Should return results via keyword fallback, not raise
        assert isinstance(results, list)
        assert ranked is False

    def test_missing_model_path_does_not_raise(self, search_db):
        """model_available() is False (model unavailable) → keyword path."""
        fts_level = detect_fts_support(search_db)

        with patch("ccrecall.search_conversations.model_available", return_value=False):
            results, _ranked = search_sessions(search_db, "database", fts_level, max_results=10)

        assert isinstance(results, list)
        # Should still return keyword results
        uuids = {r["session_uuid"] for r in results}
        assert "sess-alpha-2" in uuids

    def test_keyword_only_flag_skips_embed(self, search_db):
        """--keyword-only skips embed_text entirely."""
        fts_level = detect_fts_support(search_db)
        called = []

        def _should_not_be_called(text):
            called.append(text)
            return [0.0] * EMBEDDING_DIM

        with patch(
            "ccrecall.search_conversations.embed_text",
            side_effect=_should_not_be_called,
        ):
            results, _ranked = search_sessions(search_db, "pytest", fts_level, max_results=10, keyword_only=True)

        assert called == [], "embed_text must not be called with keyword_only=True"
        assert len(results) >= 2

    def test_vec_table_missing_falls_back(self, search_db):
        """model_available=True but chunk_vec absent → chunk_vec_queryable returns False → keyword results."""
        fts_level = detect_fts_support(search_db)
        if fts_level not in ("fts5", "fts4"):
            pytest.skip("FTS not available")

        # search_db has no chunk_vec table (no extension loaded).
        # search_sessions probes chunk_vec_queryable before embedding — returns False
        # — and falls back to the keyword path without calling embed_text.
        with patch("ccrecall.search_conversations.model_available", return_value=True):
            results, ranked = search_sessions(search_db, "database", fts_level, max_results=10)

        assert isinstance(results, list)
        assert len(results) >= 1
        uuids = {r["session_uuid"] for r in results}
        assert "sess-alpha-2" in uuids
        assert ranked is False


# stale-version chunk rows excluded from vector candidates (AC#8)


def _seed_branch_with_chunk(
    conn: sqlite3.Connection,
    uuid: str,
    content: str,
    summary: str,
    *,
    embed_vec: list[float] | None = None,
    chunk_embedding_version: int = EMBEDDING_VERSION,
    chunk_embedding_model: str = EMBEDDING_MODEL,
) -> tuple[int, int, int]:
    """Seed project/session/branch/messages + a chunk row; returns (sess_id, branch_id, chunk_id).

    Inserts the chunk into chunk_vec only if embed_vec is provided.
    Chunk embedding_version / embedding_model are set per the parameters so
    stale vs current chunks can be distinguished.
    """
    sess_id, branch_id = _seed_branch(conn, uuid, content, summary)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO chunks (branch_id, exchange_index, content_hash,
                            user_text, embedding_version, embedding_model)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (branch_id, 0, f"hash-{uuid}", content, chunk_embedding_version, chunk_embedding_model),
    )
    chunk_id = cursor.lastrowid
    if embed_vec is not None:
        cursor.execute(
            "INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, sqlite_vec.serialize_float32(embed_vec)),
        )
    conn.commit()
    return sess_id, branch_id, chunk_id


class TestStaleVersionExclusion:
    """Chunks with old embedding_version must not appear via the chunk-KNN path (AC#8)."""

    @pytest.fixture
    def vec_conn(self):
        conn = make_vec_conn()
        yield conn
        conn.close()

    @pytest.fixture
    def stale_db(self, vec_conn):
        """DB with one current-version chunk and one stale-version chunk, both in chunk_vec."""
        fake_vec = [0.1] * EMBEDDING_DIM
        # Current-version chunk
        _sess_curr, current_branch_id, _current_chunk_id = _seed_branch_with_chunk(
            vec_conn,
            "sess-current",
            "current version text",
            "current summary",
            embed_vec=fake_vec,
            chunk_embedding_version=EMBEDDING_VERSION,
        )
        # Stale-version chunk: same vector but old embedding_version on the chunk row
        _sess_stale, stale_branch_id, _stale_chunk_id = _seed_branch_with_chunk(
            vec_conn,
            "sess-stale",
            "stale text",
            "stale summary",
            embed_vec=fake_vec,
            chunk_embedding_version=EMBEDDING_VERSION - 1,  # stale
        )
        return vec_conn, current_branch_id, stale_branch_id

    @pytest.mark.skipif(
        not vec_available(sqlite3.connect(":memory:")),
        reason="sqlite-vec not available",
    )
    def test_stale_chunk_excluded_from_vec_candidates(self, stale_db):
        """_get_vec_chunk_ids must not return branches whose chunks are at a stale version (AC#8)."""
        conn, current_id, stale_id = stale_db
        cursor = conn.cursor()
        fake_vec = [0.1] * EMBEDDING_DIM
        results = _get_vec_chunk_ids(cursor, fake_vec, top_k=10)
        branch_ids = [r[0] for r in results]
        assert stale_id not in branch_ids, "Stale-version chunk must not appear in chunk-KNN candidates"
        assert current_id in branch_ids, "Current-version chunk must appear in chunk-KNN candidates"

    @pytest.mark.skipif(
        not vec_available(sqlite3.connect(":memory:")),
        reason="sqlite-vec not available",
    )
    def test_stale_branch_reachable_via_fts_not_vec(self, stale_db):
        """Integration: stale chunk session appears in FTS results but NOT in chunk-KNN candidates.

        The stale branch's aggregated_content contains "stale text", so a keyword
        search for "stale" must surface it via FTS. However, _get_vec_chunk_ids
        must exclude it because its chunk.embedding_version != EMBEDDING_VERSION.
        """
        conn, _current_id, stale_id = stale_db
        fts_level = detect_fts_support(conn)

        # Confirm chunk-KNN path excludes stale branch
        cursor = conn.cursor()
        fake_vec = [0.1] * EMBEDDING_DIM
        results = _get_vec_chunk_ids(cursor, fake_vec, top_k=10)
        branch_ids = [r[0] for r in results]
        assert stale_id not in branch_ids, "Stale-version chunk must not appear in chunk-KNN candidates"

        # Confirm keyword path (FTS or LIKE) does surface the stale session
        with patch("ccrecall.search_conversations.model_available", return_value=False):
            results_kw, _ranked = search_sessions(conn, "stale", fts_level, max_results=10, keyword_only=True)
        uuids = {r["session_uuid"] for r in results_kw}
        assert "sess-stale" in uuids, "Stale session must still be reachable via keyword/FTS search"


# session dedup: two branches of one session → one result


class TestSessionDedup:
    """Two branches of the same session in the fused top-K yield exactly one result."""

    def test_dedup_by_session_unit(self):
        """Unit test _dedup_by_session: keeps first (highest-ranked) branch per session."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()

        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/p", "-p", "p"),
        )
        proj_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
            ("sess-dup", proj_id),
        )
        sess_id = cursor.lastrowid

        # Two branches in the same session
        cursor.execute(
            "INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, ?, 1)",
            (sess_id, "leaf-A"),
        )
        b1 = cursor.lastrowid
        cursor.execute(
            "INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, ?, 1)",
            (sess_id, "leaf-B"),
        )
        b2 = cursor.lastrowid
        conn.commit()

        # b1 ranked first — dedup should keep b1 and drop b2
        result = _dedup_by_session(cursor, [b1, b2])
        assert result == [b1], "Should keep only the first branch of the session"

    def test_dedup_by_session_different_sessions(self):
        """Branches from different sessions are both kept."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()

        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/p", "-p", "p"),
        )
        proj_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
            ("sess-X", proj_id),
        )
        sx = cursor.lastrowid
        cursor.execute(
            "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
            ("sess-Y", proj_id),
        )
        sy = cursor.lastrowid

        cursor.execute(
            "INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, ?, 1)",
            (sx, "leaf-X"),
        )
        bx = cursor.lastrowid
        cursor.execute(
            "INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, ?, 1)",
            (sy, "leaf-Y"),
        )
        by = cursor.lastrowid
        conn.commit()

        result = _dedup_by_session(cursor, [bx, by])
        assert result == [bx, by], "Different sessions should both be kept"

    @pytest.mark.skipif(
        not vec_available(sqlite3.connect(":memory:")),
        reason="sqlite-vec not available",
    )
    def test_duplicate_session_via_search(self):
        """Two branches of one session ranked by chunk-KNN fusion → exactly one card returned."""
        conn = make_vec_conn()
        fts_level = detect_fts_support(conn)

        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/home/user/proj", "-home-user-proj", "proj"),
        )
        proj_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO sessions (uuid, project_id, cwd) VALUES (?, ?, ?)",
            ("sess-multi-branch", proj_id, "/home/user/proj"),
        )
        sess_id = cursor.lastrowid

        # Branch A and Branch B both belong to the same session
        for leaf, content in [
            ("leaf-A", "python async await coroutine"),
            ("leaf-B", "python async await event loop"),
        ]:
            cursor.execute(
                """
                INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count,
                                       aggregated_content, context_summary,
                                       embedding_version, embedding_model)
                VALUES (?, ?, 1, 1, ?, ?, ?, ?)
                """,
                (
                    sess_id,
                    leaf,
                    content,
                    f"summary {leaf}",
                    EMBEDDING_VERSION,
                    EMBEDDING_MODEL,
                ),
            )
            bid = cursor.lastrowid
            cursor.execute(
                "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                (sess_id, f"m-{leaf}", "user", content, "2025-01-01T00:00:00Z"),
            )
            mid = cursor.lastrowid
            cursor.execute("INSERT INTO branch_messages VALUES (?, ?)", (bid, mid))

            # Seed chunk_vec for each branch
            cursor.execute(
                """
                INSERT INTO chunks (branch_id, exchange_index, content_hash,
                                    user_text, embedding_version, embedding_model)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (bid, 0, f"hash-{leaf}", content, EMBEDDING_VERSION, EMBEDDING_MODEL),
            )
            chunk_id = cursor.lastrowid
            upsert_chunk_vec(cursor, chunk_id, [0.5] * EMBEDDING_DIM)

        conn.commit()

        with (
            patch("ccrecall.search_conversations.model_available", return_value=True),
            patch(
                "ccrecall.search_conversations.embed_text",
                return_value=[0.5] * EMBEDDING_DIM,
            ),
        ):
            results, _ranked = search_sessions(conn, "async coroutine", fts_level, max_results=10)

        session_uuids = [r["session_uuid"] for r in results]
        assert session_uuids.count("sess-multi-branch") == 1, (
            "Two branches of the same session must yield exactly one card"
        )
        conn.close()


# --status flag


class TestStatusFlag:
    """--status prints diagnostic info and exits 0 without requiring --query."""

    def test_status_exits_zero(self, tmp_path, capsys):
        """--status exits 0 and outputs all diagnostic fields (vec, model, chunk coverage, branches)."""
        db_path = tmp_path / "test.db"
        # Create a minimal DB so status can read branch/chunk counts
        c = sqlite3.connect(str(db_path))
        c.executescript(SCHEMA_CORE)
        c.commit()
        c.close()

        settings = {"db_path": str(db_path)}

        with pytest.raises(SystemExit) as exc:
            print_status(settings)

        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "vec extension:" in captured.out
        assert "model:" in captured.out
        assert "chunk coverage:" in captured.out
        assert "embedded branches:" in captured.out

    def test_status_does_not_require_query(self, tmp_path):
        """--status works without --query."""
        db_path = tmp_path / "conv.db"
        c = sqlite3.connect(str(db_path))
        c.executescript(SCHEMA_CORE)
        c.commit()
        c.close()

        with pytest.raises(SystemExit) as exc:
            run(status=True, db=db_path)
        assert exc.value.code == 0

    def test_status_ignores_keyword_only(self, tmp_path):
        """--status combined with --keyword-only still exits 0."""
        db_path = tmp_path / "conv2.db"
        c = sqlite3.connect(str(db_path))
        c.executescript(SCHEMA_CORE)
        c.commit()
        c.close()

        with pytest.raises(SystemExit) as exc:
            run(status=True, keyword_only=True, db=db_path)
        assert exc.value.code == 0

    def test_run_errors_without_query_or_status(self, tmp_path):
        """run() errors when neither --query nor --status is provided."""
        db_path = tmp_path / "conv3.db"

        with pytest.raises(SystemExit) as exc:
            run(db=db_path)
        # run() exits 2 when neither --query nor --status is given
        assert exc.value.code != 0


class TestExceptionNarrowing:
    """The vec/FTS degradation paths catch DB errors only (issue #10): a query
    failure falls back gracefully, but a genuine bug propagates."""

    def test_get_vec_chunk_ids_returns_empty_on_db_error(self):
        """A DB error (chunk_vec table missing) degrades to an empty list, not a crash."""
        conn = sqlite3.connect(":memory:")  # no chunk_vec table → vec query raises sqlite3.Error
        try:
            result = _get_vec_chunk_ids(conn.cursor(), [0.1, 0.2, 0.3, 0.4], top_k=5)
            assert result == []
        finally:
            conn.close()

    def test_get_vec_chunk_ids_propagates_non_db_error(self):
        """A non-DB error (a real bug, e.g. AttributeError) propagates instead of being masked.

        The `except sqlite3.Error` guards only degrade DB-level failures. A non-DB
        exception raised by the KNN execute must surface. `serialize_float32` is a
        pure-Python pack that needs no loaded extension, so a MagicMock cursor whose
        execute raises AttributeError reaches and escapes the guard.
        """
        cursor = MagicMock()
        cursor.execute.side_effect = AttributeError("real bug")
        with pytest.raises(AttributeError):
            _get_vec_chunk_ids(cursor, [0.1] * EMBEDDING_DIM, top_k=5)

    def test_detect_fts_support_returns_none_on_db_error(self):
        """A DB error (querying a closed connection) degrades to None, not a crash."""
        conn = sqlite3.connect(":memory:")
        conn.close()  # subsequent execute raises sqlite3.ProgrammingError (a sqlite3.Error)
        assert detect_fts_support(conn) is None


# card field contract


class TestCardFields:
    """_hydrate_cards must produce the output contract fields (session_uuid, handle, topic, disposition)."""

    def _seed_with_summary(self, conn):
        """Seed a branch with context_summary_json carrying topic + disposition."""
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/home/user/cf", "-home-user-cf", "cf"),
        )
        proj_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO sessions (uuid, project_id, git_branch) VALUES (?, ?, ?)",
            ("cf-sess-uuid", proj_id, "main"),
        )
        sess_id = cursor.lastrowid
        summary_json = json.dumps(
            {
                "version": 1,
                "topic": "Debugging a pytest fixture",
                "disposition": "shipped",
                "first_exchanges": [],
                "last_exchanges": [],
                "metadata": {"exchange_count": 4, "files_modified": ["a.py"], "commits": ["abc123"], "tool_counts": {}},
            }
        )
        cursor.execute(
            """
            INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count,
                                   files_modified, commits, aggregated_content,
                                   context_summary_json, embedding_version, embedding_model)
            VALUES (?, ?, 1, 4, ?, ?, ?, ?, ?, ?)
            """,
            (
                sess_id,
                "cf-leaf",
                '["a.py"]',
                '["abc123"]',
                "Debugging a pytest fixture",
                summary_json,
                EMBEDDING_VERSION,
                EMBEDDING_MODEL,
            ),
        )
        branch_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (sess_id, "cf-msg", "user", "Debugging a pytest fixture", "2025-06-01T10:00:00Z"),
        )
        msg_id = cursor.lastrowid
        cursor.execute("INSERT INTO branch_messages VALUES (?, ?)", (branch_id, msg_id))
        conn.commit()
        return branch_id

    def test_card_fields_from_context_summary_json(self):
        """Cards include session_uuid, handle, topic, disposition from context_summary_json."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()
        branch_id = self._seed_with_summary(conn)

        cards = _hydrate_cards(conn.cursor(), [branch_id])
        assert len(cards) == 1
        card = cards[0]

        assert card["session_uuid"] == "cf-sess-uuid"
        assert card["handle"] == "cf-sess-"  # first 8 chars of "cf-sess-uuid"
        assert card["topic"] == "Debugging a pytest fixture"
        assert card["disposition"] == "shipped"
        assert card["exchange_count"] == 4
        assert card["git_branch"] == "main"
        assert card["project"] == "cf"
        assert "a.py" in card["files_modified"]
        assert "abc123" in card["commits"]
        conn.close()

    def test_card_handle_is_first_8_chars_of_uuid(self):
        """handle = session_uuid[:8]."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()
        branch_id = self._seed_with_summary(conn)

        cards = _hydrate_cards(conn.cursor(), [branch_id])
        uuid = cards[0]["session_uuid"]
        assert cards[0]["handle"] == uuid[:8]

    def test_card_graceful_degrade_no_summary_json(self):
        """When context_summary_json is absent, topic is derived from first user message (FR#11)."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()
        _sess_id, branch_id = _seed_branch(conn, "gr-sess", "What is the deadline?", "")

        cards = _hydrate_cards(conn.cursor(), [branch_id])
        assert len(cards) == 1
        card = cards[0]
        assert card["session_uuid"] == "gr-sess"
        # Graceful degrade: topic from first user message
        assert card["topic"] is not None
        assert "deadline" in card["topic"].lower()
        # No context_summary_json → disposition is None
        assert card["disposition"] is None
        conn.close()

    def test_keyword_search_returns_ranked_false(self, search_db):
        """Keyword-only path returns ranked=False (no relevance signal)."""
        fts_level = detect_fts_support(search_db)
        _results, ranked = search_sessions(search_db, "pytest", fts_level, max_results=10, keyword_only=True)
        assert ranked is False


# AC#1: chunk-KNN finds middle exchanges


class TestMidSessionRecall:
    """AC#1: In a session with many exchanges, a query matching exchange 5 finds the session via chunk-KNN."""

    @pytest.mark.skipif(
        not vec_available(sqlite3.connect(":memory:")),
        reason="sqlite-vec not available",
    )
    def test_mid_session_chunk_found_by_knn(self):
        """Chunk at exchange_index=4 (middle) is the KNN nearest-neighbor → branch appears in results.

        Uses orthogonal dummy vectors so the query vec is unambiguously closest to the
        middle chunk and far from all others. Verifies that _get_vec_chunk_ids returns
        the branch via its middle chunk (not only first/last).
        """
        conn = make_vec_conn()
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/home/user/mid", "-home-user-mid", "mid"),
        )
        proj_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
            ("sess-mid", proj_id),
        )
        sess_id = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count,
                                   aggregated_content, embedding_version, embedding_model)
            VALUES (?, ?, 1, 10, ?, ?, ?)
            """,
            (sess_id, "leaf-mid", "test mid session content", EMBEDDING_VERSION, EMBEDDING_MODEL),
        )
        branch_id = cursor.lastrowid

        # Insert 10 chunks with orthogonal dummy vectors. Only exchange_index=4 has the
        # query-matching vector (all 1s normalized); others have (1, 0, 0, ...) variants.
        query_vec = [1.0 / EMBEDDING_DIM**0.5] * EMBEDDING_DIM  # uniform unit vector
        for i in range(10):
            vec = [0.0] * EMBEDDING_DIM
            if i == 4:
                vec = query_vec  # middle chunk matches
            else:
                vec[i % EMBEDDING_DIM] = 1.0  # orthogonal to query for other chunks
            cursor.execute(
                """
                INSERT INTO chunks (branch_id, exchange_index, content_hash,
                                    user_text, embedding_version, embedding_model)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (branch_id, i, f"hash-{i}", f"exchange {i}", EMBEDDING_VERSION, EMBEDDING_MODEL),
            )
            chunk_id = cursor.lastrowid
            upsert_chunk_vec(cursor, chunk_id, vec)

        conn.commit()

        results = _get_vec_chunk_ids(cursor, query_vec, top_k=5)
        branch_ids = [r[0] for r in results]
        assert branch_id in branch_ids, (
            "AC#1: branch must appear in chunk-KNN results when the middle exchange matches the query"
        )

        # The matching chunk is exchange_index=4 — verify best-chunk rollup picked the right one
        winning_chunk_id = next(r[2] for r in results if r[0] == branch_id)
        ex_idx = cursor.execute("SELECT exchange_index FROM chunks WHERE id = ?", (winning_chunk_id,)).fetchone()[0]
        assert ex_idx == 4, f"Best chunk must be exchange_index=4 (got {ex_idx})"
        conn.close()


class TestCappedChunkRetrieval:
    """AC#9 (retrieval half): a chunk embedded from a head+tail-capped exchange is still
    retrievable via chunk-KNN for a query matching it — the end-to-end complement to T02's
    cap-produces-a-vector unit test."""

    @pytest.mark.skipif(
        not vec_available(sqlite3.connect(":memory:")),
        reason="sqlite-vec not available",
    )
    def test_capped_chunk_is_retrievable_by_knn(self):
        """A was_capped=1 chunk (head+tail display text) whose vector matches the query
        is returned by _get_vec_chunk_ids, alongside an orthogonal distractor that is not."""
        conn = make_vec_conn()
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/home/user/cap", "-home-user-cap", "cap"),
        )
        proj_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO sessions (uuid, project_id) VALUES (?, ?)",
            ("sess-cap", proj_id),
        )
        sess_id = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count,
                                   aggregated_content, embedding_version, embedding_model)
            VALUES (?, ?, 1, 1, ?, ?, ?)
            """,
            (sess_id, "leaf-cap", "huge pasted file exchange", EMBEDDING_VERSION, EMBEDDING_MODEL),
        )
        branch_id = cursor.lastrowid

        # The capped chunk: its embedded text was head+tail-capped (was_capped=1), so the
        # stored display text shows the head and tail only. Its vector is the query vector.
        query_vec = [1.0 / EMBEDDING_DIM**0.5] * EMBEDDING_DIM
        head_tail_text = "HEAD: opening of the pasted blob …(capped)… TAIL: closing of the pasted blob"
        cursor.execute(
            """
            INSERT INTO chunks (branch_id, exchange_index, content_hash, user_text,
                                was_capped, embedding_version, embedding_model)
            VALUES (?, 0, ?, ?, 1, ?, ?)
            """,
            (branch_id, "hash-capped", head_tail_text, EMBEDDING_VERSION, EMBEDDING_MODEL),
        )
        capped_chunk_id = cursor.lastrowid
        upsert_chunk_vec(cursor, capped_chunk_id, query_vec)

        # An orthogonal distractor chunk (different branch) that must NOT match the query.
        cursor.execute(
            """
            INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count,
                                   aggregated_content, embedding_version, embedding_model)
            VALUES (?, ?, 1, 1, ?, ?, ?)
            """,
            (sess_id, "leaf-other", "unrelated exchange", EMBEDDING_VERSION, EMBEDDING_MODEL),
        )
        other_branch_id = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO chunks (branch_id, exchange_index, content_hash, user_text,
                                embedding_version, embedding_model)
            VALUES (?, 0, ?, ?, ?, ?)
            """,
            (other_branch_id, "hash-other", "unrelated", EMBEDDING_VERSION, EMBEDDING_MODEL),
        )
        other_chunk_id = cursor.lastrowid
        orthogonal = [0.0] * EMBEDDING_DIM
        orthogonal[0] = 1.0
        upsert_chunk_vec(cursor, other_chunk_id, orthogonal)
        conn.commit()

        results = _get_vec_chunk_ids(cursor, query_vec, top_k=5)
        branch_ids = [r[0] for r in results]
        assert branch_id in branch_ids, (
            "AC#9: a head+tail-capped chunk must be retrievable via chunk-KNN for a matching query"
        )
        # The best chunk for that branch is the capped one we seeded.
        winning_chunk_id = next(r[2] for r in results if r[0] == branch_id)
        assert winning_chunk_id == capped_chunk_id
        was_capped = cursor.execute("SELECT was_capped FROM chunks WHERE id = ?", (winning_chunk_id,)).fetchone()[0]
        assert was_capped == 1, "retrieved chunk must be the head+tail-capped one"
        conn.close()


# post-teardown write-path regression


class TestPostTeardownWritePath:
    """After branch_vec teardown, chunk embedding writes must still work (regression guard)."""

    @pytest.mark.skipif(
        not vec_available(sqlite3.connect(":memory:")),
        reason="sqlite-vec not available",
    )
    def test_write_chunk_embedding_works_after_teardown(self):
        """write_chunk_embedding inserts into chunk_vec after branch_vec has been torn down.

        Verifies that the T06 teardown (which removes branch_vec and its infrastructure)
        does NOT break the chunk embedding write path used by session_ops.
        """
        conn = make_vec_conn()  # _ensure_vec_schema runs → branch_vec absent, chunk_vec present
        cursor = conn.cursor()

        # Confirm teardown state
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "branch_vec" not in tables, "Precondition: branch_vec must be absent"
        assert "chunk_vec" in tables, "Precondition: chunk_vec must exist"

        # Seed a branch and chunk
        cursor.execute("INSERT INTO projects (path, key, name) VALUES ('/p-wr', '-p-wr', 'p-wr')")
        proj_id = cursor.lastrowid
        cursor.execute("INSERT INTO sessions (uuid, project_id) VALUES ('sess-wr', ?)", (proj_id,))
        sess_id = cursor.lastrowid
        cursor.execute("INSERT INTO branches (session_id, leaf_uuid) VALUES (?, 'leaf-wr')", (sess_id,))
        branch_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO chunks (branch_id, exchange_index, content_hash) VALUES (?, 0, 'hash-wr')",
            (branch_id,),
        )
        chunk_id = cursor.lastrowid
        conn.commit()

        write_chunk_embedding(cursor, chunk_id, [0.7] * EMBEDDING_DIM, EMBEDDING_VERSION, EMBEDDING_MODEL)
        conn.commit()

        assert conn.execute("SELECT COUNT(*) FROM chunk_vec WHERE chunk_id = ?", (chunk_id,)).fetchone()[0] == 1, (
            "write_chunk_embedding must succeed after branch_vec teardown"
        )
        ver, model = conn.execute(
            "SELECT embedding_version, embedding_model FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        assert ver == EMBEDDING_VERSION
        assert model == EMBEDDING_MODEL
        conn.close()


# ---------------------------------------------------------------------------
# T07: Entrypoint B — search_messages / search-messages command
# ---------------------------------------------------------------------------


def _seed_two_chunks_same_branch(
    conn: sqlite3.Connection,
    query_vec: list[float],
) -> tuple[int, int, int, int]:
    """Seed a single branch with two chunks, both similar to query_vec.

    Returns (branch_id, chunk0_id, chunk1_id, session_db_id).
    user_text[0] = 'first exchange user turn'  (bounded, shorter than full)
    user_text[1] = 'second exchange user turn' (bounded, shorter than full)
    assistant_text[0] = 'first exchange assistant turn'
    assistant_text[1] = 'second exchange assistant turn'
    """
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO projects (path, key, name) VALUES (?, ?, ?)", ("/p/b2c", "-p-b2c", "b2c"))
    cursor.execute("SELECT id FROM projects WHERE key = ?", ("-p-b2c",))
    proj_id = cursor.fetchone()[0]
    cursor.execute("INSERT INTO sessions (uuid, project_id, cwd) VALUES (?, ?, ?)", ("sess-b2c", proj_id, "/p/b2c"))
    sess_id = cursor.lastrowid
    cursor.execute(
        """
        INSERT INTO branches (session_id, leaf_uuid, is_active, exchange_count,
                               aggregated_content, embedding_version, embedding_model)
        VALUES (?, ?, 1, 2, ?, ?, ?)
        """,
        (sess_id, "leaf-b2c", "two chunk content", EMBEDDING_VERSION, EMBEDDING_MODEL),
    )
    branch_id = cursor.lastrowid

    # Chunk 0 — exchange_index=0
    cursor.execute(
        """
        INSERT INTO chunks (branch_id, exchange_index, content_hash,
                            first_message_uuid, timestamp, user_text, assistant_text,
                            embedding_version, embedding_model)
        VALUES (?, 0, 'h0', 'uuid-m0', '2025-03-01T10:00:00Z',
                'first exchange user turn', 'first exchange assistant turn', ?, ?)
        """,
        (branch_id, EMBEDDING_VERSION, EMBEDDING_MODEL),
    )
    chunk0_id = cursor.lastrowid
    upsert_chunk_vec(cursor, chunk0_id, query_vec)

    # Chunk 1 — exchange_index=1
    cursor.execute(
        """
        INSERT INTO chunks (branch_id, exchange_index, content_hash,
                            first_message_uuid, timestamp, user_text, assistant_text,
                            embedding_version, embedding_model)
        VALUES (?, 1, 'h1', 'uuid-m1', '2025-03-01T10:05:00Z',
                'second exchange user turn', 'second exchange assistant turn', ?, ?)
        """,
        (branch_id, EMBEDDING_VERSION, EMBEDDING_MODEL),
    )
    chunk1_id = cursor.lastrowid
    upsert_chunk_vec(cursor, chunk1_id, query_vec)

    conn.commit()
    return branch_id, chunk0_id, chunk1_id, sess_id


class TestSearchMessages:
    """T07: Entrypoint B — chunk-KNN without rollup, snippet shape."""

    @pytest.mark.skipif(
        not vec_available(sqlite3.connect(":memory:")),
        reason="sqlite-vec not available",
    )
    def test_not_rolled_up_two_matches_same_session(self):
        """AC#3: two chunks in one session both appear (no rollup to session)."""
        conn = make_vec_conn()
        query_vec = [1.0 / EMBEDDING_DIM**0.5] * EMBEDDING_DIM

        _branch_id, _c0, _c1, _sess_id = _seed_two_chunks_same_branch(conn, query_vec)

        with (
            patch("ccrecall.search_conversations.model_available", return_value=True),
            patch("ccrecall.search_conversations.embed_text", return_value=query_vec),
        ):
            snippets, ranked = search_messages(conn, "test query", max_results=10)

        assert ranked is True
        # Both chunk hits for the same session must appear (B does NOT roll up to session)
        assert len(snippets) == 2, f"Expected 2 snippets (one per chunk), got {len(snippets)}"
        exchange_indices = {s["exchange_index"] for s in snippets}
        assert exchange_indices == {0, 1}, f"Expected exchange_indices 0 and 1, got {exchange_indices}"
        conn.close()

    @pytest.mark.skipif(
        not vec_available(sqlite3.connect(":memory:")),
        reason="sqlite-vec not available",
    )
    def test_locator_fields_present(self):
        """AC#3: each snippet carries (handle, exchange_index, timestamp) locator."""
        conn = make_vec_conn()
        query_vec = [1.0 / EMBEDDING_DIM**0.5] * EMBEDDING_DIM

        _branch_id, _c0, _c1, _sess_id = _seed_two_chunks_same_branch(conn, query_vec)

        with (
            patch("ccrecall.search_conversations.model_available", return_value=True),
            patch("ccrecall.search_conversations.embed_text", return_value=query_vec),
        ):
            snippets, _ranked = search_messages(conn, "test query", max_results=10)

        assert snippets, "Expected snippets to be non-empty"
        s = snippets[0]
        assert s.get("handle"), "Locator: handle must be present and non-empty"
        assert s.get("exchange_index") is not None, "Locator: exchange_index must be present"
        assert s.get("timestamp"), "Locator: timestamp must be present"
        assert s["handle"] == "sess-b2c"[:8], "handle must be first 8 chars of session_uuid"
        conn.close()

    @pytest.mark.skipif(
        not vec_available(sqlite3.connect(":memory:")),
        reason="sqlite-vec not available",
    )
    def test_bounded_excerpt_used(self):
        """AC#3 + FR#13: user_text/assistant_text from chunks row (pre-bounded) are returned as-is."""
        conn = make_vec_conn()
        query_vec = [1.0 / EMBEDDING_DIM**0.5] * EMBEDDING_DIM

        _branch_id, _c0, _c1, _sess_id = _seed_two_chunks_same_branch(conn, query_vec)

        with (
            patch("ccrecall.search_conversations.model_available", return_value=True),
            patch("ccrecall.search_conversations.embed_text", return_value=query_vec),
        ):
            snippets, _ranked = search_messages(conn, "test query", max_results=10)

        s0 = next(s for s in snippets if s["exchange_index"] == 0)
        assert s0["user"] == "first exchange user turn", "user field must come from chunks.user_text"
        assert s0["assistant"] == "first exchange assistant turn", (
            "assistant field must come from chunks.assistant_text"
        )
        conn.close()

    def test_vec0_unavailable_returns_ranked_false_empty(self):
        """AC#14: when chunk_vec_queryable returns False, search_messages returns ([], False)."""
        conn = sqlite3.connect(":memory:")
        # No sqlite-vec extension loaded → chunk_vec_queryable returns False
        with patch("ccrecall.search_conversations.model_available", return_value=True):
            snippets, ranked = search_messages(conn, "some query", max_results=5)
        assert snippets == [], "vec0 unavailable: must return empty list"
        assert ranked is False, "vec0 unavailable: must return ranked=False"
        conn.close()

    def test_run_messages_vec0_unavailable_returns_empty_envelope(self, tmp_path, capsys):
        """AC#14: run_messages with vec0 unavailable emits an empty ranked:false JSON
        envelope and returns normally (no SystemExit), so the CLI process exits 0."""
        db_path = tmp_path / "test.db"
        c = sqlite3.connect(str(db_path))
        c.executescript(SCHEMA_CORE)
        c.commit()
        c.close()

        # Patch chunk_vec_queryable at the search module level to simulate unavailability
        with (
            patch("ccrecall.search_conversations.model_available", return_value=True),
            patch("ccrecall.search_conversations.chunk_vec_queryable", return_value=False),
        ):
            result = run_messages(query="some query", output_format="json", db=db_path)

        assert result is None, "run_messages returns normally on the vec0-unavailable path → CLI exits 0"
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ranked"] is False, "envelope must have ranked:false when vec0 unavailable"
        assert data["results"] == [], "envelope must have empty results when vec0 unavailable"
        assert data["count"] == 0

    @pytest.mark.skipif(
        not vec_available(sqlite3.connect(":memory:")),
        reason="sqlite-vec not available",
    )
    def test_snippet_json_contract_parity(self):
        """AC#10 snippet half: JSON snippet has all contract fields including matched_role:null, match_terms:[]."""
        conn = make_vec_conn()
        query_vec = [1.0 / EMBEDDING_DIM**0.5] * EMBEDDING_DIM

        _seed_two_chunks_same_branch(conn, query_vec)

        with (
            patch("ccrecall.search_conversations.model_available", return_value=True),
            patch("ccrecall.search_conversations.embed_text", return_value=query_vec),
        ):
            snippets, ranked = search_messages(conn, "test query", max_results=10)

        assert snippets, "Expected non-empty snippets for contract parity check"
        normalized = apply_scores(snippets, ranked)
        json_snippet = format_snippet_json(normalized[0])

        # All required contract fields must be present
        required_fields = {
            "score",
            "score_raw",
            "session_uuid",
            "handle",
            "project",
            "git_branch",
            "exchange_index",
            "matched_role",
            "timestamp",
            "user",
            "assistant",
            "match_terms",
        }
        missing = required_fields - set(json_snippet.keys())
        assert not missing, f"Missing contract fields in JSON snippet: {missing}"

        # Vector path: matched_role must be None, match_terms must be []
        assert json_snippet["matched_role"] is None, "Vector path: matched_role must be null"
        assert json_snippet["match_terms"] == [], "Vector path: match_terms must be []"

        # score_raw must be a float (1.0 - distance, higher=better)
        assert isinstance(json_snippet["score_raw"], float), "score_raw must be a float"
        assert 0.0 <= json_snippet["score_raw"] <= 1.0, "score_raw must be in [0, 1] for L2-normalized vecs"

        conn.close()

    @pytest.mark.skipif(
        not vec_available(sqlite3.connect(":memory:")),
        reason="sqlite-vec not available",
    )
    def test_snippet_markdown_contract_parity(self):
        """AC#10 snippet half: markdown snippet renders score, locator, user, assistant, tail ref."""
        conn = make_vec_conn()
        query_vec = [1.0 / EMBEDDING_DIM**0.5] * EMBEDDING_DIM

        _seed_two_chunks_same_branch(conn, query_vec)

        with (
            patch("ccrecall.search_conversations.model_available", return_value=True),
            patch("ccrecall.search_conversations.embed_text", return_value=query_vec),
        ):
            snippets, ranked = search_messages(conn, "test query", max_results=2)

        normalized = apply_scores(snippets, ranked)
        md = format_snippet_markdown(normalized[0])
        # Must include the ccrecall tail reference
        assert "ccrecall tail" in md, "Snippet markdown must include 'ccrecall tail' navigation hint"
        # Must include User and Asst labels
        assert "User:" in md
        assert "Asst:" in md
        conn.close()
