"""Tests for the chunk-grain embedding backfill hook.

Backfill embeds all eligible branches at chunk grain via embed_branch_chunks.
Resume processes only remaining branches; the heal clause re-embeds "version-done
but chunk_vec missing" rows. Per-batch progress is logged. Bumping EMBEDDING_VERSION
makes branches re-appear. A model-load failure marks nothing; one bad embed marks
exactly that branch.

Scope: only active leaves (is_active=1) with messages are embedded — inactive forks
and message-less branches are skipped.
Opt-in: --days bounds by recency, --limit caps the run.
"""

import builtins
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from conftest import VEC_SKIP, make_vec_conn, patched_clear, patched_record

from ccrecall.db import CONTENT_ERROR_VERSION
from ccrecall.embed_ops import MAX_WRITE_PATH_EMBEDS_PER_SYNC
from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.health import REASON_VEC_UNAVAILABLE
from ccrecall.hooks.backfill_embeddings import run
from ccrecall.hooks.backfill_query import BATCH_SIZE, EXIT_ABORT, EXIT_OK

# A fixed EMBEDDING_DIM-dim float vector for stubbing embed_text.
_FIXED_VEC = [0.001] * EMBEDDING_DIM

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(autouse=True)
def _isolate_embedding_status(monkeypatch):
    """Keep every test in this module off the real ~/.ccrecall sidecar.

    run()'s success path calls clear_embedding_failure(), which
    would otherwise delete a developer's live embedding-status.json. Default to a
    no-op; the status-recording tests re-patch this locally with a tmp-path
    side_effect, which overrides this guard inside their own `with patch(...)`.
    """
    monkeypatch.setattr("ccrecall.hooks.backfill_embeddings.clear_embedding_failure", lambda *a, **k: None)


# Monotonic counter for unique IDs across test helpers.
_branch_counter = [0]


_VEC_SKIP = VEC_SKIP


# Helpers


def _insert_branch_with_messages(
    conn: sqlite3.Connection,
    is_active: int = 1,
    ended_at: str | None = None,
    num_exchanges: int = 1,
    context_summary: str | None = None,
) -> int:
    """Insert a branch with messages and return its id.

    Creates `num_exchanges` user+assistant message pairs. The branch is active
    by default. `context_summary` may be NULL (chunk path doesn't require it).
    """
    _branch_counter[0] += 1
    uid = _branch_counter[0]

    conn.execute(
        "INSERT INTO sessions(uuid, project_id) VALUES (?, NULL)",
        (f"sess-{uid}",),
    )
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        """
        INSERT INTO branches(session_id, leaf_uuid, context_summary, summary_version,
                             is_active, ended_at)
        VALUES (?, ?, ?, 0, ?, ?)
        """,
        (session_id, f"leaf-{uid}", context_summary, is_active, ended_at),
    )
    branch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for i in range(num_exchanges):
        ts_h = 10 + i
        conn.execute(
            "INSERT INTO messages(session_id, uuid, role, content, timestamp) VALUES (?, ?, 'user', ?, ?)",
            (session_id, f"u-{uid}-{i}", f"User message {i}", f"2024-01-01T{ts_h:02d}:00:00Z"),
        )
        user_msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO branch_messages(branch_id, message_id) VALUES (?, ?)",
            (branch_id, user_msg_id),
        )

        conn.execute(
            "INSERT INTO messages(session_id, uuid, role, content, timestamp) VALUES (?, ?, 'assistant', ?, ?)",
            (session_id, f"a-{uid}-{i}", f"Assistant response {i}", f"2024-01-01T{ts_h:02d}:30:00Z"),
        )
        asst_msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO branch_messages(branch_id, message_id) VALUES (?, ?)",
            (branch_id, asst_msg_id),
        )

    conn.commit()
    return branch_id


def _insert_assistant_only_branch(conn: sqlite3.Connection, num_messages: int = 3) -> int:
    """Insert an active branch whose only messages are assistant-role (no user turns).

    Mirrors a sub-agent / sidechain transcript: eligible under CHUNK_EMBEDDABLE
    (it has branch_messages) but build_exchange_pairs yields nothing, so there is
    nothing to embed. Such a branch must not perpetually re-select and stall.
    """
    _branch_counter[0] += 1
    uid = _branch_counter[0]

    conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, NULL)", (f"sess-{uid}",))
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO branches(session_id, leaf_uuid, summary_version, is_active) VALUES (?, ?, 0, 1)",
        (session_id, f"leaf-{uid}"),
    )
    branch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for i in range(num_messages):
        ts_h = 10 + i
        conn.execute(
            "INSERT INTO messages(session_id, uuid, role, content, timestamp) VALUES (?, ?, 'assistant', ?, ?)",
            (session_id, f"a-{uid}-{i}", f"Assistant message {i}", f"2024-01-01T{ts_h:02d}:00:00Z"),
        )
        msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO branch_messages(branch_id, message_id) VALUES (?, ?)", (branch_id, msg_id))

    conn.commit()
    return branch_id


def _branch_embedding_version(conn: sqlite3.Connection, branch_id: int) -> int | None:
    row = conn.execute("SELECT embedding_version FROM branches WHERE id = ?", (branch_id,)).fetchone()
    return row[0] if row else None


def _chunk_count(conn: sqlite3.Connection) -> int:
    """Total chunk_vec rows."""
    return conn.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0]


def _branch_has_chunk_vecs(conn: sqlite3.Connection, branch_id: int) -> bool:
    """True if any chunk_vec exists for chunks belonging to this branch."""
    return (
        conn.execute(
            """
            SELECT COUNT(*) FROM chunk_vec
            JOIN chunks ON chunk_vec.chunk_id = chunks.id
            WHERE chunks.branch_id = ?
            """,
            (branch_id,),
        ).fetchone()[0]
        > 0
    )


def _chunks_for_branch(conn: sqlite3.Connection, branch_id: int) -> list[tuple]:
    """Return (id, embedding_version, first_message_uuid) for all chunks of a branch."""
    return conn.execute(
        "SELECT id, embedding_version, first_message_uuid FROM chunks WHERE branch_id = ?",
        (branch_id,),
    ).fetchall()


class _NoCloseConn:
    """Wrapper that delegates to a sqlite3.Connection but makes close() a no-op.

    Stands in for get_connection() (a @contextlib.contextmanager) in these tests
    via `patch(..., return_value=_NoCloseConn(conn))`: __enter__/__exit__ let it
    satisfy `with get_connection(...) as conn:` without the production commit/
    rollback/close behavior, so the test keeps access to the same connection
    (and its rows) after run() returns.
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


def _run_backfill_with_stub(conn: sqlite3.Connection, *, days=None, limit=None):
    """Run run() with embed_text stubbed via session_ops to _FIXED_VEC."""
    with (
        patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
        patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts: [_FIXED_VEC] * len(texts)),
        patch(
            "ccrecall.hooks.backfill_embeddings.get_connection",
            return_value=_NoCloseConn(conn),
        ),
        patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
        patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        # Patch clear so the success path never touches the real ~/.ccrecall sidecar.
        patch("ccrecall.hooks.backfill_embeddings.clear_embedding_failure"),
    ):
        run(days=days, limit=limit)


# Backfill embeds all eligible branches


@_VEC_SKIP
class TestBackfillEmbedsFull:
    def test_all_eligible_branches_embedded(self):
        """All active-leaf branches with messages get chunk-embedded."""
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn) for _ in range(5)]

        _run_backfill_with_stub(conn)

        assert _chunk_count(conn) == 5  # 1 exchange per branch
        for bid in ids:
            assert _branch_embedding_version(conn, bid) == EMBEDDING_VERSION
            assert _branch_has_chunk_vecs(conn, bid)

    def test_long_branch_fully_embedded_in_one_run(self):
        """A branch with more exchanges than the write-path cap is FULLY embedded.

        The backfill passes max_embeds=None so a single run embeds every exchange.
        With the write-path cap inherited, this branch would get only the cap's
        worth of chunks, stay eligible, and trip the no-progress guard (EXIT_ABORT)
        on re-selection — leaving long sessions (the feature's whole point)
        permanently under-embedded. Guards version-bump eligibility for branches longer than the cap.
        """
        conn = make_vec_conn()
        n_exchanges = MAX_WRITE_PATH_EMBEDS_PER_SYNC * 2 + 3  # well over the write-path cap
        bid = _insert_branch_with_messages(conn, num_exchanges=n_exchanges)

        exit_code = None
        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts: [_FIXED_VEC] * len(texts)),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            exit_code = run()

        # No no-progress abort, and every exchange has a current-version chunk vector.
        assert exit_code == EXIT_OK, "backfill must complete (not no-progress abort) on a long branch"
        assert _branch_embedding_version(conn, bid) == EMBEDDING_VERSION
        chunk_vec_for_branch = conn.execute(
            "SELECT COUNT(*) FROM chunk_vec JOIN chunks ON chunk_vec.chunk_id = chunks.id WHERE chunks.branch_id = ?",
            (bid,),
        ).fetchone()[0]
        assert chunk_vec_for_branch == n_exchanges, (
            f"all {n_exchanges} exchanges must have a chunk vector after backfill, got {chunk_vec_for_branch}"
        )

    def test_null_summary_still_embedded(self):
        """Branches with NULL context_summary are embedded — the chunk path
        reads raw exchange text, not the summary."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn, context_summary=None)

        _run_backfill_with_stub(conn)

        assert _branch_has_chunk_vecs(conn, bid)
        assert _branch_embedding_version(conn, bid) == EMBEDDING_VERSION

    def test_branch_without_messages_skipped(self):
        """A branch with no messages is not eligible (CHUNK_EMBEDDABLE requires messages)."""
        conn = make_vec_conn()
        _branch_counter[0] += 1
        uid = _branch_counter[0]
        conn.execute("INSERT INTO sessions(uuid, project_id) VALUES (?, NULL)", (f"sess-{uid}",))
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO branches(session_id, leaf_uuid, is_active) VALUES (?, ?, 1)",
            (session_id, f"leaf-{uid}"),
        )
        conn.commit()

        _run_backfill_with_stub(conn)

        assert _chunk_count(conn) == 0

    def test_version_columns_set_correctly(self):
        """embedding_version and embedding_model are written on the branch watermark."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)

        _run_backfill_with_stub(conn)

        row = conn.execute(
            "SELECT embedding_version, embedding_model FROM branches WHERE id = ?",
            (bid,),
        ).fetchone()
        assert row[0] == EMBEDDING_VERSION
        assert row[1] == EMBEDDING_MODEL

    def test_commits_per_batch(self):
        """Each batch is committed; data is durable between batches."""
        conn = make_vec_conn()
        count = BATCH_SIZE + 3
        for _ in range(count):
            _insert_branch_with_messages(conn)

        _run_backfill_with_stub(conn)

        assert _chunk_count(conn) == count


# Resume processes only remaining; heal clause for missing chunk_vecs


@_VEC_SKIP
class TestBackfillResume:
    def test_resume_skips_already_done(self):
        """Second run does not re-embed already-done branches."""
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn) for _ in range(4)]

        call_count = [0]

        def counting_embed(texts: list[str]) -> list[list[float]]:
            call_count[0] += len(texts)
            return [_FIXED_VEC[:]] * len(texts)

        def _run_counting(conn):
            with (
                patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
                patch("ccrecall.embed_ops.embed_batch", side_effect=counting_embed),
                patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
                patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
                patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
            ):
                run()

        _run_counting(conn)
        first_run_calls = call_count[0]
        assert first_run_calls == len(ids)

        # Second run: nothing new to process
        call_count[0] = 0
        _run_counting(conn)
        assert call_count[0] == 0

    def test_resume_processes_new_branch(self):
        """After first run, a newly added branch is processed on second run."""
        conn = make_vec_conn()
        _insert_branch_with_messages(conn)

        _run_backfill_with_stub(conn)
        assert _chunk_count(conn) == 1

        new_id = _insert_branch_with_messages(conn)
        _run_backfill_with_stub(conn)

        assert _chunk_count(conn) == 2
        assert _branch_has_chunk_vecs(conn, new_id)

    def test_heal_clause_chunk_without_vec(self):
        """Heal clause: chunk row exists with no chunk_vec, watermark reads
        EMBEDDING_VERSION → branch is re-selected and the missing vector is created."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)

        # Run once to create the chunk rows and set watermark
        _run_backfill_with_stub(conn)
        assert _branch_has_chunk_vecs(conn, bid)

        # Simulate a crash victim: delete the chunk_vec row but leave the chunks row
        conn.execute(
            "DELETE FROM chunk_vec WHERE chunk_id IN (SELECT id FROM chunks WHERE branch_id = ?)",
            (bid,),
        )
        conn.commit()
        assert not _branch_has_chunk_vecs(conn, bid)

        # Heal clause should re-select this branch and recreate the missing vector
        _run_backfill_with_stub(conn)
        assert _branch_has_chunk_vecs(conn, bid)


# Version-bump eligibility


@_VEC_SKIP
class TestBackfillVersionBump:
    def test_version_bump_makes_branches_eligible(self):
        """After an EMBEDDING_VERSION bump, all active-leaf branches are eligible.

        Simulates the post-bump state by inserting branches with a stale watermark
        (embedding_version = 0) and verifying backfill re-embeds them.
        """
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn) for _ in range(3)]
        # Seed as "done at old version 0" — stale watermark
        for bid in ids:
            conn.execute("UPDATE branches SET embedding_version = 0 WHERE id = ?", (bid,))
        conn.commit()

        _run_backfill_with_stub(conn)

        for bid in ids:
            assert _branch_embedding_version(conn, bid) == EMBEDDING_VERSION
            assert _branch_has_chunk_vecs(conn, bid)

    def test_model_change_reselects(self):
        """A branch at current version but a stale embedding_model is re-embedded."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)
        conn.execute(
            "UPDATE branches SET embedding_version = ?, embedding_model = 'old/stale-model' WHERE id = ?",
            (EMBEDDING_VERSION, bid),
        )
        conn.commit()

        _run_backfill_with_stub(conn)

        row = conn.execute("SELECT embedding_model FROM branches WHERE id = ?", (bid,)).fetchone()
        assert row[0] == EMBEDDING_MODEL

    def test_embedding_version_stale_reselects(self):
        """A branch with embedding_version < EMBEDDING_VERSION is re-embedded."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)
        conn.execute(
            "UPDATE branches SET embedding_version = 0, embedding_model = ? WHERE id = ?",
            (EMBEDDING_MODEL, bid),
        )
        conn.commit()

        _run_backfill_with_stub(conn)

        assert _branch_embedding_version(conn, bid) == EMBEDDING_VERSION
        assert _branch_has_chunk_vecs(conn, bid)


# No-progress guard: loop breaks when same batch re-selected


@_VEC_SKIP
class TestBackfillNoProgressGuard:
    def test_guard_fires_when_embed_does_not_advance_row(self):
        """If embed_branch_chunks is a no-op (row stays eligible), the no-progress
        guard detects the same batch on the next iteration and aborts rather than
        looping forever."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            # no-op: row never advanced; return_value=0 so total_inferences stays an int
            patch("ccrecall.hooks.backfill_embeddings.embed_branch_chunks", return_value=0),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            run()  # must return, not hang

        # Row was never stamped (embed was a no-op), confirming exit via the guard.
        ev = _branch_embedding_version(conn, bid)
        assert ev != EMBEDDING_VERSION, "Row should not be at EMBEDDING_VERSION — embed was no-op"


@_VEC_SKIP
class TestBackfillZeroExchangeBranches:
    """Assistant-only (sub-agent/sidechain) branches are eligible under
    CHUNK_EMBEDDABLE but have no embeddable exchange. They must leave the
    eligible set instead of re-selecting forever and stalling the backfill."""

    def test_assistant_only_branch_does_not_stall(self):
        """A branch with no user turns embeds nothing but advances its watermark,
        so it isn't perpetually re-selected."""
        conn = make_vec_conn()
        bid = _insert_assistant_only_branch(conn)

        _run_backfill_with_stub(conn)

        assert _chunk_count(conn) == 0
        assert _branch_embedding_version(conn, bid) == EMBEDDING_VERSION

    def test_assistant_only_does_not_block_real_branch(self):
        """A whole batch of zero-exchange branches must not prevent a real,
        embeddable branch behind them from being reached and embedded — the
        roadblock that the pre-fix no-progress guard hit."""
        conn = make_vec_conn()
        # Insert the zero-exchange branches first so they fill the first batch
        # (selection is ORDER BY id) and the real branch lands in a later batch.
        for _ in range(BATCH_SIZE):
            _insert_assistant_only_branch(conn)
        real = _insert_branch_with_messages(conn)

        _run_backfill_with_stub(conn)

        assert _branch_has_chunk_vecs(conn, real), (
            "real branch must embed, not be blocked behind zero-exchange branches"
        )


# Failure modes: model failure marks nothing; content errors mark only that row


@_VEC_SKIP
class TestBackfillFailureModes:
    def test_model_unavailable_marks_nothing(self):
        """model_available=False → zero rows marked, all stay eligible."""
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn) for _ in range(3)]

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=False),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
        ):
            run()

        for bid in ids:
            ev = _branch_embedding_version(conn, bid)
            assert ev != CONTENT_ERROR_VERSION, f"branch {bid} should not be marked -1"
        assert _chunk_count(conn) == 0

    def test_single_bad_embed_marks_only_itself(self):
        """One branch whose embed_text raises marks only that branch -1; rest succeed."""
        conn = make_vec_conn()
        good_id1 = _insert_branch_with_messages(conn)
        bad_id = _insert_branch_with_messages(conn)
        good_id2 = _insert_branch_with_messages(conn)

        # Raise on the second call (bad_id was inserted second)
        call_no = [0]

        def counting_embed(texts: list[str]) -> list[list[float]]:
            call_no[0] += 1
            if call_no[0] == 2:  # second branch's embed raises
                raise ValueError("simulated tokenizer overflow")
            return [_FIXED_VEC] * len(texts)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=counting_embed),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            run()

        # Second branch (bad_id) is marked -1
        assert _branch_embedding_version(conn, bad_id) == CONTENT_ERROR_VERSION
        assert not _branch_has_chunk_vecs(conn, bad_id)

        # First and third branches succeeded
        assert _branch_embedding_version(conn, good_id1) == EMBEDDING_VERSION
        assert _branch_embedding_version(conn, good_id2) == EMBEDDING_VERSION
        assert _branch_has_chunk_vecs(conn, good_id1)
        assert _branch_has_chunk_vecs(conn, good_id2)

    def test_infra_failure_marks_no_rows(self):
        """RuntimeError from embed_text (infra failure) → zero rows marked -1, all stay eligible."""
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn) for _ in range(3)]

        def infra_fail(texts: list[str]) -> list[list[float]]:
            raise RuntimeError("ONNX session crashed")

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=infra_fail),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            run()

        for bid in ids:
            ev = _branch_embedding_version(conn, bid)
            assert ev != CONTENT_ERROR_VERSION, f"branch {bid} should not be marked -1 on infra failure"
        assert _chunk_count(conn) == 0

    def test_sentinel_row_not_reprocessed(self):
        """A branch with embedding_version=-1 is excluded from the selection predicate."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)
        conn.execute("UPDATE branches SET embedding_version = -1 WHERE id = ?", (bid,))
        conn.commit()

        call_count = [0]

        def counting_embed(texts: list[str]) -> list[list[float]]:
            call_count[0] += len(texts)
            return [_FIXED_VEC] * len(texts)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=counting_embed),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            run()

        assert call_count[0] == 0

    def test_content_error_vs_batch_abort(self):
        """T1: sqlite3.Error from fetch_branch_messages is a batch-abort (EXIT_ABORT),
        NOT a per-row content-error sentinel — the two must not be conflated."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            # Simulate an infra failure during message fetch
            patch(
                "ccrecall.hooks.backfill_embeddings.fetch_branch_messages",
                side_effect=sqlite3.OperationalError("disk I/O error"),
            ),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            exit_code = run()

        # Must be a batch abort, not per-row error sentinel
        assert exit_code == EXIT_ABORT
        # Branch must NOT be marked CONTENT_ERROR_VERSION
        assert _branch_embedding_version(conn, bid) != CONTENT_ERROR_VERSION


# Scope: only active leaves with messages are embedded


@_VEC_SKIP
class TestBackfillScopeActive:
    def test_inactive_branch_not_embedded(self):
        """is_active=0 branches are skipped — vectors on them are never returnable."""
        conn = make_vec_conn()
        active = _insert_branch_with_messages(conn, is_active=1)
        inactive = _insert_branch_with_messages(conn, is_active=0)

        _run_backfill_with_stub(conn)

        assert _branch_has_chunk_vecs(conn, active)
        assert not _branch_has_chunk_vecs(conn, inactive)
        assert _chunk_count(conn) == 1


# Opt-in flags: --days bounds recency, --limit caps the run


@_VEC_SKIP
class TestBackfillFlags:
    def test_days_excludes_branches_outside_window(self):
        """--days N only embeds active leaves ended within the last N days."""
        conn = make_vec_conn()
        recent = _insert_branch_with_messages(conn)
        old = _insert_branch_with_messages(conn)
        conn.execute("UPDATE branches SET ended_at = datetime('now') WHERE id = ?", (recent,))
        conn.execute("UPDATE branches SET ended_at = datetime('now', '-60 days') WHERE id = ?", (old,))
        conn.commit()

        _run_backfill_with_stub(conn, days=30)

        assert _branch_has_chunk_vecs(conn, recent)
        assert not _branch_has_chunk_vecs(conn, old)
        assert _chunk_count(conn) == 1

    def test_limit_caps_embeds_across_batches(self):
        """--limit N stops after N branches even though batches are BATCH_SIZE-wide."""
        conn = make_vec_conn()
        for _ in range(5):
            _insert_branch_with_messages(conn)

        _run_backfill_with_stub(conn, limit=2)

        assert _chunk_count(conn) == 2


# Backfill locator: first_message_uuid is set on backfilled chunks (M10)


@_VEC_SKIP
class TestBackfillLocator:
    def test_backfilled_chunks_have_first_message_uuid(self):
        """M10: backfilled chunks carry a non-NULL first_message_uuid (fetch_branch_messages
        selects m.uuid so the locator is populated even for historical branches)."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn, num_exchanges=2)

        _run_backfill_with_stub(conn)

        chunks = _chunks_for_branch(conn, bid)
        assert len(chunks) == 2
        for chunk_id, _ev, uuid in chunks:
            assert uuid is not None, f"chunk {chunk_id} missing first_message_uuid"


# History preservation


@_VEC_SKIP
class TestHistoryPreservation:
    def test_messages_branches_unchanged_after_backfill(self):
        """messages/branches/branch_messages row counts and contents are
        unchanged across an EMBEDDING_VERSION bump + backfill."""
        conn = make_vec_conn()
        for _ in range(3):
            _insert_branch_with_messages(conn, num_exchanges=2)

        def _snapshot(conn):
            return {
                "messages": conn.execute("SELECT id, content FROM messages ORDER BY id").fetchall(),
                "branches_core": conn.execute(
                    "SELECT id, session_id, leaf_uuid, is_active FROM branches ORDER BY id"
                ).fetchall(),
                "branch_messages": conn.execute(
                    "SELECT branch_id, message_id FROM branch_messages ORDER BY branch_id, message_id"
                ).fetchall(),
            }

        before = _snapshot(conn)
        _run_backfill_with_stub(conn)
        after = _snapshot(conn)

        assert after["messages"] == before["messages"]
        assert after["branches_core"] == before["branches_core"]
        assert after["branch_messages"] == before["branch_messages"]


# --status: read-only chunk-coverage progress reporter


def _run_status(conn: sqlite3.Connection, capsys, *, json_mode=False, days=None):
    """Invoke run(status=True, ...) against conn; return captured stdout."""
    with (
        patch("ccrecall.hooks.backfill_status.get_connection", return_value=_NoCloseConn(conn)),
        patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
    ):
        code = run(status=True, json_mode=json_mode, days=days)
    assert code == 0
    return capsys.readouterr().out


@_VEC_SKIP
class TestBackfillStatus:
    def _seed_mixed(self, conn: sqlite3.Connection) -> None:
        """3 done branches (embedded), 2 eligible, 1 errored — 6 total branches."""
        for _ in range(3):
            _insert_branch_with_messages(conn)
        _run_backfill_with_stub(conn)  # marks those 3 done (3 chunk_vec rows)
        for _ in range(2):
            _insert_branch_with_messages(conn)
        errored = _insert_branch_with_messages(conn)
        conn.execute("UPDATE branches SET embedding_version = -1 WHERE id = ?", (errored,))
        conn.commit()

    def test_json_counts(self, capsys):
        conn = make_vec_conn()
        self._seed_mixed(conn)

        out = _run_status(conn, capsys, json_mode=True)
        data = json.loads(out)

        # universe = total chunks (3 done branches x 1 exchange = 3 chunks)
        assert data["universe"] == 3
        # done = current-version chunks with chunk_vec (the 3 embedded above)
        assert data["done"] == 3
        # eligible = branches still needing work (the 2 new eligible ones)
        assert data["eligible"] == 2
        # errored = branches with content-error sentinel (1)
        assert data["errored"] == 1
        # branch grain: 6 embeddable total, 3 embedded (= total - eligible - errored)
        assert data["total_branches"] == 6
        assert data["embedded_branches"] == 3
        assert data["days"] is None

    def test_human_output_reports_branch_coverage(self, capsys):
        conn = make_vec_conn()
        self._seed_mixed(conn)

        out = _run_status(conn, capsys)

        # Honest branch-grain coverage, not the misleading partial-universe chunk %.
        assert "3 / 6 embedded" in out
        assert "remaining: 2 branches" in out
        assert "errored" in out
        assert "chunks" not in out

    def test_days_filters_counts(self, capsys):
        """--status --days N bounds universe/eligible/errored by recency."""
        conn = make_vec_conn()
        recent = _insert_branch_with_messages(conn)
        recent_err = _insert_branch_with_messages(conn)
        old = _insert_branch_with_messages(conn)  # ended 60d ago → out of window
        _insert_branch_with_messages(conn)  # NULL ended_at → out of window

        conn.execute(
            "UPDATE branches SET ended_at = datetime('now') WHERE id IN (?, ?)",
            (recent, recent_err),
        )
        conn.execute(
            "UPDATE branches SET ended_at = datetime('now', '-60 days') WHERE id = ?",
            (old,),
        )
        conn.execute("UPDATE branches SET embedding_version = -1 WHERE id = ?", (recent_err,))
        conn.commit()

        out = _run_status(conn, capsys, json_mode=True, days=30)
        data = json.loads(out)

        # Only recent (eligible) and recent_err (errored) are within the window.
        # universe = chunks in those branches = 0 (neither has been embedded yet,
        # but universe counts existing chunks; recent has none since not yet run)
        assert data["eligible"] == 1
        assert data["errored"] == 1
        # Branch grain is recency-bounded too: 2 in-window branches, 0 embedded.
        assert data["total_branches"] == 2
        assert data["embedded_branches"] == 0
        assert data["days"] == 30

    def test_status_does_not_embed(self, capsys):
        """--status is read-only: it must not write any vectors."""
        conn = make_vec_conn()
        _insert_branch_with_messages(conn)

        before = _chunk_count(conn)
        _run_status(conn, capsys, json_mode=True)

        assert _chunk_count(conn) == before == 0


# Total inferences counter: reported alongside branches


@_VEC_SKIP
class TestBackfillInferencesCounter:
    def test_json_output_includes_inferences(self):
        """The JSON completion output includes 'inferences' alongside 'embedded'."""
        conn = make_vec_conn()
        _insert_branch_with_messages(conn, num_exchanges=3)

        captured = []

        original_print = builtins.print

        def capturing_print(*args, **kwargs):
            if kwargs.get("file") is None:
                captured.append(args[0] if args else "")
            original_print(*args, **kwargs)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts: [_FIXED_VEC] * len(texts)),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
            patch("builtins.print", side_effect=capturing_print),
        ):
            run(json_mode=True)

        # At least one JSON line was captured to stdout
        json_lines = [c for c in captured if c.startswith("{")]
        assert json_lines, "Expected JSON output on stdout"
        data = json.loads(json_lines[-1])
        assert "inferences" in data
        assert data["inferences"] >= 1  # at least 1 exchange was embedded
        assert data["embedded"] >= 1


# Embedding-status sidecar recording/clearing in backfill_embeddings.run()


class TestBackfillEmbeddingStatusRecording:
    """backfill_embeddings.run() records capability failures and clears on success.

    Only the points inside run() (not run_status()) are instrumented — do NOT
    instrument run_status(), which is a read-only diagnostic.
    """

    def test_model_unavailable_records_reason(self, tmp_path):
        """model_available() → False inside run() writes 'model_unavailable' to sidecar."""
        sidecar = tmp_path / "embedding-status.json"

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=False),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch(
                "ccrecall.hooks.backfill_embeddings.record_embedding_failure",
                side_effect=patched_record(sidecar),
            ),
        ):
            run()

        assert sidecar.exists(), "sidecar must be written on model-unavailable abort"
        data = json.loads(sidecar.read_text())
        assert data["reason"] == "model_unavailable"
        assert "since" in data

    def test_vec_unavailable_records_reason(self, tmp_path):
        """chunk_vec_queryable() → False inside run() writes 'vec_unavailable' to sidecar."""
        sidecar = tmp_path / "embedding-status.json"
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=mock_conn),
            patch("ccrecall.hooks.backfill_embeddings.chunk_vec_queryable", return_value=False),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch(
                "ccrecall.hooks.backfill_embeddings.record_embedding_failure",
                side_effect=patched_record(sidecar),
            ),
        ):
            run()

        assert sidecar.exists(), "sidecar must be written on vec-unavailable abort"
        data = json.loads(sidecar.read_text())
        assert data["reason"] == "vec_unavailable"
        assert "since" in data

    @_VEC_SKIP
    def test_successful_run_clears_status(self, tmp_path):
        """A run() that embeds successfully clears the embedding-status sidecar."""
        conn = make_vec_conn()
        _insert_branch_with_messages(conn)

        sidecar = tmp_path / "embedding-status.json"
        # Pre-seed sidecar as if there was a prior failure
        sidecar.write_text(json.dumps({"reason": REASON_VEC_UNAVAILABLE, "since": "2026-01-01T00:00:00Z"}))

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts: [_FIXED_VEC] * len(texts)),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
            patch(
                "ccrecall.hooks.backfill_embeddings.clear_embedding_failure",
                side_effect=patched_clear(sidecar),
            ),
        ):
            run()

        assert not sidecar.exists(), "sidecar must be absent (cleared) after a successful embedding run"

    def test_run_status_does_not_record(self, tmp_path):
        """run(status=True) (run_status) must NOT record to the sidecar even when vec is unavailable.

        Recording inside run_status() would fire a false alert every time the user
        runs `ccrecall backfill embeddings --status` — a design violation (Edge Cases).
        """
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False

        record_calls = []

        with (
            patch("ccrecall.hooks.backfill_status.get_connection", return_value=mock_conn),
            patch("ccrecall.hooks.backfill_status.chunk_vec_queryable", return_value=False),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch(
                "ccrecall.hooks.backfill_embeddings.record_embedding_failure",
                side_effect=lambda reason: record_calls.append(reason),
            ),
        ):
            run(status=True)

        assert record_calls == [], (
            f"run_status() (--status path) must never call record_embedding_failure; got calls: {record_calls}"
        )


class TestBackfillETAProgress:
    """#84: ETA uses message-proportional work units and windowed rate."""

    def test_progress_shows_warming_up_for_initial_branches(self, capsys):
        """During the first few branches, ETA shows 'warming up' instead of
        a misleading extrapolation from warm-up throughput."""
        conn = make_vec_conn()
        # Create 3 branches — below the warmup threshold
        for _ in range(3):
            _insert_branch_with_messages(conn, num_exchanges=2)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts: [_FIXED_VEC] * len(texts)),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
            patch("ccrecall.hooks.backfill_embeddings.clear_embedding_failure"),
        ):
            code = run(progress_every=1)

        assert code == EXIT_OK
        captured = capsys.readouterr()
        # First progress lines should say "warming up" since < _WARMUP_BRANCHES
        lines = [line for line in captured.err.splitlines() if "ETA" in line]
        assert any("warming up" in line for line in lines)

    def test_progress_shows_numeric_eta_after_warmup(self, capsys):
        """After enough branches, ETA switches from 'warming up' to a numeric estimate."""
        conn = make_vec_conn()
        for _ in range(10):
            _insert_branch_with_messages(conn, num_exchanges=2)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts: [_FIXED_VEC] * len(texts)),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
            patch("ccrecall.hooks.backfill_embeddings.clear_embedding_failure"),
        ):
            code = run(progress_every=1)

        assert code == EXIT_OK
        captured = capsys.readouterr()
        lines = [line for line in captured.err.splitlines() if "ETA" in line]
        # After warmup, at least some lines should have a numeric ETA (not "warming up")
        numeric_eta_lines = [line for line in lines if "warming up" not in line]
        assert len(numeric_eta_lines) > 0

    def test_progress_reported_despite_all_content_errors(self, capsys):
        """A run where every branch hits a content error still emits progress
        lines, because the gate counts branches processed (success or
        content-error), not total_updated (successes only)."""
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn, num_exchanges=2) for _ in range(3)]

        def always_fail(texts: list[str]) -> list[list[float]]:
            raise ValueError("simulated tokenizer overflow")

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=always_fail),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
            patch("ccrecall.hooks.backfill_embeddings.clear_embedding_failure"),
        ):
            code = run(progress_every=1)

        assert code == EXIT_OK
        # Every branch content-errored — total_updated (successes) stayed 0.
        for bid in ids:
            assert _branch_embedding_version(conn, bid) == CONTENT_ERROR_VERSION
        assert _chunk_count(conn) == 0

        captured = capsys.readouterr()
        eta_lines = [line for line in captured.err.splitlines() if "ETA" in line]
        assert len(eta_lines) > 0, "progress line should print even though every branch content-errored"
