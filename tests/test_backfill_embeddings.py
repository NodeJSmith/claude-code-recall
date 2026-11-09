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
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import VEC_SKIP, locked_error, make_vec_conn, patched_clear, patched_record

from ccrecall import config, job_lock, run_history
from ccrecall.cli.commands import cmd_backfill_embeddings
from ccrecall.config import JOB_KEY_BACKFILL_EMBEDDINGS as JOB_KEY
from ccrecall.db import CONTENT_ERROR_VERSION
from ccrecall.db_vec import find_stamped_branches_missing_chunks, upsert_chunk_vec
from ccrecall.embed_ops import MAX_WRITE_PATH_EMBEDS_PER_SYNC, EmbedOutcome, embed_branch_chunks
from ccrecall.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, EMBEDDING_VERSION
from ccrecall.health import REASON_VEC_UNAVAILABLE
from ccrecall.hooks.backfill_embeddings import run
from ccrecall.hooks.backfill_query import BATCH_SIZE, EXIT_ABORT, EXIT_OK, build_selection

# A fixed EMBEDDING_DIM-dim float vector for stubbing embed_text.
_FIXED_VEC = [0.001] * EMBEDDING_DIM

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


_VEC_SKIP = VEC_SKIP


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
        patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kwargs: [_FIXED_VEC] * len(texts)),
        patch(
            "ccrecall.hooks.backfill_embeddings.get_connection",
            return_value=_NoCloseConn(conn),
        ),
        patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
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

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kwargs: [_FIXED_VEC] * len(texts)),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
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

    def test_every_batch_is_processed(self):
        conn = make_vec_conn()
        count = BATCH_SIZE + 3
        for _ in range(count):
            _insert_branch_with_messages(conn)

        _run_backfill_with_stub(conn)

        assert _chunk_count(conn) == count

    def test_each_branch_commits_and_no_lock_is_held_during_inference(self, tmp_path):
        """While one branch infers, the previous branch is already durable and
        another writer can take the lock without waiting."""
        db_path = str(tmp_path / "backfill.db")
        conn = make_vec_conn(db_path)
        first = _insert_branch_with_messages(conn)
        second = _insert_branch_with_messages(conn)
        other = sqlite3.connect(db_path, timeout=0)
        seen = []

        def embed(texts: list[str], **kwargs) -> list[list[float]]:
            seen.append(_branch_embedding_version(other, first))
            other.execute("BEGIN IMMEDIATE")
            other.commit()
            return [_FIXED_VEC] * len(texts)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=embed),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            assert run() == EXIT_OK

        assert seen[1] == EMBEDDING_VERSION
        assert _branch_embedding_version(other, second) == EMBEDDING_VERSION
        other.close()


# Resume processes only remaining; heal clause for missing chunk_vecs


@_VEC_SKIP
class TestBackfillResume:
    def test_resume_skips_already_done(self):
        """Second run does not re-embed already-done branches."""
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn) for _ in range(4)]

        call_count = [0]

        def counting_embed(texts: list[str], **kwargs) -> list[list[float]]:
            call_count[0] += len(texts)
            return [_FIXED_VEC[:]] * len(texts)

        def _run_counting(conn):
            with (
                patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
                patch("ccrecall.embed_ops.embed_batch", side_effect=counting_embed),
                patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
                patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
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


# Cap-tokens-based selection (draft-quality upgrade candidates)


@_VEC_SKIP
class TestBuildSelectionCapTokens:
    """build_selection selects branches with draft-quality chunks (cap_tokens <
    MODEL_TOKEN_LIMIT) even when the branch's embedding_version watermark is
    otherwise current."""

    def _insert_current_branch_with_chunk(self, conn: sqlite3.Connection, cap_tokens: int | None) -> int:
        """Insert a branch already at the current watermark, with one chunk (+
        vector) at the given cap_tokens. Every other build_selection clause
        (version, model, heal) is satisfied, so only the new cap-tokens clause
        can explain a match."""
        branch_id = _insert_branch_with_messages(conn)
        conn.execute(
            "UPDATE branches SET embedding_version = ?, embedding_model = ? WHERE id = ?",
            (EMBEDDING_VERSION, EMBEDDING_MODEL, branch_id),
        )
        conn.execute(
            """
            INSERT INTO chunks (
                branch_id, exchange_index, content_hash, cap_tokens,
                embedding_version, embedding_model
            ) VALUES (?, 0, 'hash0', ?, ?, ?)
            """,
            (branch_id, cap_tokens, EMBEDDING_VERSION, EMBEDDING_MODEL),
        )
        chunk_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        upsert_chunk_vec(conn.cursor(), chunk_id, _FIXED_VEC)
        conn.commit()
        return branch_id

    def _is_selected(self, conn: sqlite3.Connection, branch_id: int) -> bool:
        where, params = build_selection(days=None)
        rows = conn.execute(f"SELECT id FROM branches {where}", params).fetchall()
        return branch_id in {row[0] for row in rows}

    def test_draft_quality_chunk_makes_branch_eligible(self):
        """A branch whose only chunk has cap_tokens=4096 is selected even
        though its embedding_version/embedding_model watermark is current."""
        conn = make_vec_conn()
        bid = self._insert_current_branch_with_chunk(conn, cap_tokens=4096)

        assert self._is_selected(conn, bid)

    def test_full_quality_chunk_not_selected(self):
        """A branch whose only chunk has cap_tokens=NULL (full quality) and a
        current watermark is not selected."""
        conn = make_vec_conn()
        bid = self._insert_current_branch_with_chunk(conn, cap_tokens=None)

        assert not self._is_selected(conn, bid)

    def test_upgrade_removes_branch_from_selection(self):
        """After backfill re-embeds a draft chunk at full quality
        (cap_tokens -> NULL), the branch is no longer selected."""
        conn = make_vec_conn()
        bid = self._insert_current_branch_with_chunk(conn, cap_tokens=4096)
        assert self._is_selected(conn, bid)

        conn.execute("UPDATE chunks SET cap_tokens = NULL WHERE branch_id = ?", (bid,))
        conn.commit()

        assert not self._is_selected(conn, bid)


# Forward-only pass: each branch is attempted at most once per run


def _run_backfill_with_embed_patch(conn: sqlite3.Connection, embed_side_effect, **run_kwargs) -> tuple[int, list[int]]:
    """Run run() with embed_branch_chunks replaced, so a test decides each branch's outcome."""
    with (
        patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
        patch("ccrecall.hooks.backfill_embeddings.embed_branch_chunks", side_effect=embed_side_effect) as mock_embed,
        patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kwargs: [_FIXED_VEC] * len(texts)),
        patch("ccrecall.hooks.backfill_embeddings.embed_batch", return_value=[_FIXED_VEC]),
        patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
        patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
        patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
    ):
        code = run(**run_kwargs)
    return code, [call.args[1] for call in mock_embed.call_args_list]


@_VEC_SKIP
class TestBackfillForwardOnlyPass:
    def test_unfinished_branch_is_attempted_once_and_run_completes(self):
        """A branch left eligible is not re-selected in the same run, and the run
        ends normally instead of tripping the stuck guard."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)

        code, attempted = _run_backfill_with_embed_patch(conn, lambda *a, **k: EmbedOutcome(embedded=0, stamped=False))

        assert code == EXIT_OK
        assert attempted == [bid]

    def test_unfinished_branches_do_not_repeat_across_batches(self):
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn) for _ in range(BATCH_SIZE + 2)]

        code, attempted = _run_backfill_with_embed_patch(conn, lambda *a, **k: EmbedOutcome(embedded=0, stamped=False))

        assert code == EXIT_OK
        assert attempted == ids

    def test_unfinished_branch_is_retried_by_the_next_run(self):
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)
        _run_backfill_with_embed_patch(conn, lambda *a, **k: EmbedOutcome(embedded=0, stamped=False))

        _run_backfill_with_stub(conn)

        assert _branch_embedding_version(conn, bid) == EMBEDDING_VERSION

    def test_completion_reports_branches_not_updated(self, capsys):
        conn = make_vec_conn()
        _insert_branch_with_messages(conn)
        finished = _insert_branch_with_messages(conn)

        def embed(conn_arg, branch_id, **kwargs):
            if branch_id == finished:
                return embed_branch_chunks(conn_arg, branch_id, **kwargs)
            return EmbedOutcome(embedded=0, stamped=False)

        _run_backfill_with_embed_patch(conn, embed, json_mode=True)

        report = json.loads(capsys.readouterr().out)
        assert report["embedded"] == 1
        assert report["not_updated"] == 1


@_VEC_SKIP
class TestBackfillLockTimeout:
    def test_lock_timeout_costs_one_branch_and_run_continues(self):
        conn = make_vec_conn()
        contended = _insert_branch_with_messages(conn)
        later = _insert_branch_with_messages(conn)

        def embed(conn_arg, branch_id, **kwargs):
            if branch_id == contended:
                raise locked_error()
            return embed_branch_chunks(conn_arg, branch_id, **kwargs)

        code, attempted = _run_backfill_with_embed_patch(conn, embed)

        assert code == EXIT_OK
        assert attempted == [contended, later]
        assert _branch_embedding_version(conn, contended) != CONTENT_ERROR_VERSION
        assert _branch_embedding_version(conn, later) == EMBEDDING_VERSION

    def test_lock_timeout_on_sentinel_leaves_row_unmarked_and_run_continues(self):
        conn = make_vec_conn()
        bad = _insert_branch_with_messages(conn)
        later = _insert_branch_with_messages(conn)

        def embed(conn_arg, branch_id, **kwargs):
            if branch_id == bad:
                raise ValueError("simulated tokenizer overflow")
            return embed_branch_chunks(conn_arg, branch_id, **kwargs)

        with patch("ccrecall.hooks.backfill_embeddings.commit_single_write", side_effect=locked_error()):
            code, _attempted = _run_backfill_with_embed_patch(conn, embed)

        assert code == EXIT_OK
        assert _branch_embedding_version(conn, bad) != CONTENT_ERROR_VERSION
        assert _branch_embedding_version(conn, later) == EMBEDDING_VERSION

    def test_consecutive_lock_timeouts_abort_the_run(self):
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn) for _ in range(5)]

        code, attempted = _run_backfill_with_embed_patch(conn, locked_error())

        assert code == EXIT_ABORT
        assert attempted == ids[:3]

    def test_a_finished_branch_resets_the_lock_timeout_count(self):
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn) for _ in range(5)]

        def embed(conn_arg, branch_id, **kwargs):
            if branch_id == ids[2]:
                return embed_branch_chunks(conn_arg, branch_id, **kwargs)
            raise locked_error()

        code, attempted = _run_backfill_with_embed_patch(conn, embed)

        assert code == EXIT_OK
        assert attempted == ids

    def test_other_database_errors_still_abort(self):
        conn = make_vec_conn()
        _insert_branch_with_messages(conn)
        later = _insert_branch_with_messages(conn)

        code, attempted = _run_backfill_with_embed_patch(conn, sqlite3.OperationalError("disk I/O error"))

        assert code == EXIT_ABORT
        assert later not in attempted


@_VEC_SKIP
class TestBackfillReplacesStaleChunks:
    """Stale chunks stay until their replacement commits, so a failed embed loses nothing."""

    def _branch_with_stale_chunk(self, conn: sqlite3.Connection) -> tuple[int, int]:
        bid = _insert_branch_with_messages(conn)
        _run_backfill_with_stub(conn)
        conn.execute("UPDATE chunks SET embedding_version = 0 WHERE branch_id = ?", (bid,))
        conn.execute("UPDATE branches SET embedding_version = 0 WHERE id = ?", (bid,))
        conn.commit()
        return bid, conn.execute("SELECT id FROM chunks WHERE branch_id = ?", (bid,)).fetchone()[0]

    def test_failed_embed_keeps_stale_chunk_and_vector(self):
        conn = make_vec_conn()
        bid, chunk_id = self._branch_with_stale_chunk(conn)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=OSError("disk I/O error")),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            assert run() == EXIT_ABORT

        assert [row[0] for row in _chunks_for_branch(conn, bid)] == [chunk_id]
        assert _branch_has_chunk_vecs(conn, bid)

    def test_successful_embed_replaces_stale_chunk(self):
        conn = make_vec_conn()
        bid, _chunk_id = self._branch_with_stale_chunk(conn)

        _run_backfill_with_stub(conn)

        assert [row[1] for row in _chunks_for_branch(conn, bid)] == [EMBEDDING_VERSION]
        assert _branch_has_chunk_vecs(conn, bid)


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


def _stamp_watermark(conn: sqlite3.Connection, branch_id: int) -> None:
    conn.execute(
        "UPDATE branches SET embedding_version = ?, embedding_model = ? WHERE id = ?",
        (EMBEDDING_VERSION, EMBEDDING_MODEL, branch_id),
    )
    conn.commit()


def _embedded_exchange_indices(conn: sqlite3.Connection, branch_id: int) -> set[int]:
    """Exchange indices of this branch that have a chunk row and a vector."""
    rows = conn.execute(
        """
        SELECT c.exchange_index FROM chunks c
        JOIN chunk_vec v ON v.chunk_id = c.id
        WHERE c.branch_id = ?
        """,
        (branch_id,),
    ).fetchall()
    return {row[0] for row in rows}


@_VEC_SKIP
class TestBackfillHealsStampedBranches:
    """A branch stamped as embedded but missing exchanges has no chunk row for
    build_selection to find, so the backfill clears its watermark at startup."""

    def test_stamped_branch_with_no_chunks_gets_embedded(self):
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn, num_exchanges=2)
        _stamp_watermark(conn, bid)

        _run_backfill_with_stub(conn)

        assert _embedded_exchange_indices(conn, bid) == {0, 1}
        assert _branch_embedding_version(conn, bid) == EMBEDDING_VERSION
        assert find_stamped_branches_missing_chunks(conn.cursor()) == {}

    def test_stamped_branch_missing_newest_exchange_gets_embedded(self):
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn, num_exchanges=2)
        _run_backfill_with_stub(conn)
        conn.execute("DELETE FROM chunks WHERE branch_id = ? AND exchange_index = 1", (bid,))
        conn.commit()

        _run_backfill_with_stub(conn)

        assert _embedded_exchange_indices(conn, bid) == {0, 1}

    def test_fully_embedded_branch_is_not_re_embedded(self):
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn, num_exchanges=2)
        _run_backfill_with_stub(conn)
        chunk_ids = {row[0] for row in _chunks_for_branch(conn, bid)}

        _run_backfill_with_stub(conn)

        assert {row[0] for row in _chunks_for_branch(conn, bid)} == chunk_ids
        assert _branch_embedding_version(conn, bid) == EMBEDDING_VERSION

    def test_content_error_branch_is_not_retried(self):
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)
        conn.execute("UPDATE branches SET embedding_version = ? WHERE id = ?", (CONTENT_ERROR_VERSION, bid))
        conn.commit()

        _run_backfill_with_stub(conn)

        assert _branch_embedding_version(conn, bid) == CONTENT_ERROR_VERSION
        assert _chunk_count(conn) == 0

    def test_heal_survives_a_run_that_aborts_mid_batch(self):
        """An abort must not undo the heal, or a branch that keeps aborting
        would go back to claiming it's fully embedded."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)
        _stamp_watermark(conn, bid)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=sqlite3.OperationalError("disk I/O error")),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            assert run() == EXIT_ABORT

        assert _branch_embedding_version(conn, bid) != EMBEDDING_VERSION


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
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
        ):
            run()

        for bid in ids:
            ev = _branch_embedding_version(conn, bid)
            assert ev != CONTENT_ERROR_VERSION, f"branch {bid} should not be marked -1"
        assert _chunk_count(conn) == 0

    def test_single_bad_embed_marks_only_itself(self):
        """One branch whose embed_batch raises marks only that branch -1; rest succeed."""
        conn = make_vec_conn()
        good_id1 = _insert_branch_with_messages(conn)
        bad_id = _insert_branch_with_messages(conn)
        good_id2 = _insert_branch_with_messages(conn)

        # Raise on the second call (bad_id was inserted second)
        call_no = [0]

        def counting_embed(texts: list[str], **kwargs) -> list[list[float]]:
            call_no[0] += 1
            if call_no[0] == 2:  # second branch's embed raises
                raise ValueError("simulated tokenizer overflow")
            return [_FIXED_VEC] * len(texts)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=counting_embed),
            patch("ccrecall.hooks.backfill_embeddings.embed_batch", return_value=[_FIXED_VEC]),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
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
        """OSError from embed_batch (infra failure) → zero rows marked -1, all stay eligible."""
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn) for _ in range(3)]

        def infra_fail(texts: list[str], **kwargs) -> list[list[float]]:
            raise OSError("disk I/O error loading model weights")

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=infra_fail),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            run()

        for bid in ids:
            ev = _branch_embedding_version(conn, bid)
            assert ev != CONTENT_ERROR_VERSION, f"branch {bid} should not be marked -1 on infra failure"
        assert _chunk_count(conn) == 0

    def test_unanticipated_exception_type_marks_sentinel_not_wedge(self):
        """An exception type outside any hardcoded content-error allow-list still
        marks the sentinel instead of falling through to the infra-abort path.

        Regression (#201, mirroring #178): the old taxonomy allow-listed only
        (ValueError, OverflowError, UnicodeError) as content errors, so any
        other exception type (e.g. a RuntimeError from an ONNX inference
        failure specific to one branch's text) fell through to the infra
        handler, left the row unmarked, and — since this backfill is run
        unattended on a recurring schedule — wedged that row (and every
        branch behind it) forever.
        """
        conn = make_vec_conn()
        bad_id = _insert_branch_with_messages(conn)
        good_id = _insert_branch_with_messages(conn)

        call_no = [0]

        def counting_embed(texts: list[str], **kwargs) -> list[list[float]]:
            call_no[0] += 1
            if call_no[0] == 1:
                raise RuntimeError("unexpected inference failure for this branch's text")
            return [_FIXED_VEC] * len(texts)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=counting_embed),
            patch("ccrecall.hooks.backfill_embeddings.embed_batch", return_value=[_FIXED_VEC]),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            run()

        assert _branch_embedding_version(conn, bad_id) == CONTENT_ERROR_VERSION
        assert _branch_embedding_version(conn, good_id) == EMBEDDING_VERSION

    def test_model_session_failure_aborts_without_marking(self):
        """When the model health probe also fails after a branch's embed
        raises, the failure is a model/session break, not a per-row content
        error: the failing branch must NOT be marked CONTENT_ERROR_VERSION,
        the run must abort (EXIT_ABORT), and branches later in the batch
        must not be processed at all.
        """
        conn = make_vec_conn()
        bad_id = _insert_branch_with_messages(conn)
        later_id = _insert_branch_with_messages(conn)

        def failing_embed(texts: list[str], **kwargs) -> list[list[float]]:
            raise RuntimeError("model session broke mid-inference")

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=failing_embed),
            patch(
                "ccrecall.hooks.backfill_embeddings.embed_batch",
                side_effect=RuntimeError("model session broke mid-inference"),
            ),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            exit_code = run()

        assert exit_code == EXIT_ABORT
        # The failing branch is untouched — not marked CONTENT_ERROR_VERSION,
        # still eligible for retry on the next run.
        bad_ev = _branch_embedding_version(conn, bad_id)
        assert bad_ev != CONTENT_ERROR_VERSION
        assert bad_ev != EMBEDDING_VERSION
        # The batch aborted before reaching the later branch — proves the
        # run stopped rather than continuing past the broken model.
        later_ev = _branch_embedding_version(conn, later_id)
        assert later_ev != CONTENT_ERROR_VERSION
        assert later_ev != EMBEDDING_VERSION
        assert not _branch_has_chunk_vecs(conn, bad_id)
        assert not _branch_has_chunk_vecs(conn, later_id)

    def test_sentinel_row_not_reprocessed(self):
        """A branch with embedding_version=-1 is excluded from the selection predicate."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)
        conn.execute("UPDATE branches SET embedding_version = -1 WHERE id = ?", (bid,))
        conn.commit()

        call_count = [0]

        def counting_embed(texts: list[str], **kwargs) -> list[list[float]]:
            call_count[0] += len(texts)
            return [_FIXED_VEC] * len(texts)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=counting_embed),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
        ):
            run()

        assert call_count[0] == 0

    def test_content_error_vs_batch_abort(self):
        """sqlite3.Error from fetch_branch_messages is a batch-abort (EXIT_ABORT),
        NOT a per-row content-error sentinel — the two must not be conflated."""
        conn = make_vec_conn()
        bid = _insert_branch_with_messages(conn)

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            # Simulate an infra failure during message fetch
            patch(
                "ccrecall.embed_ops.fetch_branch_messages",
                side_effect=sqlite3.OperationalError("disk I/O error"),
            ),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
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

    def test_limit_counts_attempts_not_stamped_branches(self):
        """--limit is a ceiling on work, so branches left unfinished use it up too."""
        conn = make_vec_conn()
        ids = [_insert_branch_with_messages(conn) for _ in range(5)]

        code, attempted = _run_backfill_with_embed_patch(
            conn, lambda *a, **k: EmbedOutcome(embedded=0, stamped=False), limit=2
        )

        assert code == EXIT_OK
        assert attempted == ids[:2]

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


def test_run_forwards_custom_db_path_to_load_settings():
    """A custom db= argument reaches load_settings_for_db unchanged — regression
    guard against silently routing work to the default database."""
    custom_path = Path("/tmp/custom-embed.db")
    with (
        patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=False),
        patch("ccrecall.hooks.backfill_embeddings.record_embedding_failure"),
        patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}) as mock_load,
    ):
        run(db=custom_path)
    mock_load.assert_called_once_with(custom_path)


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
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kwargs: [_FIXED_VEC] * len(texts)),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
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
    """backfill_embeddings.run() records capability failures and clears on success."""

    def test_model_unavailable_records_reason(self, tmp_path):
        """model_available() → False inside run() writes 'model_unavailable' to sidecar."""
        sidecar = tmp_path / "embedding-status.json"

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=False),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
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
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
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
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kwargs: [_FIXED_VEC] * len(texts)),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.time.sleep"),
            patch(
                "ccrecall.hooks.backfill_embeddings.clear_embedding_failure",
                side_effect=patched_clear(sidecar),
            ),
        ):
            run()

        assert not sidecar.exists(), "sidecar must be absent (cleared) after a successful embedding run"


class TestBackfillJobLock:
    """Self-concurrency guard for the manual embeddings backfill.

    The CLI takes the job lock before run() and releases it on exit, so a second
    instance (a systemd timer firing while a previous run is alive, or a
    double-invoked manual run) skips instead of doubling the onnxruntime memory
    peak.
    """

    def test_cli_skips_when_another_instance_holds_the_lock(self, capsys, hold_job_lock):
        holder = hold_job_lock(JOB_KEY)
        with (
            patch("ccrecall.cli.commands.backfill_embeddings_mod.run") as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_backfill_embeddings()

        assert exc_info.value.code == EXIT_OK, "skip is not a failure — a scheduler shouldn't see non-zero"
        mock_run.assert_not_called()
        assert "already running" in capsys.readouterr().err
        state = job_lock.probe(JOB_KEY)
        assert state is not None
        assert (state.running, state.pid) == (True, holder)

    def test_cli_holds_the_lock_with_its_scope_while_running_then_releases(self):
        seen: list[job_lock.JobState | None] = []
        with (
            patch(
                "ccrecall.cli.commands.backfill_embeddings_mod.run",
                side_effect=lambda **_kw: seen.append(job_lock.probe(JOB_KEY)) or EXIT_OK,
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_backfill_embeddings(days=14)

        assert exc_info.value.code == EXIT_OK
        assert seen[0] is not None
        assert (seen[0].running, seen[0].pid) == (True, os.getpid())
        assert seen[0].scope == {"days": 14, "limit": None, "db": str(config.DEFAULT_DB_PATH.resolve())}
        assert job_lock.probe(JOB_KEY) is None

    @pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt()])
    def test_cli_releases_on_an_exception_or_ctrl_c(self, exc):
        with (
            patch("ccrecall.cli.commands.backfill_embeddings_mod.run", side_effect=exc),
            pytest.raises(type(exc)),
        ):
            cmd_backfill_embeddings()

        assert job_lock.probe(JOB_KEY) is None


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
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kwargs: [_FIXED_VEC] * len(texts)),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
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
            patch("ccrecall.embed_ops.embed_batch", side_effect=lambda texts, **kwargs: [_FIXED_VEC] * len(texts)),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
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

        def always_fail(texts: list[str], **kwargs) -> list[list[float]]:
            raise ValueError("simulated tokenizer overflow")

        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=True),
            patch("ccrecall.embed_ops.embed_batch", side_effect=always_fail),
            patch("ccrecall.hooks.backfill_embeddings.get_connection", return_value=_NoCloseConn(conn)),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
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


def _backfill_runs() -> dict:
    # load_settings_for_db is patched to {}, so the run records against the default
    # database, whose sidecars conftest redirects into the isolated runtime dir.
    return run_history.read_runs(config.DEFAULT_DB_PATH).get("backfill", {})


@_VEC_SKIP
class TestBackfillRunRecording:
    """Every backfill records its outcome; only an unscoped success advances last_ok (#234)."""

    def test_unscoped_success_advances_last_ok(self):
        conn = make_vec_conn()
        _insert_branch_with_messages(conn)

        _run_backfill_with_stub(conn)

        runs = _backfill_runs()
        assert runs["last"]["outcome"] == "ok"
        assert runs["last_ok"] == runs["last"]

    @pytest.mark.parametrize("scope", [{"limit": 1}, {"days": 7}])
    def test_scoped_success_never_advances_last_ok(self, scope):
        conn = make_vec_conn()
        _insert_branch_with_messages(conn)

        _run_backfill_with_stub(conn, **scope)

        runs = _backfill_runs()
        assert runs["last"]["outcome"] == "ok"
        assert runs["last_ok"] is None

    def test_a_lock_timeout_records_partial_database_busy(self):
        conn = make_vec_conn()
        contended = _insert_branch_with_messages(conn)
        _insert_branch_with_messages(conn)

        def embed(conn_arg, branch_id, **kwargs):
            if branch_id == contended:
                raise locked_error()
            return embed_branch_chunks(conn_arg, branch_id, **kwargs)

        _run_backfill_with_embed_patch(conn, embed)

        runs = _backfill_runs()
        assert (runs["last"]["outcome"], runs["last"]["reason"]) == ("partial", "database_busy")
        assert runs["last_ok"] is None

    def test_a_content_error_still_records_ok(self):
        conn = make_vec_conn()
        bad = _insert_branch_with_messages(conn)

        def embed(conn_arg, branch_id, **kwargs):
            if branch_id == bad:
                raise ValueError("simulated tokenizer overflow")
            return embed_branch_chunks(conn_arg, branch_id, **kwargs)

        _run_backfill_with_embed_patch(conn, embed)

        assert _backfill_runs()["last"]["outcome"] == "ok"

    def test_repeated_lock_timeouts_record_failed_database_busy(self):
        conn = make_vec_conn()
        for _ in range(4):
            _insert_branch_with_messages(conn)

        _run_backfill_with_embed_patch(conn, locked_error())

        last = _backfill_runs()["last"]
        assert (last["outcome"], last["reason"]) == ("failed", "database_busy")

    def test_other_database_errors_record_failed_error(self):
        conn = make_vec_conn()
        _insert_branch_with_messages(conn)

        _run_backfill_with_embed_patch(conn, sqlite3.OperationalError("disk I/O error"))

        last = _backfill_runs()["last"]
        assert (last["outcome"], last["reason"]) == ("failed", "error")

    def test_model_failing_its_probe_records_model_unavailable(self):
        conn = make_vec_conn()
        _insert_branch_with_messages(conn)

        with patch("ccrecall.hooks.backfill_embeddings._model_still_healthy", return_value=False):
            _run_backfill_with_embed_patch(conn, RuntimeError("model session broke"))

        last = _backfill_runs()["last"]
        assert (last["outcome"], last["reason"]) == ("failed", "model_unavailable")

    def test_model_unavailable_at_start_is_recorded(self):
        with (
            patch("ccrecall.hooks.backfill_embeddings.model_available", return_value=False),
            patch("ccrecall.hooks.backfill_embeddings.load_settings_for_db", return_value={}),
            patch("ccrecall.hooks.backfill_embeddings.record_embedding_failure"),
        ):
            assert run() == EXIT_ABORT

        last = _backfill_runs()["last"]
        assert (last["outcome"], last["reason"]) == ("failed", "model_unavailable")
