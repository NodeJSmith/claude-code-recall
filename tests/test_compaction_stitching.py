"""Compaction stitching: the active-chain walk, repair of old sessions, and the
background path that runs it."""

import os
import sqlite3
from pathlib import Path

import pytest
from conftest import make_jsonl_entry, write_jsonl

from ccrecall import ingestion_status
from ccrecall.embeddings import EMBEDDING_VERSION
from ccrecall.hooks import import_conversations, import_repair
from ccrecall.ingestion_status import _db_coverage_fingerprint, find_repairable_sessions, summarize_ingestion
from ccrecall.parsing import active_chain, find_all_branches, is_valid_entry, select_active_leaf_entry

SUMMARY_TEXT = "This session is being continued from a previous conversation"


def boundary(uuid: str, logical_parent: str | None, ts: str, parent: str | None = None) -> dict:
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "logicalParentUuid": logical_parent,
        "type": "system",
        "subtype": "compact_boundary",
        "timestamp": ts,
    }


def summary(uuid: str, parent: str, ts: str) -> dict:
    return {**make_jsonl_entry(uuid, parent, ts, "user", SUMMARY_TEXT), "isCompactSummary": True}


def turn(uuid: str, parent: str | None, minute: int, role: str = "user") -> dict:
    return make_jsonl_entry(uuid, parent, f"2026-01-01T10:{minute:02d}:00Z", role, f"text of {uuid}")


def compacted_session(logical_parent: str = "a2") -> list[dict]:
    """Two real exchanges, a compaction, then one more exchange."""
    return [
        turn("u1", None, 0),
        turn("a1", "u1", 1, "assistant"),
        turn("u2", "a1", 2),
        turn("a2", "u2", 3, "assistant"),
        boundary("b1", logical_parent, "2026-01-01T10:04:00Z"),
        summary("s1", "b1", "2026-01-01T10:05:00Z"),
        turn("u3", "s1", 6),
        turn("a3", "u3", 7, "assistant"),
    ]


def chain_uuids(entries: list[dict]) -> list[str]:
    return [entry["uuid"] for entry in active_chain(entries)]


def parent_only_branches(all_entries: list[dict]) -> list[dict]:
    """The walk as it was before stitching, for seeding an already-imported session."""
    by_uuid = {entry["uuid"]: entry for entry in all_entries}
    latest = select_active_leaf_entry(all_entries)
    uuids: set[str] = set()
    current = latest["uuid"]
    while current and current not in uuids and current in by_uuid:
        uuids.add(current)
        current = by_uuid[current].get("parentUuid")
    return [{"leaf_uuid": latest["uuid"], "uuids": uuids, "is_active": True}]


@pytest.fixture
def legacy_session(memory_db, project_id, tmp_path, monkeypatch) -> Path:
    """A compacted session imported by the old walk, cached ok under the old
    fingerprint and with a current embedding watermark."""
    filepath = tmp_path / "sess-compacted.jsonl"
    write_jsonl(filepath, compacted_session())
    with monkeypatch.context() as patched:
        patched.setattr("ccrecall.session_ops.find_all_branches", parent_only_branches)
        import_conversations.import_session(memory_db, filepath, project_id)
    memory_db.execute("UPDATE branches SET embedding_version = ?", (EMBEDDING_VERSION,))
    session_id = memory_db.execute("SELECT id FROM sessions").fetchone()[0]
    stat = filepath.stat()
    memory_db.execute(
        "INSERT INTO ingestion_check_cache (session_uuid, source_fingerprint, db_coverage_fingerprint)"
        " VALUES (?, ?, ?)",
        (
            "sess-compacted",
            f"{filepath}\t{stat.st_size}\t{stat.st_mtime_ns}",
            _db_coverage_fingerprint(memory_db.cursor(), session_id),
        ),
    )
    memory_db.commit()
    return filepath


def linked_uuids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT m.uuid FROM branch_messages bm
        JOIN branches b ON b.id = bm.branch_id AND b.is_active = 1
        JOIN messages m ON m.id = bm.message_id
        """
    ).fetchall()
    return {row[0] for row in rows}


class TestActiveChain:
    def test_crosses_a_boundary_in_order_and_drops_the_summary(self):
        assert chain_uuids(compacted_session()) == ["u1", "a1", "u2", "a2", "b1", "u3", "a3"]

    def test_crosses_every_boundary_in_a_session(self):
        entries = [
            *compacted_session(),
            boundary("b2", "a3", "2026-01-01T10:08:00Z"),
            summary("s2", "b2", "2026-01-01T10:09:00Z"),
            turn("u4", "s2", 10),
        ]
        assert chain_uuids(entries) == ["u1", "a1", "u2", "a2", "b1", "u3", "a3", "b2", "u4"]

    def test_dangling_pointer_ends_the_walk_and_keeps_the_summary(self):
        assert chain_uuids(compacted_session("gone")) == ["b1", "s1", "u3", "a3"]

    def test_null_pointer_ends_the_walk_and_keeps_the_summary(self):
        entries = compacted_session()
        del entries[4]["logicalParentUuid"]
        assert chain_uuids(entries) == ["b1", "s1", "u3", "a3"]

    def test_hop_pointing_downstream_ends_the_walk_and_keeps_the_summary(self):
        assert chain_uuids(compacted_session("u3")) == ["b1", "s1", "u3", "a3"]

    def test_parent_cycle_across_a_hop_terminates(self):
        entries = compacted_session()
        entries[0]["parentUuid"] = "b1"  # u1 -> b1 -> (hop) a2 -> ... -> u1
        assert chain_uuids(entries) == ["u1", "a1", "u2", "a2", "b1", "u3", "a3"]

    def test_rewound_fork_stays_out(self):
        entries = [*compacted_session(), turn("fork", "a1", 3)]
        assert "fork" not in chain_uuids(entries)

    def test_logical_parent_on_a_non_boundary_is_not_followed(self):
        entries = [
            turn("u1", None, 0),
            {**turn("u2", None, 1), "logicalParentUuid": "u1"},
        ]
        assert chain_uuids(entries) == ["u2"]

    def test_session_ending_on_its_summary_keeps_a_nonempty_chain(self):
        entries = compacted_session()[:6]
        assert chain_uuids(entries) == ["u1", "a1", "u2", "a2", "b1"]
        assert find_all_branches(entries)[0]["leaf_uuid"] == "b1"

    @pytest.mark.parametrize(
        ("logical_parent", "expected"),
        [
            ("a2", ["u1", "a1", "u2", "a2", "b1", "att", "info"]),
            ("gone", ["b1", "s1", "att", "info"]),
        ],
    )
    def test_session_left_idle_after_a_compaction(self, logical_parent, expected):
        """The real shape of a session whose last act was a compaction: the boundary is
        timestamped just after its summary, and attachments and a system note trail it."""
        entries = [
            *compacted_session(logical_parent)[:4],
            summary("s1", "b1", "2026-01-01T10:04:00.000Z"),
            boundary("b1", logical_parent, "2026-01-01T10:04:00.005Z"),
            {"uuid": "att", "parentUuid": "s1", "type": "attachment", "timestamp": "2026-01-01T10:04:01Z"},
            {
                "uuid": "info",
                "parentUuid": "att",
                "type": "system",
                "subtype": "informational",
                "timestamp": "2026-01-01T10:04:02Z",
            },
        ]
        assert chain_uuids(entries) == expected

    def test_non_string_logical_parent_is_rejected_at_the_parse_boundary(self):
        assert not is_valid_entry(boundary("b1", ["not", "a", "uuid"], "2026-01-01T10:04:00Z"))

    @pytest.mark.parametrize("logical_parent", ["a2", "gone", "u3"])
    def test_indexing_and_the_ingestion_check_agree(self, tmp_path, logical_parent):
        """Guards against a second copy of the walk; the tests above pin the walk itself."""
        entries = compacted_session(logical_parent)
        filepath = tmp_path / "sess.jsonl"
        write_jsonl(filepath, entries)
        indexed = find_all_branches(entries)[0]["uuids"]
        expected = ingestion_status._expected_uuids([filepath])
        assert set(expected) == indexed - {"b1"}


class TestFreshImport:
    def test_indexes_every_chain_and_counts_real_turns(self, memory_db, project_id, tmp_path):
        filepath = tmp_path / "sess.jsonl"
        write_jsonl(filepath, compacted_session())
        import_conversations.import_session(memory_db, filepath, project_id)

        assert linked_uuids(memory_db) == {"u1", "a1", "u2", "a2", "u3", "a3"}
        assert memory_db.execute("SELECT exchange_count FROM branches").fetchone()[0] == 3
        assert memory_db.execute("SELECT COUNT(*) FROM messages WHERE uuid = 's1'").fetchone()[0] == 0
        assert summarize_ingestion(memory_db)["ok_sessions"] == 1

    def test_dangling_pointer_keeps_the_summary_searchable(self, memory_db, project_id, tmp_path):
        filepath = tmp_path / "sess.jsonl"
        write_jsonl(filepath, compacted_session("gone")[4:])
        import_conversations.import_session(memory_db, filepath, project_id)

        assert linked_uuids(memory_db) == {"s1", "u3", "a3"}
        assert SUMMARY_TEXT in memory_db.execute("SELECT aggregated_content FROM branches").fetchone()[0]


class TestRepairOfImportedSessions:
    def test_old_ok_verdict_no_longer_hides_the_session(self, memory_db, legacy_session):
        candidates = find_repairable_sessions(memory_db)
        assert [candidate[0] for candidate in candidates] == ["sess-compacted"]

    def test_bumping_the_walk_version_invalidates_ok_verdicts(self, memory_db, project_id, tmp_path, monkeypatch):
        filepath = tmp_path / "sess.jsonl"
        write_jsonl(filepath, compacted_session())
        import_conversations.import_session(memory_db, filepath, project_id)
        find_repairable_sessions(memory_db)

        with monkeypatch.context() as patched:
            patched.setattr(ingestion_status, "ACTIVE_CHAIN_VERSION", ingestion_status.ACTIVE_CHAIN_VERSION + 1)
            parses = _count_parses(patched)
            find_repairable_sessions(memory_db)
        assert parses == [filepath]

    def test_repair_recovers_history_and_keeps_every_existing_row(self, memory_db, legacy_session):
        assert linked_uuids(memory_db) == {"s1", "u3", "a3"}
        rows_before = {row[0] for row in memory_db.execute("SELECT uuid FROM messages")}

        repaired, recovered, failed, unrepairable = import_repair.repair_sessions(
            memory_db, find_repairable_sessions(memory_db)
        )

        assert (repaired, recovered, failed, unrepairable) == (1, 4, 0, 0)
        assert linked_uuids(memory_db) == {"u1", "a1", "u2", "a2", "u3", "a3"}
        assert rows_before <= {row[0] for row in memory_db.execute("SELECT uuid FROM messages")}
        exchange_count, content = memory_db.execute(
            "SELECT exchange_count, aggregated_content FROM branches"
        ).fetchone()
        assert exchange_count == 3
        assert "text of u1" in content
        assert SUMMARY_TEXT not in content

    def test_repair_hands_the_session_to_backfill(self, memory_db, legacy_session):
        import_repair.repair_sessions(memory_db, find_repairable_sessions(memory_db))
        assert memory_db.execute("SELECT embedding_version FROM branches").fetchone()[0] is None

    def test_second_run_over_unchanged_sessions_parses_nothing(self, memory_db, legacy_session, monkeypatch):
        import_repair.repair_sessions(memory_db, find_repairable_sessions(memory_db))
        assert find_repairable_sessions(memory_db) == []  # verified by parsing; records the verdict

        with monkeypatch.context() as patched:
            parses = _count_parses(patched)
            assert find_repairable_sessions(memory_db) == []
        assert parses == []

    def test_verdict_for_a_changed_transcript_is_rechecked(self, memory_db, legacy_session, monkeypatch):
        import_repair.repair_sessions(memory_db, find_repairable_sessions(memory_db))
        find_repairable_sessions(memory_db)

        os.utime(legacy_session, ns=(1, 1))
        with monkeypatch.context() as patched:
            parses = _count_parses(patched)
            find_repairable_sessions(memory_db)
        assert parses == [legacy_session]


class TestOkVerdictWrites:
    def _seed_sessions(self, memory_db, project_id, tmp_path, count: int) -> list[Path]:
        paths = []
        for index in range(count):
            filepath = tmp_path / f"sess-{index}.jsonl"
            write_jsonl(filepath, [turn("u1", None, 0), turn("a1", "u1", 1, "assistant")])
            import_conversations.import_session(memory_db, filepath, project_id)
            paths.append(filepath)
        memory_db.commit()
        return paths

    def test_no_transaction_is_left_open_for_the_repair_loop(self, memory_db, project_id, tmp_path):
        self._seed_sessions(memory_db, project_id, tmp_path, 2)
        find_repairable_sessions(memory_db)
        assert not memory_db.in_transaction
        assert memory_db.execute("SELECT COUNT(*) FROM ingestion_check_cache").fetchone()[0] == 2

    def test_a_killed_run_keeps_its_committed_batches(self, memory_db, project_id, tmp_path, monkeypatch):
        self._seed_sessions(memory_db, project_id, tmp_path, 3)
        monkeypatch.setattr(ingestion_status, "OK_CACHE_COMMIT_BATCH", 1)
        real_expected = ingestion_status._expected_uuids
        calls = []

        def die_on_last_session(filepaths):
            calls.append(filepaths)
            if len(calls) == 3:
                raise KeyboardInterrupt
            return real_expected(filepaths)

        monkeypatch.setattr(ingestion_status, "_expected_uuids", die_on_last_session)
        with pytest.raises(KeyboardInterrupt):
            find_repairable_sessions(memory_db)
        memory_db.rollback()

        assert memory_db.execute("SELECT COUNT(*) FROM ingestion_check_cache").fetchone()[0] == 2

    def test_one_unreadable_transcript_does_not_stop_the_others(self, memory_db, legacy_session, tmp_path, monkeypatch):
        bad = self._seed_sessions(memory_db, memory_db.execute("SELECT id FROM projects").fetchone()[0], tmp_path, 1)[0]
        real_expected = ingestion_status._expected_uuids

        def unreadable(filepaths):
            if filepaths == [bad]:
                raise PermissionError(13, "Permission denied", str(bad))
            return real_expected(filepaths)

        monkeypatch.setattr(ingestion_status, "_expected_uuids", unreadable)

        assert [candidate[0] for candidate in find_repairable_sessions(memory_db)] == ["sess-compacted"]
        assert summarize_ingestion(memory_db)["missing_source_sessions"] == 1

    def test_one_unstatable_transcript_does_not_stop_the_others(self, memory_db, legacy_session, tmp_path, monkeypatch):
        bad = self._seed_sessions(memory_db, memory_db.execute("SELECT id FROM projects").fetchone()[0], tmp_path, 1)[0]
        real_fingerprint = ingestion_status._source_fingerprint

        def unstatable(filepaths):
            if filepaths == [bad]:
                raise PermissionError(13, "Permission denied", str(bad))
            return real_fingerprint(filepaths)

        monkeypatch.setattr(ingestion_status, "_source_fingerprint", unstatable)

        assert [candidate[0] for candidate in find_repairable_sessions(memory_db)] == ["sess-compacted"]
        assert summarize_ingestion(memory_db)["missing_source_sessions"] == 1

    def test_gapped_sessions_get_no_verdict(self, memory_db, legacy_session):
        find_repairable_sessions(memory_db)
        row = memory_db.execute("SELECT source_fingerprint FROM ingestion_check_cache").fetchone()
        assert "active-chain-v" not in row[0]


def _count_parses(patched: pytest.MonkeyPatch) -> list[Path]:
    """Record which transcripts the ingestion check parses."""
    parses: list[Path] = []
    real_parse = ingestion_status.parse_all_with_uuids

    def counting_parse(filepath):
        parses.append(filepath)
        return real_parse(filepath)

    patched.setattr(ingestion_status, "parse_all_with_uuids", counting_parse)
    return parses
