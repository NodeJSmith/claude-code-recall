"""Session recaps: the harness recap at the parse boundary, the pick rule, selection,
the generation input and call, the backfill run, and the read surface."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, CLINotFoundError, ResultMessage, TextBlock
from conftest import make_jsonl_entry, write_jsonl
from whenever import Instant, TimeDelta

from ccrecall import config, recaps, run_history
from ccrecall.cli import app
from ccrecall.db import get_connection
from ccrecall.findings import diagnose
from ccrecall.hooks import backfill_recaps, import_conversations
from ccrecall.parsing import AWAY_SUMMARY_SUFFIX, find_all_branches
from ccrecall.session_ops import sync_session

NOW = Instant.from_utc(2026, 11, 15, 12, 0)
UUID_A = "aaaaaaaa-1111-4111-8111-111111111111"
UUID_B = "bbbbbbbb-2222-4222-8222-222222222222"


def iso(instant: Instant) -> str:
    return instant.format_iso()


def ago(minutes: float) -> str:
    return iso(NOW - TimeDelta(minutes=minutes))


def away_summary(uuid: str, parent: str, ts: str, text: str) -> dict:
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "system",
        "subtype": "away_summary",
        "content": text,
        "timestamp": ts,
    }


def conversation(turns: int, *, end_minutes_ago: float = 60) -> list[dict]:
    """``turns`` user/assistant exchanges, one minute apart, the last ending ``end_minutes_ago``."""
    entries = []
    parent = None
    for i in range(turns):
        minutes = end_minutes_ago + 2 * (turns - 1 - i)
        user, assistant = f"u{i}", f"a{i}"
        entries.append(make_jsonl_entry(user, parent, ago(minutes + 1), "user", f"question {i}"))
        entries.append(make_jsonl_entry(assistant, user, ago(minutes), "assistant", f"answer {i}"))
        parent = assistant
    return entries


def seed(
    conn: sqlite3.Connection, tmp_path: Path, uuid: str, entries: list[dict], project: str = "-home-u-src-app"
) -> int:
    """Sync a transcript into ``conn`` and return its sessions.id."""
    project_dir = tmp_path / "projects" / project
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{uuid}.jsonl"
    write_jsonl(path, entries)
    sync_session(conn, path, project_dir)
    conn.commit()
    return conn.execute("SELECT id FROM sessions WHERE uuid = ?", (uuid,)).fetchone()[0]


def select(conn: sqlite3.Connection, **overrides) -> list[str]:
    kwargs = {"now": NOW, "days": 7, "idle_minutes": 30, "limit": None, "exclude_projects": []} | overrides
    return [c.session_uuid for c in recaps.select_candidates(conn, **kwargs)]


class TestAwaySummaryParsing:
    def test_newest_away_summary_on_the_chain_is_kept_with_its_suffix_stripped(self):
        entries = [
            *conversation(2),
            away_summary("s1", "a1", ago(50), "first recap" + AWAY_SUMMARY_SUFFIX),
            away_summary("s2", "s1", ago(40), "second recap" + AWAY_SUMMARY_SUFFIX),
        ]
        [branch] = find_all_branches(entries)
        assert branch["away_summary"] == "second recap"
        assert branch["away_summary_at"] == ago(40)

    def test_an_away_summary_on_a_rewound_fork_is_ignored(self):
        entries = [
            *conversation(2),
            away_summary("fork", "u0", ago(55), "abandoned"),
            make_jsonl_entry("u9", "a1", ago(10), "user", "latest"),
        ]
        [branch] = find_all_branches(entries)
        assert branch["away_summary"] is None
        assert branch["away_summary_at"] is None

    def test_sync_stores_the_harness_recap_on_the_branch(self, memory_db, tmp_path):
        entries = [*conversation(3), away_summary("s1", "a2", ago(45), "Fixing the parser. Next, run tests.")]
        seed(memory_db, tmp_path, UUID_A, entries)
        row = memory_db.execute("SELECT away_summary, away_summary_at FROM branches").fetchone()
        assert row == ("Fixing the parser. Next, run tests.", ago(45))


class TestSchema:
    def test_a_new_database_has_the_recap_columns_and_table(self, tmp_path):
        with get_connection({"db_path": str(tmp_path / "fresh.db")}) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(branches)")}
            recap_columns = {row[1] for row in conn.execute("PRAGMA table_info(recaps)")}
        assert {"away_summary", "away_summary_at"} <= columns
        assert recap_columns == {"session_id", "text", "as_of", "error", "attempted_at"}


class TestPickRecap:
    def pick(self, **kwargs):
        defaults = {
            "harness_text": None,
            "harness_at": None,
            "generated_text": None,
            "generated_as_of": None,
            "last_message_at": ago(60),
        }
        return recaps.pick_recap(**(defaults | kwargs))

    def test_no_recap_at_all(self):
        assert self.pick() is None

    def test_the_newer_recap_wins(self):
        recap = self.pick(harness_text="old", harness_at=ago(90), generated_text="new", generated_as_of=ago(60))
        assert (recap.source, recap.text, recap.current) == (recaps.SOURCE_GENERATED, "new", True)

    def test_claude_code_wins_a_tie(self):
        recap = self.pick(harness_text="h", harness_at=ago(60), generated_text="g", generated_as_of=ago(60))
        assert recap.source == recaps.SOURCE_CLAUDE_CODE

    def test_an_unreadable_last_message_time_is_never_current(self):
        recap = self.pick(harness_text="h", harness_at=ago(10), last_message_at="not a time")
        assert recap.current is False

    def test_a_recap_older_than_the_last_message_is_not_current(self):
        recap = self.pick(harness_text="h", harness_at=ago(70))
        assert (recap.source, recap.current, recap.as_of) == (recaps.SOURCE_CLAUDE_CODE, False, ago(70))


class TestInput:
    def test_input_is_the_whole_branch_prose_without_notifications_or_tool_markers(self, memory_db, tmp_path):
        entries = [
            make_jsonl_entry("u1", None, ago(20), "user", "please fix the bug"),
            make_jsonl_entry(
                "a1",
                "u1",
                ago(19),
                "assistant",
                [
                    {"type": "text", "text": "looking"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
                ],
            ),
            make_jsonl_entry("u2", "a1", ago(18), "user", "<task-notification>agent done</task-notification>"),
            make_jsonl_entry("a2", "u2", ago(17), "assistant", "fixed it"),
        ]
        seed(memory_db, tmp_path, UUID_A, entries)
        branch_id = memory_db.execute("SELECT id FROM branches").fetchone()[0]
        recap_input = recaps.build_input(memory_db, branch_id)
        assert recap_input.prompt == (
            "<transcript>\n[user]\nplease fix the bug\n\n[assistant]\nlooking\n\n[assistant]\nfixed it\n</transcript>\n\n"
            + recaps.RECAP_PROMPT
        )
        assert recap_input.as_of == ago(17)

    def test_a_compacted_branch_sends_turns_from_both_sides_of_the_boundary(self, memory_db, tmp_path):
        entries = [
            make_jsonl_entry("u1", None, ago(30), "user", "before compaction"),
            make_jsonl_entry("a1", "u1", ago(29), "assistant", "earlier answer"),
            {
                "uuid": "b1",
                "parentUuid": None,
                "logicalParentUuid": "a1",
                "type": "system",
                "subtype": "compact_boundary",
                "timestamp": ago(28),
            },
            {
                **make_jsonl_entry("s1", "b1", ago(27), "user", "This session is being continued"),
                "isCompactSummary": True,
            },
            make_jsonl_entry("u2", "s1", ago(26), "user", "after compaction"),
            make_jsonl_entry("a2", "u2", ago(25), "assistant", "later answer"),
        ]
        seed(memory_db, tmp_path, UUID_A, entries)
        branch_id = memory_db.execute("SELECT id FROM branches").fetchone()[0]
        prompt = recaps.build_input(memory_db, branch_id).prompt
        assert "before compaction" in prompt
        assert "after compaction" in prompt
        assert "being continued" not in prompt

    def test_over_the_cap_it_keeps_the_first_user_turn_and_the_most_recent_rows(self):
        blocks = [("user", "FIRST" + "x" * 95)] + [("assistant", f"row{i:03d}" + "y" * 94) for i in range(50)]
        capped = recaps.cap_blocks(blocks, 1000)
        assert capped[0] == blocks[0][1]
        assert capped[1] == recaps.OMITTED_MARKER
        assert capped[-1] == blocks[-1][1]
        assert len("\n\n".join(capped)) <= 1000

    def test_an_oversized_newest_row_is_truncated_rather_than_dropped(self):
        blocks = [("user", "FIRST"), ("assistant", "middle"), ("assistant", "x" * 2000 + "END")]
        capped = recaps.cap_blocks(blocks, 1000)
        assert capped[0] == "FIRST"
        assert capped[-1].endswith("END")
        assert len("\n\n".join(capped)) <= 1000

    def test_under_the_cap_nothing_is_dropped(self):
        blocks = [("user", "a"), ("assistant", "b")]
        assert recaps.cap_blocks(blocks, 1000) == ["a", "b"]


class TestSelection:
    def test_a_stopped_session_with_three_user_turns_is_selected(self, memory_db, tmp_path):
        seed(memory_db, tmp_path, UUID_A, conversation(3))
        assert select(memory_db) == [UUID_A]

    def test_sessions_with_fewer_than_three_user_turns_are_left_out(self, memory_db, tmp_path):
        seed(memory_db, tmp_path, UUID_A, conversation(2))
        assert select(memory_db) == []

    def test_a_session_still_in_use_waits_until_it_is_idle(self, memory_db, tmp_path):
        seed(memory_db, tmp_path, UUID_A, conversation(3, end_minutes_ago=5))
        assert select(memory_db) == []
        assert select(memory_db, idle_minutes=0) == [UUID_A]

    def test_sessions_older_than_the_window_are_left_out(self, memory_db, tmp_path):
        seed(memory_db, tmp_path, UUID_A, conversation(3, end_minutes_ago=8 * 24 * 60))
        assert select(memory_db) == []

    def test_a_current_harness_recap_needs_no_generated_one(self, memory_db, tmp_path):
        entries = [*conversation(3), away_summary("s1", "a2", ago(45), "recap")]
        seed(memory_db, tmp_path, UUID_A, entries)
        assert select(memory_db) == []

    def test_a_recap_the_session_has_moved_past_is_regenerated(self, memory_db, tmp_path):
        session_id = seed(memory_db, tmp_path, UUID_A, conversation(3))
        recaps.write_success(memory_db, session_id, "old", ago(200), NOW)
        assert select(memory_db) == [UUID_A]

    def test_excluded_projects_are_never_selected_by_name_or_lossy_key(self, memory_db, tmp_path):
        seed(memory_db, tmp_path, UUID_A, conversation(3), project="-home-u-src-secret-client")
        assert select(memory_db, exclude_projects=["secret-client"]) == []

    def test_a_failed_session_cools_down_for_a_day(self, memory_db, tmp_path):
        session_id = seed(memory_db, tmp_path, UUID_A, conversation(3))
        recaps.write_failure(memory_db, session_id, "boom", NOW - TimeDelta(hours=23))
        assert select(memory_db) == []
        recaps.write_failure(memory_db, session_id, "boom", NOW - TimeDelta(hours=25))
        assert select(memory_db) == [UUID_A]

    def test_newest_first_and_limited(self, memory_db, tmp_path):
        seed(memory_db, tmp_path, UUID_A, conversation(3, end_minutes_ago=120))
        seed(memory_db, tmp_path, UUID_B, conversation(3, end_minutes_ago=60))
        assert select(memory_db) == [UUID_B, UUID_A]
        assert select(memory_db, limit=1) == [UUID_B]


class TestWrites:
    def test_a_failure_keeps_the_earlier_recap_and_a_success_clears_the_error(self, memory_db, tmp_path):
        session_id = seed(memory_db, tmp_path, UUID_A, conversation(3))
        recaps.write_success(memory_db, session_id, "first", ago(100), NOW)
        recaps.write_failure(memory_db, session_id, "boom", NOW)
        row = memory_db.execute("SELECT text, as_of, error FROM recaps").fetchone()
        assert row == ("first", ago(100), "boom")
        recaps.write_success(memory_db, session_id, "second", ago(60), NOW)
        row = memory_db.execute("SELECT text, as_of, error FROM recaps").fetchone()
        assert row == ("second", ago(60), None)


class TestCall:
    def test_call_options_isolate_the_call_and_leave_no_transcript(self):
        options = recaps.call_options()
        assert options.extra_args == {"no-session-persistence": None}
        assert options.cwd == str(config.RECAP_CALL_DIR)
        assert (options.tools, options.setting_sources, options.strict_mcp_config) == ([], [], True)
        assert (options.model, options.max_turns) == (recaps.RECAP_MODEL, 1)
        assert "ANTHROPIC_API_KEY" not in options.env

    @pytest.mark.parametrize(
        ("exc", "assistant_error", "status", "expected"),
        [
            (CLINotFoundError("missing"), None, None, run_history.REASON_CLAUDE_UNAVAILABLE),
            (None, "authentication_failed", None, run_history.REASON_CLAUDE_AUTH),
            (None, "rate_limit", None, run_history.REASON_RATE_LIMITED),
            (None, None, 429, run_history.REASON_RATE_LIMITED),
            (None, None, 503, run_history.REASON_API_UNAVAILABLE),
            (None, "invalid_request", 400, None),
            (TimeoutError(), None, None, None),
        ],
    )
    def test_classify_failure_separates_run_level_from_per_session(self, exc, assistant_error, status, expected):
        assert recaps.classify_failure(exc, assistant_error, status) == expected

    def fake_query(self, monkeypatch, messages):
        async def query(*, prompt, options):
            for message in messages:
                yield message

        monkeypatch.setattr(recaps, "query", query)

    def test_the_answer_is_trimmed_and_capped(self, monkeypatch):
        self.fake_query(monkeypatch, [AssistantMessage(content=[TextBlock(text="  " + "w" * 500)], model="m")])
        assert asyncio.run(recaps.generate_async("p")) == "w" * recaps.OUTPUT_CAP_CHARS

    def test_an_empty_answer_is_a_per_session_failure(self, monkeypatch):
        self.fake_query(monkeypatch, [AssistantMessage(content=[TextBlock(text="  ")], model="m")])
        with pytest.raises(recaps.RecapCallError) as raised:
            asyncio.run(recaps.generate_async("p"))
        assert raised.value.run_reason is None

    def test_a_call_past_the_timeout_is_a_per_session_failure(self, monkeypatch):
        async def query(*, prompt, options):
            await asyncio.sleep(5)
            yield AssistantMessage(content=[TextBlock(text="late")], model="m")

        monkeypatch.setattr(recaps, "query", query)
        monkeypatch.setattr(recaps, "CALL_TIMEOUT_SECONDS", 0.05)
        with pytest.raises(recaps.RecapCallError) as raised:
            asyncio.run(recaps.generate_async("p"))
        assert raised.value.run_reason is None
        assert "timed out" in str(raised.value)

    def test_an_auth_error_seen_before_the_sdk_raises_is_still_run_level(self, monkeypatch):
        async def query(*, prompt, options):
            yield AssistantMessage(content=[TextBlock(text="")], model="m", error="authentication_failed")
            raise RuntimeError("CLI exited 1")

        monkeypatch.setattr(recaps, "query", query)
        with pytest.raises(recaps.RecapCallError) as raised:
            asyncio.run(recaps.generate_async("p"))
        assert raised.value.run_reason == run_history.REASON_CLAUDE_AUTH

    def test_a_rate_limited_result_is_run_level(self, monkeypatch):
        result = ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="x",
            result="API Error: 429",
            api_error_status=429,
        )
        self.fake_query(monkeypatch, [result])
        with pytest.raises(recaps.RecapCallError) as raised:
            asyncio.run(recaps.generate_async("p"))
        assert raised.value.run_reason == run_history.REASON_RATE_LIMITED


class TestBackfillRun:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        """A file database and a projects dir holding one stopped, unimported session."""
        db = tmp_path / "conversations.db"
        projects = tmp_path / "projects"
        project_dir = projects / "-home-u-src-app"
        project_dir.mkdir(parents=True)
        now = Instant.now()

        def stopped(uuid: str) -> None:
            entries = []
            parent = None
            for i in range(3):
                ts = now - TimeDelta(minutes=120 - i * 2)
                entries.append(make_jsonl_entry(f"{uuid}-u{i}", parent, iso(ts), "user", f"question {i}"))
                entries.append(
                    make_jsonl_entry(f"{uuid}-a{i}", f"{uuid}-u{i}", iso(ts + TimeDelta(seconds=30)), "assistant", "ok")
                )
                parent = f"{uuid}-a{i}"
            write_jsonl(project_dir / f"{uuid}.jsonl", entries)

        stopped(UUID_A)
        return db, projects, stopped

    def run(self, db, projects, **kwargs):
        return backfill_recaps.run(db=db, projects_dir=projects, **kwargs)

    def test_imports_first_then_stores_the_recap(self, env, monkeypatch, capsys):
        db, projects, _stopped = env
        prompts = []
        monkeypatch.setattr(recaps, "generate", lambda prompt: prompts.append(prompt) or "Goal and next step.")
        assert self.run(db, projects, json_mode=True) == backfill_recaps.EXIT_OK
        assert json.loads(capsys.readouterr().out) == {
            "outcome": "ok",
            "reason": None,
            "selected": 1,
            "generated": 1,
            "failed": 0,
        }
        assert "question 0" in prompts[0]
        with get_connection({"db_path": str(db)}) as conn:
            [row] = recaps.read_recaps(conn, [UUID_A])
        assert row.recap.text == "Goal and next step."
        assert row.recap.current is True
        assert run_history.read_runs(db)[run_history.KIND_RECAPS]["last"]["outcome"] == "ok"

    def test_a_run_level_failure_stops_the_run_and_cools_nothing_down(self, env, monkeypatch, capsys):
        db, projects, stopped = env
        stopped(UUID_B)
        calls = []

        def fail(prompt):
            calls.append(prompt)
            raise recaps.RecapCallError("not logged in", run_history.REASON_CLAUDE_AUTH)

        monkeypatch.setattr(recaps, "generate", fail)
        assert self.run(db, projects, json_mode=True) == backfill_recaps.EXIT_FAILED
        assert len(calls) == 1
        assert json.loads(capsys.readouterr().out)["failed"] == 1
        with get_connection({"db_path": str(db)}) as conn:
            assert conn.execute("SELECT COUNT(*) FROM recaps").fetchone()[0] == 0
        last = run_history.read_runs(db)[run_history.KIND_RECAPS]["last"]
        assert (last["outcome"], last["reason"]) == ("failed", run_history.REASON_CLAUDE_AUTH)

    def test_a_per_session_failure_records_it_and_the_run_continues(self, env, monkeypatch):
        db, projects, stopped = env
        stopped(UUID_B)
        answers = iter([recaps.RecapCallError("bad answer"), "fine"])

        def answer(prompt):
            value = next(answers)
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(recaps, "generate", answer)
        assert self.run(db, projects) == backfill_recaps.EXIT_FAILED
        with get_connection({"db_path": str(db)}) as conn:
            rows = conn.execute("SELECT text, error FROM recaps ORDER BY session_id").fetchall()
        assert sorted(rows, key=str) == sorted([(None, "bad answer"), ("fine", None)], key=str)
        assert run_history.read_runs(db)[run_history.KIND_RECAPS]["last"]["outcome"] == "partial"
        ids = {finding.id for finding in diagnose(db, {"exclude_projects": []}).findings}
        assert "recaps-failed" in ids

    def test_three_per_session_failures_in_a_row_stop_the_run(self, env, monkeypatch):
        db, projects, stopped = env
        for uuid in ("cccccccc-3", "dddddddd-4", "eeeeeeee-5"):
            stopped(uuid)
        calls = []

        def fail(prompt):
            calls.append(prompt)
            raise recaps.RecapCallError("bad answer")

        monkeypatch.setattr(recaps, "generate", fail)
        assert self.run(db, projects) == backfill_recaps.EXIT_FAILED
        assert len(calls) == recaps.MAX_CONSECUTIVE_FAILURES
        last = run_history.read_runs(db)[run_history.KIND_RECAPS]["last"]
        assert last["reason"] == run_history.REASON_REPEATED_FAILURES


class TestImportSkipsRecapCalls:
    def test_the_recap_call_directory_is_never_imported(self, memory_db, tmp_path):
        project_dir = tmp_path / config.claude_project_dir_name(config.RECAP_CALL_DIR)
        project_dir.mkdir()
        write_jsonl(project_dir / f"{UUID_A}.jsonl", conversation(3))
        assert import_conversations.import_project(memory_db, project_dir) == (0, 0, 0)
        assert memory_db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert memory_db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0

    def test_the_directory_name_matches_claude_codes_encoding(self):
        assert config.claude_project_dir_name(Path("/home/u/.ccrecall/recap-calls")) == "-home-u--ccrecall-recap-calls"


class TestShowRecaps:
    def test_a_prefix_matching_several_sessions_returns_each(self, memory_db, tmp_path):
        seed(memory_db, tmp_path, "abcd0001-0000-4000-8000-000000000000", conversation(3))
        seed(memory_db, tmp_path, "abcd0002-0000-4000-8000-000000000000", conversation(3))
        rows = recaps.read_recaps(memory_db, ["abcd"])
        assert [row.session_id for row in rows] == [
            "abcd0001-0000-4000-8000-000000000000",
            "abcd0002-0000-4000-8000-000000000000",
        ]
        assert all(row.recap is None for row in rows)

    def test_json_rows_for_known_prefix_and_unknown_ids(self, tmp_path, capsys):
        db = tmp_path / "conversations.db"
        with get_connection({"db_path": str(db)}) as conn:
            entries = [*conversation(3), away_summary("s1", "a2", ago(45), "Harness recap.")]
            seed(conn, tmp_path, UUID_A, entries)
        with pytest.raises(SystemExit) as exited:
            app.meta(["--json", "show-recaps", UUID_A[:8], "ffffffff", "--db", str(db)])
        assert exited.value.code == 0
        assert json.loads(capsys.readouterr().out) == {
            "recaps": [
                {
                    "session_id": UUID_A,
                    "text": "Harness recap.",
                    "source": "claude_code",
                    "current": True,
                    "as_of": ago(45),
                },
                {"session_id": "ffffffff", "text": None, "source": None, "current": False, "as_of": None},
            ]
        }
