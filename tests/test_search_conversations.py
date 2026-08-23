"""Tests for the three-state compute_caveat (FR#16, AC#12).

Full compute_caveat coverage for the vec-unavailable/error-degradation/
at-threshold cases already lives in test_search.py's TestRecallCaveat class;
this file covers only the new draft-quality branch state that
branch_embedding_coverage's three-tuple return adds.
"""

import sqlite3
from unittest.mock import patch

from ccrecall.schema import SCHEMA
from ccrecall.search_conversations import compute_caveat


def _seed_branch(conn, i: int, *, cap_tokens: int | None, has_chunk: bool = True) -> None:
    """Insert an active, CHUNK_EMBEDDABLE branch with a stale watermark and one chunk."""
    conn.execute("INSERT INTO projects (path, key, name) VALUES (?, ?, ?)", (f"/p/{i}", f"-p{i}", f"p{i}"))
    proj_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO sessions (uuid, project_id) VALUES (?, ?)", (f"sess-{i}", proj_id))
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, ?, 1)",
        (session_id, f"leaf-{i}"),
    )
    branch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, ?, 'user', 'x', '2025-01-01T00:00:00Z')",
        (session_id, f"m-{i}"),
    )
    msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)", (branch_id, msg_id))
    if has_chunk:
        conn.execute(
            "INSERT INTO chunks (branch_id, exchange_index, content_hash, cap_tokens) VALUES (?, 0, 'hash', ?)",
            (branch_id, cap_tokens),
        )
    conn.commit()


class TestComputeCaveatDraftQuality:
    def test_draft_only_branch_mentions_draft_quality_not_not_embedded(self):
        """AC#12: a branch with only draft-quality chunks reads as 'draft quality',
        not as an unqualified 'not embedded'/low-percentage caveat."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()
        _seed_branch(conn, 1, cap_tokens=4096)

        with patch("ccrecall.search_conversations.chunk_vec_queryable", return_value=True):
            caveat = compute_caveat(conn)

        assert caveat is not None
        assert "draft quality" in caveat.lower()
        conn.close()

    def test_mixed_full_and_draft_branches_reports_both(self):
        """A DB with both fully-embedded and draft-quality branches surfaces both counts."""
        from ccrecall.embeddings import EMBEDDING_MODEL, EMBEDDING_VERSION

        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()
        conn.execute("INSERT INTO projects (path, key, name) VALUES ('/p', '-p', 'p')")
        proj_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO sessions (uuid, project_id) VALUES ('s1', ?)", (proj_id,))
        s1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO branches (session_id, leaf_uuid, is_active, embedding_version, embedding_model)"
            " VALUES (?, 'l1', 1, ?, ?)",
            (s1, EMBEDDING_VERSION, EMBEDDING_MODEL),
        )
        b1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO messages (session_id, uuid, role, content, timestamp) VALUES (?, 'm1', 'user', 'x', '2025-01-01T00:00:00Z')",
            (s1,),
        )
        m1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO branch_messages VALUES (?, ?)", (b1, m1))
        _seed_branch(conn, 2, cap_tokens=2048)
        conn.commit()

        with patch("ccrecall.search_conversations.chunk_vec_queryable", return_value=True):
            caveat = compute_caveat(conn)

        assert caveat is not None
        assert "50%" in caveat
        assert "draft quality" in caveat.lower()
        conn.close()

    def test_no_draft_branches_omits_draft_quality_wording(self):
        """No draft-quality branches → the ordinary partial-coverage caveat, no 'draft quality' mention."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        conn.commit()
        _seed_branch(conn, 1, cap_tokens=None, has_chunk=False)

        with patch("ccrecall.search_conversations.chunk_vec_queryable", return_value=True):
            caveat = compute_caveat(conn)

        assert caveat is not None
        assert "draft quality" not in caveat.lower()
        conn.close()
