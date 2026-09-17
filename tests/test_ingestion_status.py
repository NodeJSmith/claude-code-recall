"""Tests for transcript-vs-DB ingestion diagnostics."""

import os
from pathlib import Path
from unittest.mock import patch

from conftest import age_past_grace_window, delete_message, make_jsonl_entry, write_four_turns, write_jsonl

from ccrecall import ingestion_status, parsing
from ccrecall.import_log_ops import import_log_source_index
from ccrecall.ingestion_status import (
    _db_coverage_fingerprint,
    find_repairable_sessions,
    reclassify_session,
    summarize_ingestion,
)


def _link_messages_to_branch(memory_db, session_id: int, branch_id: int, uuids: list[str]) -> None:
    """Link each message identified by uuid (for this session) to branch_id."""
    for uuid in uuids:
        message_id = memory_db.execute(
            "SELECT id FROM messages WHERE session_id = ? AND uuid = ?", (session_id, uuid)
        ).fetchone()[0]
        memory_db.execute(
            "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
            (branch_id, message_id),
        )


def _link_active_branch(memory_db, session_id: int, leaf_uuid: str, uuids: list[str]) -> int:
    """Create one new active branch for session_id and link it to each
    message identified by uuid. Returns the new branch's id."""
    memory_db.execute(
        "INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, ?, 1)",
        (session_id, leaf_uuid),
    )
    branch_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    _link_messages_to_branch(memory_db, session_id, branch_id, uuids)
    return branch_id


def _seed_session(memory_db, filepath: Path, db_uuids: list[str], *, link_active_branch: bool = True) -> None:
    """Insert a session with the given message UUIDs.

    By default all seeded messages are linked to one new active branch —
    matching real ingestion's invariant that a message row is reachable from
    the active branch, not just present in `messages`. classify_sessions()
    checks active-branch linkage (not bare row existence), so a message
    inserted here but left unlinked would silently count as missing rather
    than present.

    Pass link_active_branch=False for tests that construct their own branch
    topology (e.g. partial linkage, multiple branches) to avoid a conflicting
    auto-created branch.
    """
    session_uuid = filepath.stem.removeprefix("agent-")
    memory_db.execute("INSERT INTO sessions (uuid) VALUES (?)", (session_uuid,))
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for uuid in db_uuids:
        memory_db.execute(
            "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, ?, 'user', 'x', '')",
            (session_id, uuid),
        )
    if link_active_branch and db_uuids:
        _link_active_branch(memory_db, session_id, db_uuids[-1], db_uuids)
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', ?)",
        (str(filepath), len(db_uuids)),
    )
    memory_db.commit()


def _insert_import_log(memory_db, filepath: Path, message_count: int = 0) -> None:
    memory_db.execute(
        "INSERT INTO import_log (file_path, file_hash, messages_imported) VALUES (?, 'hash', ?)",
        (str(filepath), message_count),
    )
    memory_db.commit()


def _cache_row(memory_db, session_uuid: str) -> tuple[str, str, str, str] | None:
    return memory_db.execute(
        """
        SELECT session_uuid, source_fingerprint, db_coverage_fingerprint, checked_at
        FROM ingestion_check_cache
        WHERE session_uuid = ?
        """,
        (session_uuid,),
    ).fetchone()


def test_pending_tail_for_recent_contiguous_suffix(memory_db, tmp_path):
    filepath = tmp_path / "sess-tail.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1"])

    status = summarize_ingestion(memory_db)

    assert status["pending_tail_sessions"] == 1
    assert status["pending_tail_turns"] == 2
    assert status["ingestion_gap_sessions"] == 0


def test_stale_tail_for_old_contiguous_suffix(memory_db, tmp_path):
    filepath = tmp_path / "sess-stale.jsonl"
    write_four_turns(filepath)
    age_past_grace_window(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1"])

    status = summarize_ingestion(memory_db)

    assert status["stale_tail_sessions"] == 1
    assert status["stale_tail_turns"] == 2
    assert status["pending_tail_sessions"] == 0


def test_middle_missing_uuid_is_ingestion_gap(memory_db, tmp_path):
    filepath = tmp_path / "sess-gap.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "u2", "a2"])

    status = summarize_ingestion(memory_db)

    assert status["ingestion_gap_sessions"] == 1
    assert status["ingestion_gap_turns"] == 1
    assert status["pending_tail_sessions"] == 0


def test_missing_source_when_no_transcript_survives(memory_db, tmp_path):
    filepath = tmp_path / "sess-gone.jsonl"
    _seed_session(memory_db, filepath, ["u1"])

    status = summarize_ingestion(memory_db)

    assert status["missing_source_sessions"] == 1
    assert status["sessions_checked"] == 1


def test_complete_session_is_ok(memory_db, tmp_path):
    filepath = tmp_path / "sess-ok.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"])

    status = summarize_ingestion(memory_db)

    assert status["ok_sessions"] == 1
    assert status["pending_tail_sessions"] == 0
    assert status["ingestion_gap_sessions"] == 0


def test_ok_session_records_cache_and_unchanged_second_run_skips_parsing(memory_db, tmp_path):
    filepath = tmp_path / "sess-cache.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"])

    first = summarize_ingestion(memory_db)

    cached = _cache_row(memory_db, "sess-cache")
    assert first["sessions_checked"] == 1
    assert first["ok_sessions"] == 1
    assert cached is not None

    with patch(
        "ccrecall.ingestion_status.parse_all_with_uuids", side_effect=AssertionError("cache hit should skip parsing")
    ):
        second = summarize_ingestion(memory_db)

    assert second["sessions_checked"] == 1
    assert second["ok_sessions"] == 1
    assert _cache_row(memory_db, "sess-cache") == cached


def test_transcript_change_invalidates_cache_and_reparses(memory_db, tmp_path):
    filepath = tmp_path / "sess-cache-change.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"])

    summarize_ingestion(memory_db)
    first_cache = _cache_row(memory_db, "sess-cache-change")
    assert first_cache is not None

    write_jsonl(
        filepath,
        [
            make_jsonl_entry("u1", None, "2026-01-01T10:00:00Z", "user", "first"),
            make_jsonl_entry("a1", "u1", "2026-01-01T10:00:01Z", "assistant", "answer"),
            make_jsonl_entry("u2", "a1", "2026-01-01T10:00:02Z", "user", "second updated"),
            make_jsonl_entry("a2", "u2", "2026-01-01T10:00:03Z", "assistant", "answer"),
        ],
    )

    with patch("ccrecall.ingestion_status.parse_all_with_uuids", wraps=parsing.parse_all_with_uuids) as parse_all:
        status = summarize_ingestion(memory_db)

    assert parse_all.call_count == 1
    assert status["ok_sessions"] == 1
    assert _cache_row(memory_db, "sess-cache-change")[1] != first_cache[1]


def test_mtime_only_change_invalidates_cache_and_reparses(memory_db, tmp_path):
    filepath = tmp_path / "sess-cache-mtime.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"])

    summarize_ingestion(memory_db)
    first_cache = _cache_row(memory_db, "sess-cache-mtime")
    assert first_cache is not None

    stat = filepath.stat()
    os.utime(filepath, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))

    with patch("ccrecall.ingestion_status.parse_all_with_uuids", wraps=parsing.parse_all_with_uuids) as parse_all:
        status = summarize_ingestion(memory_db)

    assert parse_all.call_count == 1
    assert status["ok_sessions"] == 1
    assert _cache_row(memory_db, "sess-cache-mtime")[1] != first_cache[1]


def test_deleted_db_message_invalidates_ok_cache_and_reports_gap(memory_db, tmp_path):
    filepath = tmp_path / "sess-cache-db-gap.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"])

    first = summarize_ingestion(memory_db)

    assert first["ok_sessions"] == 1
    first_cache = _cache_row(memory_db, "sess-cache-db-gap")
    assert first_cache is not None

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-cache-db-gap",)).fetchone()[0]
    delete_message(memory_db, session_id, "a1")
    memory_db.commit()

    with patch("ccrecall.ingestion_status.parse_all_with_uuids", wraps=parsing.parse_all_with_uuids) as parse_all:
        second = summarize_ingestion(memory_db)

    assert parse_all.call_count == 1
    assert second["ok_sessions"] == 0
    assert second["ingestion_gap_sessions"] == 1
    assert second["ingestion_gap_turns"] == 1
    assert _cache_row(memory_db, "sess-cache-db-gap") == first_cache


def test_changed_db_uuid_membership_invalidates_ok_cache_and_reports_gap(memory_db, tmp_path):
    filepath = tmp_path / "sess-cache-db-membership.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"])

    first = summarize_ingestion(memory_db)

    assert first["ok_sessions"] == 1
    first_cache = _cache_row(memory_db, "sess-cache-db-membership")
    assert first_cache is not None

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-cache-db-membership",)).fetchone()[
        0
    ]
    delete_message(memory_db, session_id, "a1")
    memory_db.execute(
        "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, 'bogus', 'user', 'x', '')",
        (session_id,),
    )
    memory_db.commit()

    with patch("ccrecall.ingestion_status.parse_all_with_uuids", wraps=parsing.parse_all_with_uuids) as parse_all:
        second = summarize_ingestion(memory_db)

    assert parse_all.call_count == 1
    assert second["ok_sessions"] == 0
    assert second["ingestion_gap_sessions"] == 1
    assert second["ingestion_gap_turns"] == 1
    assert _cache_row(memory_db, "sess-cache-db-membership") == first_cache


def test_present_but_unlinked_message_is_not_counted_as_ok(memory_db, tmp_path):
    """A message row that survives in `messages` but whose branch_messages
    link to the active branch was dropped must classify as a real gap, not
    ok — a row-existence-only check (the codex-flagged bug) is blind to this
    exact scenario, the same class of corruption the coverage fingerprint
    exists to catch."""
    filepath = tmp_path / "sess-unlinked-message.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"], link_active_branch=False)

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-unlinked-message",)).fetchone()[0]
    memory_db.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'a2', 1)", (session_id,))
    branch_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Link every expected message EXCEPT u1 — its row still exists in
    # `messages`, but it's unreachable from the active branch, exactly like a
    # dropped branch_messages link would leave it.
    for uuid in ("a1", "u2", "a2"):
        message_id = memory_db.execute(
            "SELECT id FROM messages WHERE session_id = ? AND uuid = ?", (session_id, uuid)
        ).fetchone()[0]
        memory_db.execute(
            "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
            (branch_id, message_id),
        )
    memory_db.commit()

    status = summarize_ingestion(memory_db)

    assert status["ok_sessions"] == 0
    assert status["ingestion_gap_sessions"] == 1
    assert status["ingestion_gap_turns"] == 1


def test_same_count_link_substitution_changes_db_coverage_fingerprint(memory_db, tmp_path):
    """A branch losing one branch_messages link and gaining a different one
    (net per-branch link count unchanged) must still change the fingerprint —
    a bare COUNT(*) per branch can't see this, only membership can."""
    filepath = tmp_path / "sess-link-substitution.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"], link_active_branch=False)

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-link-substitution",)).fetchone()[0]
    memory_db.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'a2', 1)", (session_id,))
    branch_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _message_id(uuid: str) -> int:
        return memory_db.execute(
            "SELECT id FROM messages WHERE session_id = ? AND uuid = ?", (session_id, uuid)
        ).fetchone()[0]

    memory_db.execute(
        "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
        (branch_id, _message_id("u1")),
    )
    memory_db.commit()

    cursor = memory_db.cursor()
    before = _db_coverage_fingerprint(cursor, session_id)

    memory_db.execute(
        "DELETE FROM branch_messages WHERE branch_id = ? AND message_id = ?",
        (branch_id, _message_id("u1")),
    )
    memory_db.execute(
        "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
        (branch_id, _message_id("a1")),
    )
    memory_db.commit()

    after = _db_coverage_fingerprint(cursor, session_id)

    assert before != after


def test_new_zero_link_active_branch_changes_db_coverage_fingerprint(memory_db, tmp_path):
    """A newly created active branch with no branch_messages links yet must
    still change the fingerprint. link_part's query is an INNER JOIN starting
    from branch_messages, so a zero-link branch contributes no row there —
    indistinguishable from the branch not existing at all. branch_part (a
    plain active-branches SELECT) is what makes the branch's existence itself
    visible, independent of whether it has any links."""
    filepath = tmp_path / "sess-new-zero-link-branch.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"], link_active_branch=False)

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-new-zero-link-branch",)).fetchone()[
        0
    ]

    cursor = memory_db.cursor()
    before = _db_coverage_fingerprint(cursor, session_id)

    # A new active branch appears with no branch_messages links at all — no
    # message rows change, so message_part alone can't see this either.
    memory_db.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'a2', 1)", (session_id,))
    memory_db.commit()

    after = _db_coverage_fingerprint(cursor, session_id)

    assert before != after


def test_branch_link_substitution_invalidates_ok_cache(memory_db, tmp_path):
    """End-to-end version of the same-count substitution: after a link swap
    that leaves the branch's total link count unchanged but drops a message
    that's actually on the expected active path, the session must not stay
    cached as ok — the corrupted state must be reclassified and reported as
    a real gap on reparse, not silently trusted forever."""
    filepath = tmp_path / "sess-cache-link-substitution.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1", "u2", "a2"], link_active_branch=False)

    session_id = memory_db.execute(
        "SELECT id FROM sessions WHERE uuid = ?", ("sess-cache-link-substitution",)
    ).fetchone()[0]

    # x1: a message row that exists for this session but isn't on the
    # expected active path (e.g. it belongs to an inactive/historical
    # branch) — present in `messages`, but must never count toward active
    # coverage.
    memory_db.execute(
        "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, 'x1', 'user', 'x', '')",
        (session_id,),
    )

    memory_db.execute("INSERT INTO branches (session_id, leaf_uuid, is_active) VALUES (?, 'a2', 1)", (session_id,))
    branch_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _message_id(uuid: str) -> int:
        return memory_db.execute(
            "SELECT id FROM messages WHERE session_id = ? AND uuid = ?", (session_id, uuid)
        ).fetchone()[0]

    for uuid in ("u1", "a1", "u2", "a2"):
        memory_db.execute(
            "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
            (branch_id, _message_id(uuid)),
        )
    memory_db.commit()

    first = summarize_ingestion(memory_db)
    assert first["ok_sessions"] == 1
    first_cache = _cache_row(memory_db, "sess-cache-link-substitution")
    assert first_cache is not None

    # same-count substitution: drop the link to u1 (on the expected active
    # path), add a link to x1 (not on it) instead — branch_id's total link
    # count is still 4, so a COUNT(*)-based fingerprint wouldn't change, but
    # the active branch's real expected-message coverage now has a gap.
    memory_db.execute(
        "DELETE FROM branch_messages WHERE branch_id = ? AND message_id = ?",
        (branch_id, _message_id("u1")),
    )
    memory_db.execute(
        "INSERT INTO branch_messages (branch_id, message_id) VALUES (?, ?)",
        (branch_id, _message_id("x1")),
    )
    memory_db.commit()

    with patch("ccrecall.ingestion_status.parse_all_with_uuids", wraps=parsing.parse_all_with_uuids) as parse_all:
        second = summarize_ingestion(memory_db)

    assert parse_all.call_count == 1
    assert second["ok_sessions"] == 0
    assert second["ingestion_gap_sessions"] == 1
    # summarize_ingestion only (re-)writes ingestion_check_cache for an "ok"
    # verdict — the corrupted session is no longer "ok", so the cache is left
    # untouched rather than being overwritten with a false "ok" of the
    # corrupted state.
    assert _cache_row(memory_db, "sess-cache-link-substitution") == first_cache


def test_problem_session_is_not_cached_and_is_reparsed(memory_db, tmp_path):
    filepath = tmp_path / "sess-problem.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1"])

    first = summarize_ingestion(memory_db)

    assert first["pending_tail_sessions"] == 1
    assert _cache_row(memory_db, "sess-problem") is None

    with patch("ccrecall.ingestion_status.parse_all_with_uuids", wraps=parsing.parse_all_with_uuids) as parse_all:
        second = summarize_ingestion(memory_db)

    assert parse_all.call_count == 1
    assert second["pending_tail_sessions"] == 1
    assert _cache_row(memory_db, "sess-problem") is None


def test_multifile_session_uses_parent_chain_not_import_log_order(memory_db, tmp_path):
    parent = tmp_path / "sess-multi.jsonl"
    agent = tmp_path / "agent-sess-multi.jsonl"
    write_jsonl(
        parent,
        [
            make_jsonl_entry("u1", None, "2026-01-01T10:00:00Z", "user", "first"),
            make_jsonl_entry("a1", "u1", "2026-01-01T10:00:01Z", "assistant", "answer"),
        ],
    )
    write_jsonl(
        agent,
        [
            make_jsonl_entry("u2", "a1", "2026-01-01T10:00:02Z", "user", "second"),
            make_jsonl_entry("a2", "u2", "2026-01-01T10:00:03Z", "assistant", "answer"),
        ],
    )
    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-multi')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for uuid in ["u1", "a1"]:
        memory_db.execute(
            "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, ?, 'user', 'x', '')",
            (session_id, uuid),
        )
    _link_active_branch(memory_db, session_id, "a1", ["u1", "a1"])
    _insert_import_log(memory_db, agent, 0)
    _insert_import_log(memory_db, parent, 2)

    status = summarize_ingestion(memory_db)

    assert status["pending_tail_sessions"] == 1
    assert status["pending_tail_turns"] == 2
    assert status["ingestion_gap_sessions"] == 0


def test_multifile_equal_timestamp_prefers_deeper_agent_leaf(memory_db, tmp_path):
    parent = tmp_path / "sess-equal.jsonl"
    agent = tmp_path / "agent-sess-equal.jsonl"
    shared_ts = "2026-01-01T10:00:01Z"
    write_jsonl(
        parent,
        [
            make_jsonl_entry("u1", None, "2026-01-01T10:00:00Z", "user", "first"),
            make_jsonl_entry("a1", "u1", shared_ts, "assistant", "answer"),
        ],
    )
    write_jsonl(agent, [make_jsonl_entry("a2", "a1", shared_ts, "assistant", "agent follow-up")])
    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-equal')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for uuid in ["u1", "a1"]:
        memory_db.execute(
            "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, ?, 'user', 'x', '')",
            (session_id, uuid),
        )
    _link_active_branch(memory_db, session_id, "a1", ["u1", "a1"])
    _insert_import_log(memory_db, agent, 1)
    _insert_import_log(memory_db, parent, 2)

    status = summarize_ingestion(memory_db)

    assert status["pending_tail_sessions"] == 1
    assert status["pending_tail_turns"] == 1
    assert status["ok_sessions"] == 0


def test_import_log_only_session_is_not_counted(memory_db, tmp_path):
    filepath = tmp_path / "filtered-away.jsonl"
    _insert_import_log(memory_db, filepath, 0)

    status = summarize_ingestion(memory_db)

    assert status["sessions_checked"] == 0
    assert status["missing_source_sessions"] == 0


def test_partial_multifile_source_loss_counts_as_missing_source(memory_db, tmp_path):
    parent = tmp_path / "sess-partial.jsonl"
    missing_agent = tmp_path / "agent-sess-partial.jsonl"
    write_jsonl(parent, [make_jsonl_entry("u1", None, "2026-01-01T10:00:00Z", "user", "first")])
    _seed_session(memory_db, parent, ["u1"])
    _insert_import_log(memory_db, missing_agent, 0)

    status = summarize_ingestion(memory_db)

    assert status["sessions_checked"] == 1
    assert status["missing_source_sessions"] == 1
    assert status["ok_sessions"] == 0


def test_partial_multifile_source_loss_stays_missing_source_after_prior_ok_cache(memory_db, tmp_path):
    parent = tmp_path / "sess-partial-cache.jsonl"
    agent = tmp_path / "agent-sess-partial-cache.jsonl"
    write_jsonl(
        parent,
        [
            make_jsonl_entry("u1", None, "2026-01-01T10:00:00Z", "user", "first"),
            make_jsonl_entry("a1", "u1", "2026-01-01T10:00:01Z", "assistant", "answer"),
        ],
    )
    write_jsonl(
        agent,
        [
            make_jsonl_entry("u2", "a1", "2026-01-01T10:00:02Z", "user", "second"),
            make_jsonl_entry("a2", "u2", "2026-01-01T10:00:03Z", "assistant", "answer"),
        ],
    )
    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('sess-partial-cache')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for uuid in ["u1", "a1", "u2", "a2"]:
        memory_db.execute(
            "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, ?, 'user', 'x', '')",
            (session_id, uuid),
        )
    _link_active_branch(memory_db, session_id, "a2", ["u1", "a1", "u2", "a2"])
    _insert_import_log(memory_db, parent, 2)
    _insert_import_log(memory_db, agent, 2)

    first = summarize_ingestion(memory_db)

    assert first["ok_sessions"] == 1
    assert _cache_row(memory_db, "sess-partial-cache") is not None

    agent.unlink()

    with patch(
        "ccrecall.ingestion_status.parse_all_with_uuids",
        side_effect=AssertionError("missing source should bypass cache and parsing"),
    ):
        second = summarize_ingestion(memory_db)

    assert second["sessions_checked"] == 1
    assert second["missing_source_sessions"] == 1
    assert second["ok_sessions"] == 0


def test_find_repairable_sessions_excludes_pending_tail(memory_db, tmp_path):
    filepath = tmp_path / "sess-tail.jsonl"
    write_four_turns(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1"])

    candidates = find_repairable_sessions(memory_db)

    assert candidates == []


def test_find_repairable_sessions_matches_stale_tail_and_ingestion_gap_only(memory_db, tmp_path):
    ok_path = tmp_path / "sess-ok.jsonl"
    write_four_turns(ok_path)
    _seed_session(memory_db, ok_path, ["u1", "a1", "u2", "a2"])

    pending_path = tmp_path / "sess-pending.jsonl"
    write_four_turns(pending_path)
    _seed_session(memory_db, pending_path, ["u1", "a1"])

    stale_path = tmp_path / "sess-stale.jsonl"
    write_four_turns(stale_path)
    age_past_grace_window(stale_path)
    _seed_session(memory_db, stale_path, ["u1", "a1"])

    gap_path = tmp_path / "sess-gap.jsonl"
    write_four_turns(gap_path)
    _seed_session(memory_db, gap_path, ["u1", "u2", "a2"])

    cursor = memory_db.cursor()
    sources = import_log_source_index(cursor)

    candidates = find_repairable_sessions(memory_db, sources=sources)
    candidate_uuids = {uuid for uuid, _session_id, _project_id, _filepaths in candidates}

    assert candidate_uuids == {"sess-stale", "sess-gap"}

    status = summarize_ingestion(memory_db, sources=sources)
    assert status["stale_tail_sessions"] + status["ingestion_gap_sessions"] == len(candidates)


def test_reclassify_session_reflects_db_catch_up(memory_db, tmp_path):
    filepath = tmp_path / "sess-reclassify.jsonl"
    write_four_turns(filepath)
    age_past_grace_window(filepath)
    _seed_session(memory_db, filepath, ["u1", "a1"])

    cursor = memory_db.cursor()
    sources = import_log_source_index(cursor)
    filepaths = sources["sess-reclassify"]["existing"]

    assert reclassify_session(memory_db, "sess-reclassify", filepaths) == "stale_tail"

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-reclassify",)).fetchone()[0]
    for uuid in ("u2", "a2"):
        memory_db.execute(
            "INSERT INTO messages (session_id, uuid, role, content, tool_content) VALUES (?, ?, 'user', 'x', '')",
            (session_id, uuid),
        )
    branch_id = memory_db.execute(
        "SELECT id FROM branches WHERE session_id = ? AND is_active = 1", (session_id,)
    ).fetchone()[0]
    _link_messages_to_branch(memory_db, session_id, branch_id, ["u2", "a2"])
    memory_db.commit()

    assert reclassify_session(memory_db, "sess-reclassify", filepaths) == "ok"


def test_toctou_deleted_file_during_expected_uuids_yields_missing_source_and_continues(memory_db, tmp_path):
    """A file that vanishes between the initial _source_fingerprint() stat and
    the later _expected_uuids() read (a TOCTOU race — e.g. a concurrent import
    or a user deleting old transcripts) must be contained to that one session's
    classify_sessions() iteration, not raise out of the generator and abort
    classification for every other session too."""
    ok_path = tmp_path / "sess-ok.jsonl"
    write_four_turns(ok_path)
    _seed_session(memory_db, ok_path, ["u1", "a1", "u2", "a2"])

    race_path = tmp_path / "sess-race.jsonl"
    write_four_turns(race_path)
    _seed_session(memory_db, race_path, ["u1", "a1"])

    real_expected_uuids = ingestion_status._expected_uuids

    def flaky_expected_uuids(filepaths):
        if filepaths == [race_path]:
            raise FileNotFoundError(2, "No such file or directory", str(race_path))
        return real_expected_uuids(filepaths)

    with patch("ccrecall.ingestion_status._expected_uuids", side_effect=flaky_expected_uuids):
        status = summarize_ingestion(memory_db)

    assert status["sessions_checked"] == 2
    assert status["missing_source_sessions"] == 1
    assert status["ok_sessions"] == 1


def test_toctou_deleted_file_during_mtime_stat_yields_missing_source(memory_db, tmp_path):
    """Same TOCTOU race, but hitting the contiguous-suffix branch's
    ``path.stat()`` call (used to compute the newest mtime for pending/stale
    tail classification) rather than the _expected_uuids() read — the second
    unguarded read site the same try/except must also cover."""
    race_path = tmp_path / "sess-race-stat.jsonl"
    write_four_turns(race_path)
    _seed_session(memory_db, race_path, ["u1", "a1"])  # contiguous missing suffix -> reaches the stat() branch

    real_expected_uuids = ingestion_status._expected_uuids

    def delete_after_read(filepaths):
        result = real_expected_uuids(filepaths)
        for path in filepaths:
            path.unlink(missing_ok=True)
        return result

    with patch("ccrecall.ingestion_status._expected_uuids", side_effect=delete_after_read):
        status = summarize_ingestion(memory_db)

    assert status["missing_source_sessions"] == 1
    assert status["pending_tail_sessions"] == 0
    assert status["stale_tail_sessions"] == 0
