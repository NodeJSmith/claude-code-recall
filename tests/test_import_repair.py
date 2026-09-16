"""Tests for hooks/import_repair.py's repair_sessions() execution loop."""

import logging
import sqlite3

import pytest
from conftest import (
    age_past_grace_window,
    delete_message,
    make_jsonl_entry,
    seed_stale_tail_session,
    write_four_turns,
    write_jsonl,
)

from ccrecall import ingestion_status
from ccrecall.hooks import import_conversations, import_repair
from ccrecall.hooks.import_repair import repair_sessions
from ccrecall.import_log_ops import import_log_source_index
from ccrecall.ingestion_status import find_repairable_sessions


class _SavepointFailingConn:
    """Delegates everything to a real sqlite3.Connection except ``execute``,
    which raises on the Nth occurrence of one specific SQL statement — used
    to simulate an OperationalError on the SAVEPOINT/RELEASE statements
    themselves (Finding 4), which cannot be reproduced by monkeypatching a
    real sqlite3.Connection instance directly (its methods are read-only
    slot descriptors). With ``fail_forever=True``, every occurrence from
    ``fail_on_occurrence`` onward raises instead of just the one — used to
    simulate a persistent (not transient) failure, e.g. the RELEASE-recovery
    RELEASE call failing the same way the original RELEASE did."""

    def __init__(
        self,
        real: sqlite3.Connection,
        fail_sql: str,
        fail_on_occurrence: int = 1,
        fail_forever: bool = False,
    ) -> None:
        self._real = real
        self._fail_sql = fail_sql
        self._fail_on_occurrence = fail_on_occurrence
        self._fail_forever = fail_forever
        self._occurrences = 0

    def execute(self, sql, *args, **kwargs):
        if sql == self._fail_sql:
            self._occurrences += 1
            if self._occurrences == self._fail_on_occurrence or (
                self._fail_forever and self._occurrences > self._fail_on_occurrence
            ):
                raise sqlite3.OperationalError(f"simulated failure on: {sql}")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def project_id(memory_db):
    cursor = memory_db.cursor()
    cursor.execute(
        "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
        ("/test/project", "-test-project", "test_project"),
    )
    memory_db.commit()
    return cursor.lastrowid


def _stale_tail_candidates(memory_db) -> list:
    cursor = memory_db.cursor()
    sources = import_log_source_index(cursor)
    return find_repairable_sessions(memory_db, sources=sources)


def test_repair_stale_tail_session_recovers_missing_message(memory_db, project_id, tmp_path):
    filepath = tmp_path / "sess-stale.jsonl"
    session_id = seed_stale_tail_session(memory_db, project_id, filepath, uuid="sess-stale")

    candidates = _stale_tail_candidates(memory_db)
    assert [c[0] for c in candidates] == ["sess-stale"]

    result = repair_sessions(memory_db, candidates)

    assert result == (1, 1, 0, 0)
    assert (
        memory_db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND uuid = 'a2'", (session_id,)
        ).fetchone()[0]
        == 1
    )


def test_repair_rerun_on_already_fixed_session_is_idempotent(memory_db, project_id, tmp_path):
    filepath = tmp_path / "sess-stale.jsonl"
    session_id = seed_stale_tail_session(memory_db, project_id, filepath, uuid="sess-stale")

    candidates = _stale_tail_candidates(memory_db)
    first = repair_sessions(memory_db, candidates)
    assert first == (1, 1, 0, 0)

    before_count = memory_db.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]

    # Re-run with the *same* candidate list (as returned before the repair) —
    # the session is already "ok" now, so a fresh find_repairable_sessions()
    # call would return nothing; passing the stale list mirrors a caller that
    # computed candidates once and repairs are attempted from that snapshot.
    second = repair_sessions(memory_db, candidates)

    assert second == (1, 0, 0, 0)
    after_count = memory_db.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]
    assert after_count == before_count, "idempotent re-run must not insert duplicate rows"


def test_unrepairable_candidate_is_not_counted_as_repaired(memory_db, project_id, tmp_path, monkeypatch):
    """A candidate whose source transcript genuinely lacks the expected content
    still classifies as stale_tail/ingestion_gap after a full force-reimport.

    classify_sessions' expected-message heuristic (message_content_parts, via
    _entry_expects_message) and the real per-entry import filter share the
    same function, so a content-level divergence between "classify expects a
    message" and "the importer actually inserts one" cannot be constructed
    from JSONL content alone within this module's tests — ingestion_status's
    own test suite owns verifying that shared classification logic. What this
    task (import_repair.py's execution loop) owns is: given that
    reclassify_session reports the candidate is still stale_tail/ingestion_gap
    after a real, non-raising force-reimport, repair_sessions() must report it
    as unrepairable rather than folding it into "repaired". Mocking
    reclassify_session's return value isolates exactly that handling logic.
    """
    filepath = tmp_path / "sess-unrepairable.jsonl"
    seed_stale_tail_session(memory_db, project_id, filepath, uuid="sess-unrepairable")

    candidates = _stale_tail_candidates(memory_db)
    assert [c[0] for c in candidates] == ["sess-unrepairable"]

    monkeypatch.setattr(import_repair.ingestion_status, "reclassify_session", lambda *a, **k: "stale_tail")

    result = repair_sessions(memory_db, candidates)

    assert result == (0, 1, 0, 1)


def test_poison_file_candidate_is_counted_failed_and_batch_continues(
    memory_db, project_id, tmp_path, monkeypatch, caplog
):
    poison_path = tmp_path / "sess-poison.jsonl"
    good_path = tmp_path / "sess-good.jsonl"

    for filepath, uuid in ((poison_path, "sess-poison"), (good_path, "sess-good")):
        seed_stale_tail_session(memory_db, project_id, filepath, uuid=uuid)

    candidates = _stale_tail_candidates(memory_db)
    assert {c[0] for c in candidates} == {"sess-poison", "sess-good"}

    real_import_session = import_conversations.import_session
    real_reclassify = ingestion_status.reclassify_session
    reclassify_calls: list[str] = []

    def _raise_on_poison(conn, filepath, project_id, *, force=False):
        if filepath == poison_path:
            raise RuntimeError("simulated poison transcript")
        return real_import_session(conn, filepath, project_id, force=force)

    def _spy_reclassify(conn, session_uuid, filepaths, **kwargs):
        reclassify_calls.append(session_uuid)
        return real_reclassify(conn, session_uuid, filepaths, **kwargs)

    monkeypatch.setattr(import_repair.import_conversations, "import_session", _raise_on_poison)
    monkeypatch.setattr(import_repair.ingestion_status, "reclassify_session", _spy_reclassify)

    with caplog.at_level(logging.ERROR, logger="ccrecall"):
        result = repair_sessions(memory_db, candidates)

    sessions_repaired, messages_recovered, sessions_failed, sessions_unrepairable = result
    assert sessions_failed == 1
    assert sessions_repaired == 1
    assert sessions_unrepairable == 0
    assert messages_recovered == 1, "only the good candidate's file recovered a message"
    assert reclassify_calls == ["sess-good"], "reclassify_session must not run for the poison candidate"
    assert any(record.exc_info for record in caplog.records if record.levelno >= logging.ERROR)


def test_multifile_candidate_repair_preserves_cross_file_link(memory_db, project_id, tmp_path):
    """Load-bearing regression test for Finding 1.

    A multi-file candidate's branch_messages diff must see every file's
    entries in one merged pass. Processing files one at a time (the pre-fix
    behavior) computed each file's active-branch membership from that file's
    own entries in isolation: u1's link is only resolvable when file 1's
    entries (u1, a1) are walked together with file 2's (u2, a2) — processed
    alone, file 2's local parentUuid chain stops at a1 (a1 has no known
    parent within file 2) and never reaches u1, so the old per-file repair
    loop dropped u1's branch_messages link even though its messages row
    survived untouched. This is the exact "repaired but orphaned" scenario
    from the challenge finding.
    """
    parent = tmp_path / "sess-crosslink.jsonl"
    agent = tmp_path / "agent-sess-crosslink.jsonl"
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

    # Establish a correctly-linked baseline in one merged pass — matching what
    # the fixed repair path itself does — so the pre-repair state has all four
    # messages linked before the gap is introduced.
    import_conversations.import_session_group(memory_db, [parent, agent], project_id)
    memory_db.commit()

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-crosslink",)).fetchone()[0]

    def _linked_uuids() -> set[str]:
        rows = memory_db.execute(
            "SELECT m.uuid FROM branch_messages bm JOIN messages m ON bm.message_id = m.id WHERE m.session_id = ?",
            (session_id,),
        ).fetchall()
        return {row[0] for row in rows}

    assert _linked_uuids() == {"u1", "a1", "u2", "a2"}, "baseline setup: all four must be linked before the gap"

    # Simulate a stale tail: drop only a2 (file 2's own last message), age
    # both files past the grace window so find_repairable_sessions classifies
    # this as a repairable gap.
    delete_message(memory_db, session_id, "a2")
    memory_db.commit()
    age_past_grace_window(parent)
    age_past_grace_window(agent)

    candidates = _stale_tail_candidates(memory_db)
    assert [c[0] for c in candidates] == ["sess-crosslink"]

    result = repair_sessions(memory_db, candidates)

    assert result == (1, 1, 0, 0), "a2 must be recovered and the session reclassified as repaired"
    assert _linked_uuids() == {"u1", "a1", "u2", "a2"}, (
        "u1 (only ever derivable from file 1's own entries) must still be linked after repair — "
        "a multi-file repair must not drop a sibling file's link"
    )


def test_multifile_candidate_failure_rolls_back_atomically(memory_db, project_id, tmp_path, monkeypatch):
    """After Finding 1, a multi-file candidate is force-reimported as one
    merged unit (import_session_group) inside a single SAVEPOINT, so a
    failure anywhere in that unit rolls back the whole candidate — there is
    no more "the parent file's partial progress survives" partial-credit
    case, since there is no longer a per-file loop to partially complete."""
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

    import_conversations.import_session_group(memory_db, [parent, agent], project_id)
    memory_db.commit()

    session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-multi",)).fetchone()[0]
    delete_message(memory_db, session_id, "a1")
    delete_message(memory_db, session_id, "a2")
    memory_db.commit()
    age_past_grace_window(parent)
    age_past_grace_window(agent)

    candidates = _stale_tail_candidates(memory_db)
    assert [c[0] for c in candidates] == ["sess-multi"]

    def _raise(conn, filepaths, project_id):
        raise RuntimeError("simulated poison transcript")

    monkeypatch.setattr(import_repair.import_conversations, "import_session_group", _raise)

    result = repair_sessions(memory_db, candidates)

    sessions_repaired, messages_recovered, sessions_failed, sessions_unrepairable = result
    assert sessions_failed == 1
    assert sessions_repaired == 0
    assert sessions_unrepairable == 0
    assert messages_recovered == 0, "a merged multi-file candidate rolls back as one unit — no partial credit"


def test_savepoint_release_failure_counts_as_failed_and_batch_continues(memory_db, project_id, tmp_path):
    """Finding 4: an OperationalError on the RELEASE SAVEPOINT statement
    itself (a transient, candidate-local problem — distinct from an
    OperationalError raised by the force-reimport's own DB work, which still
    aborts the batch) must be treated as a per-candidate failure, not
    propagate and abort the whole remaining batch."""
    first_path = tmp_path / "sess-first-savepoint.jsonl"
    second_path = tmp_path / "sess-second-savepoint.jsonl"

    for filepath, uuid in ((first_path, "sess-first-savepoint"), (second_path, "sess-second-savepoint")):
        seed_stale_tail_session(memory_db, project_id, filepath, uuid=uuid)

    candidates = _stale_tail_candidates(memory_db)
    assert {c[0] for c in candidates} == {"sess-first-savepoint", "sess-second-savepoint"}

    # Fail RELEASE SAVEPOINT only on its first occurrence (the first
    # candidate processed); the second candidate's RELEASE succeeds normally.
    fake_conn = _SavepointFailingConn(memory_db, "RELEASE SAVEPOINT import_candidate", fail_on_occurrence=1)

    result = repair_sessions(fake_conn, candidates)

    sessions_repaired, _messages_recovered, sessions_failed, sessions_unrepairable = result
    assert sessions_failed == 1, "the candidate whose RELEASE failed must count as failed"
    assert sessions_repaired == 1, "the other candidate must still be repaired — the batch must continue"
    assert sessions_unrepairable == 0
    assert not memory_db.in_transaction, (
        "a failed RELEASE must roll back and release its own savepoint instead of leaving it "
        "open — otherwise the next candidate's SAVEPOINT nests inside it instead of committing "
        "independently, breaking the durability model documented in the module docstring"
    )


def test_savepoint_recovery_failure_aborts_the_batch(memory_db, project_id, tmp_path):
    """If the RELEASE-failure recovery (ROLLBACK TO SAVEPOINT + RELEASE) also
    fails — a persistent, not transient, problem — the connection's savepoint
    stack can no longer be trusted at a known depth. This must escalate to
    the same abort-the-batch handling as a genuine force-reimport
    infrastructure failure, not silently continue with an unhandled
    exception, and not keep counting the batch as merely per-candidate
    failures."""
    first_path = tmp_path / "sess-first-savepoint.jsonl"
    second_path = tmp_path / "sess-second-savepoint.jsonl"

    for filepath, uuid in ((first_path, "sess-first-savepoint"), (second_path, "sess-second-savepoint")):
        seed_stale_tail_session(memory_db, project_id, filepath, uuid=uuid)

    candidates = _stale_tail_candidates(memory_db)
    assert {c[0] for c in candidates} == {"sess-first-savepoint", "sess-second-savepoint"}

    # Fail RELEASE SAVEPOINT on its first occurrence and every occurrence
    # after — the original RELEASE and the recovery RELEASE both fail,
    # simulating a persistent (not transient) problem with the connection.
    fake_conn = _SavepointFailingConn(
        memory_db, "RELEASE SAVEPOINT import_candidate", fail_on_occurrence=1, fail_forever=True
    )

    with pytest.raises(sqlite3.OperationalError):
        repair_sessions(fake_conn, candidates)


def test_project_id_none_candidate_is_unrepairable_not_failed(memory_db, project_id, tmp_path):
    """Finding 7: a candidate with no project_id on record is a permanent
    data-integrity condition (project_id will never become non-null via
    retry), so it must be counted under sessions_unrepairable, not
    sessions_failed."""
    filepath = tmp_path / "sess-no-project.jsonl"
    write_four_turns(filepath)

    candidates = [("sess-no-project", 999999, None, [filepath])]

    result = repair_sessions(memory_db, candidates)

    sessions_repaired, messages_recovered, sessions_failed, sessions_unrepairable = result
    assert sessions_unrepairable == 1
    assert sessions_failed == 0
    assert sessions_repaired == 0
    assert messages_recovered == 0


def test_session_id_churn_still_counts_recovered_messages(memory_db, project_id, tmp_path):
    """Finding 5: the session_id captured before a candidate's force-reimport
    can go stale (import_session deletes an all-filtered-out session's row,
    and a later re-sync for the same uuid gets a new id via upsert_session's
    ON CONFLICT(uuid) DO UPDATE). repair_sessions must re-resolve session_id
    from sessions.uuid immediately before computing count_after, not trust
    the id captured before the file loop ran — otherwise a real recovery
    reads as 0 messages recovered.
    """
    filepath = tmp_path / "sess-churn.jsonl"
    write_four_turns(filepath)

    # A sessions row already exists under the real current id, with zero
    # messages (simulating "an earlier all-filtered-out import already
    # deleted-and-recreated this uuid's row under a fresh id").
    cursor = memory_db.cursor()
    cursor.execute("INSERT INTO sessions (uuid, project_id) VALUES (?, ?)", ("sess-churn", project_id))
    memory_db.commit()
    real_session_id = cursor.execute("SELECT id FROM sessions WHERE uuid = ?", ("sess-churn",)).fetchone()[0]

    # The candidate tuple carries a stale session_id (as if captured before
    # the row above was deleted and recreated) that no longer matches any row.
    stale_session_id = real_session_id + 12345
    candidates = [("sess-churn", stale_session_id, project_id, [filepath])]

    result = repair_sessions(memory_db, candidates)

    sessions_repaired, messages_recovered, sessions_failed, _sessions_unrepairable = result
    assert sessions_failed == 0
    assert messages_recovered == 4, "recovery must be measured against the re-resolved current session_id"
    assert sessions_repaired == 1


def test_repair_sessions_logs_periodic_progress(memory_db, project_id, tmp_path, monkeypatch, caplog):
    """Finding 9: a long-running repair batch must leave a progress trail in
    the log a user can tail, not go silent until the whole batch finishes."""
    monkeypatch.setattr(import_repair, "PROGRESS_LOG_INTERVAL", 2)

    uuids = ["sess-progress-1", "sess-progress-2", "sess-progress-3"]
    for i, uuid in enumerate(uuids):
        filepath = tmp_path / f"{uuid}.jsonl"
        write_jsonl(
            filepath,
            [
                make_jsonl_entry(f"u{i}a", None, f"2026-01-01T10:0{i}:00Z", "user", "first"),
                make_jsonl_entry(f"a{i}a", f"u{i}a", f"2026-01-01T10:0{i}:01Z", "assistant", "answer"),
                make_jsonl_entry(f"u{i}b", f"a{i}a", f"2026-01-01T10:0{i}:02Z", "user", "second"),
                make_jsonl_entry(f"a{i}b", f"u{i}b", f"2026-01-01T10:0{i}:03Z", "assistant", "answer"),
            ],
        )
        import_conversations.import_session(memory_db, filepath, project_id)
        memory_db.commit()
        session_id = memory_db.execute("SELECT id FROM sessions WHERE uuid = ?", (uuid,)).fetchone()[0]
        delete_message(memory_db, session_id, f"a{i}b")
        memory_db.commit()
        age_past_grace_window(filepath)

    candidates = _stale_tail_candidates(memory_db)
    assert len(candidates) == 3

    with caplog.at_level(logging.INFO, logger="ccrecall"):
        result = repair_sessions(memory_db, candidates)

    assert result == (3, 3, 0, 0)

    progress_messages = [record.getMessage() for record in caplog.records if "repair progress" in record.message]
    assert any("2/3" in msg for msg in progress_messages), "expected an intermediate progress log at candidate 2"
    assert any("3/3" in msg for msg in progress_messages), "expected a final progress log at candidate 3"
