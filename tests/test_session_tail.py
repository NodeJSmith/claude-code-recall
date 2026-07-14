"""Tests for ccrecall.session_tail — prior-session tail recovery."""

import json
import os

from ccrecall.session_tail import (
    _build_search_dirs,
    _emit_full,
    _last_event_timestamp,
    _resolve_across_dirs,
    build_tail,
    emit,
    find_pending_question,
    format_pending_block,
    last_typed_instruction,
    list_transcripts,
    load_tail_entries,
    resolve_target,
    resolve_target_global,
    transcript_dir,
    typed_instruction,
)

# entry builders (mirror the real transcript shapes)

_counter = [0]


def _uuid() -> str:
    _counter[0] += 1
    return f"uuid-{_counter[0]:04d}"


def user_text(text: str, sidechain: bool = False) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": _uuid(),
        "isSidechain": sidechain,
    }


def user_tool_result(tool_id: str, content) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}],
        },
        "uuid": _uuid(),
    }


def assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "uuid": _uuid(),
    }


def ask_question(tool_id: str, question: str, options, sidechain: bool = False) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "AskUserQuestion",
                    "input": {"questions": [{"question": question, "options": options}]},
                }
            ],
        },
        "uuid": _uuid(),
        "isSidechain": sidechain,
    }


OPTS = [
    {"label": "Ship it", "description": "commit and PR"},
    {"label": "Wait", "description": "hold"},
]
ANSWERED = 'Your questions have been answered: "How do you want to proceed?"="Ship it"'
REJECTED = "The user doesn't want to proceed with this tool use. The tool use was rejected."
INTERRUPT = "[Request interrupted by user for tool use]"


class TestFindPendingQuestion:
    def test_asked_no_result_is_pending(self):
        entries = [user_text("do the thing"), ask_question("t1", "proceed?", OPTS)]
        payload = find_pending_question(entries)
        assert payload is not None
        assert payload["questions"][0]["question"] == "proceed?"

    def test_asked_then_rejected_is_pending(self):
        entries = [
            ask_question("t1", "proceed?", OPTS),
            user_tool_result("t1", REJECTED),
        ]
        assert find_pending_question(entries) is not None

    def test_asked_then_interrupted_is_pending(self):
        entries = [
            ask_question("t1", "proceed?", OPTS),
            user_tool_result("t1", INTERRUPT),
        ]
        assert find_pending_question(entries) is not None

    def test_asked_then_answered_is_not_pending(self):
        entries = [
            ask_question("t1", "proceed?", OPTS),
            user_tool_result("t1", ANSWERED),
        ]
        assert find_pending_question(entries) is None

    def test_no_question_is_not_pending(self):
        entries = [user_text("hi"), assistant_text("hello")]
        assert find_pending_question(entries) is None

    def test_sidechain_question_ignored(self):
        entries = [ask_question("t1", "subagent ask?", OPTS, sidechain=True)]
        assert find_pending_question(entries) is None

    def test_only_last_question_matters(self):
        # Earlier question answered, later one left open -> pending on the later.
        entries = [
            ask_question("t1", "first?", OPTS),
            user_tool_result("t1", ANSWERED),
            ask_question("t2", "second?", OPTS),
        ]
        payload = find_pending_question(entries)
        assert payload["questions"][0]["question"] == "second?"

    def test_last_answered_after_earlier_open(self):
        # Later question answered -> not pending even if an earlier one looked open.
        entries = [
            ask_question("t1", "first?", OPTS),
            ask_question("t2", "second?", OPTS),
            user_tool_result("t2", ANSWERED),
        ]
        assert find_pending_question(entries) is None

    def test_result_as_list_content(self):
        # Real transcripts sometimes store tool_result content as a list of blocks;
        # the answer marker must be read from the extracted text, not str(list).
        listed = [{"type": "text", "text": ANSWERED}]
        entries = [ask_question("t1", "proceed?", OPTS), user_tool_result("t1", listed)]
        assert find_pending_question(entries) is None

    def test_list_result_without_marker_is_pending(self):
        # A non-answer delivered as list-of-blocks must not be read as answered.
        listed = [{"type": "text", "text": "user chose to stop"}]
        entries = [ask_question("t1", "proceed?", OPTS), user_tool_result("t1", listed)]
        assert find_pending_question(entries) is not None


class TestTypedInstruction:
    def test_real_text(self):
        assert typed_instruction(user_text("ship the feature")) == "ship the feature"

    def test_tool_result_filtered(self):
        assert typed_instruction(user_tool_result("t1", "output")) is None

    def test_interrupt_filtered(self):
        assert typed_instruction(user_text(INTERRUPT)) is None

    def test_task_notification_filtered(self):
        assert typed_instruction(user_text("<task-notification>\ndone\n</task-notification>")) is None

    def test_system_reminder_filtered(self):
        assert typed_instruction(user_text("<system-reminder>be good</system-reminder>")) is None

    def test_skill_body_filtered(self):
        assert typed_instruction(user_text("Base directory for this skill: /x\n# Foo")) is None

    def test_command_wrapper_stripped_to_empty(self):
        entry = user_text("<command-message>x</command-message><command-name>/x</command-name>")
        assert typed_instruction(entry) is None

    def test_last_typed_instruction_skips_trailing_noise(self):
        entries = [
            user_text("the real instruction"),
            assistant_text("working"),
            user_tool_result("t1", "out"),
            user_text("<task-notification>done</task-notification>"),
        ]
        assert last_typed_instruction(entries) == "the real instruction"


class TestBuildTail:
    def test_orders_and_limits(self):
        entries = [
            user_text("u1"),
            assistant_text("a1"),
            ask_question("t1", "q?", OPTS),
            user_tool_result("t1", "out"),
        ]
        tail = build_tail(entries, 8)
        assert tail[0] == ("user", "u1")
        assert tail[1] == ("assistant", "a1")
        assert ("tool", "AskUserQuestion") in tail

    def test_respects_k(self):
        entries = [user_text(f"u{i}") for i in range(20)]
        assert len(build_tail(entries, 5)) == 5

    def test_non_positive_k_returns_empty(self):
        entries = [user_text("u1"), assistant_text("a1")]
        assert build_tail(entries, 0) == []
        assert build_tail(entries, -1) == []


class TestTranscriptDir:
    def test_worktree_path_not_normalized(self):
        # The transcript lives in the RAW-cwd dir, worktree segment included.
        cwd = "/home/j/source/hassette/.claude/worktrees/959"
        assert transcript_dir(cwd).name == "-home-j-source-hassette--claude-worktrees-959"

    def test_plain_path(self):
        assert transcript_dir("/home/j/repo").name == "-home-j-repo"

    def test_dots_become_dashes(self):
        assert transcript_dir("/home/j/.config/app").name == "-home-j--config-app"


class TestResolveTarget:
    def _write(self, path, stem, timestamp=None):
        f = path / f"{stem}.jsonl"
        entry = {"type": "user", "uuid": "u", "message": {}}
        if timestamp:
            entry["timestamp"] = timestamp
        f.write_text(json.dumps(entry) + "\n")
        return f

    def test_picks_second_newest(self, tmp_path):
        # mtimes are set OPPOSITE to the JSONL timestamps — proves ordering
        # follows the timestamp field, not the filesystem mtime (issue #45).
        a = self._write(tmp_path, "a", timestamp="2024-01-01T00:00:00Z")  # earlier event, newer mtime
        b = self._write(tmp_path, "b", timestamp="2024-06-01T00:00:00Z")  # later event, older mtime
        os.utime(a, (2000, 2000))
        os.utime(b, (1000, 1000))
        # b has the latest timestamp, so it's treated as the current/live
        # session; a (earlier timestamp) is the prior session returned.
        assert resolve_target(tmp_path, None) == a

    def test_selector_substring(self, tmp_path):
        self._write(tmp_path, "aaaa-1111")
        self._write(tmp_path, "bbbb-2222")
        assert resolve_target(tmp_path, "bbbb").stem == "bbbb-2222"

    def test_single_session_returns_none(self, tmp_path):
        self._write(tmp_path, "only")
        assert resolve_target(tmp_path, None) is None


class TestResolveTargetGlobal:
    def _write(self, path, stem, timestamp=None):
        path.mkdir(parents=True, exist_ok=True)
        f = path / f"{stem}.jsonl"
        entry = {"type": "user", "uuid": "u", "message": {}}
        if timestamp:
            entry["timestamp"] = timestamp
        f.write_text(json.dumps(entry) + "\n")
        return f

    def test_finds_session_in_other_project(self, tmp_path):
        proj_a = tmp_path / "project-a"
        proj_b = tmp_path / "project-b"
        self._write(proj_a, "aaaa-1111")
        self._write(proj_b, "bbbb-2222")
        result = resolve_target_global("bbbb", projects_dir=tmp_path)
        assert result is not None
        assert result.stem == "bbbb-2222"

    def test_no_match_returns_none(self, tmp_path):
        proj = tmp_path / "project-a"
        self._write(proj, "aaaa-1111")
        assert resolve_target_global("zzzz", projects_dir=tmp_path) is None

    def test_picks_newest_when_ambiguous(self, tmp_path):
        proj_a = tmp_path / "project-a"
        proj_b = tmp_path / "project-b"
        self._write(proj_a, "abc-older", timestamp="2024-01-01T00:00:00Z")
        self._write(proj_b, "abc-newer", timestamp="2024-06-01T00:00:00Z")
        result = resolve_target_global("abc", projects_dir=tmp_path)
        assert result is not None
        assert result.stem == "abc-newer"

    def test_skips_non_directories(self, tmp_path):
        (tmp_path / "not-a-dir.txt").write_text("hi")
        proj = tmp_path / "project-a"
        self._write(proj, "aaaa-1111")
        result = resolve_target_global("aaaa", projects_dir=tmp_path)
        assert result is not None

    def test_missing_projects_dir_returns_none(self, tmp_path):
        assert resolve_target_global("anything", projects_dir=tmp_path / "nope") is None


class TestLastEventTimestampFallback:
    def test_no_parseable_timestamp_falls_back_to_mtime(self, tmp_path):
        # Neither entry has a "timestamp" field at all, so the mtime fallback
        # must produce an ISO string that participates correctly in ordering.
        no_ts = tmp_path / "no-ts.jsonl"
        no_ts.write_text(json.dumps({"type": "something"}) + "\n" + json.dumps({"type": "other"}) + "\n")
        has_ts = tmp_path / "has-ts.jsonl"
        has_ts.write_text(json.dumps({"type": "user", "timestamp": "2024-01-01T00:00:00Z"}) + "\n")

        # Give the no-timestamp file a newer mtime than the timestamped file's
        # actual event time, so it should sort first (newest first).
        os.utime(no_ts, (2000000000, 2000000000))
        os.utime(has_ts, (1000000000, 1000000000))

        files = list_transcripts(tmp_path)
        assert files[0] == no_ts
        assert files[1] == has_ts

        # The fallback value itself is a valid ISO 8601 string derived from mtime.
        fallback = _last_event_timestamp(no_ts)
        assert fallback.startswith("2033-")

    def test_corrupt_json_lines_fall_back_to_mtime_without_crashing(self, tmp_path):
        f = tmp_path / "corrupt.jsonl"
        # Last ~20 lines are malformed JSON — _last_event_timestamp only scans
        # the tail window, so a fully-corrupt tail must not crash and must
        # still fall back to mtime.
        lines = ["not valid json {{{" for _ in range(20)]
        f.write_text("\n".join(lines) + "\n")
        os.utime(f, (1700000000, 1700000000))

        result = _last_event_timestamp(f)

        assert result.startswith("2023-")


class TestLoadTailEntries:
    def test_reads_only_tail(self, tmp_path):
        f = tmp_path / "s.jsonl"
        lines = [json.dumps({"uuid": f"u{i}", "type": "user", "message": {}}) for i in range(50)]
        f.write_text("\n".join(lines) + "\n")
        entries = load_tail_entries(f, tail_lines=10)
        assert len(entries) == 10
        assert entries[-1]["uuid"] == "u49"

    def test_skips_lines_without_uuid(self, tmp_path):
        f = tmp_path / "s.jsonl"
        f.write_text(
            json.dumps({"type": "file-history-snapshot"})
            + "\n"
            + json.dumps({"uuid": "u1", "type": "user", "message": {}})
            + "\n"
        )
        assert [e["uuid"] for e in load_tail_entries(f, tail_lines=10)] == ["u1"]


class TestEmitFull:
    def test_prints_unclipped_content(self, capsys):
        long_text = "x" * 2000
        long_asst = "y" * 2000
        entries = [user_text(long_text), assistant_text(long_asst)]
        _emit_full(entries, pending=None)
        out = capsys.readouterr().out
        assert long_text in out
        assert long_asst in out
        assert "[…]" not in out

    def test_full_omits_assistant_when_pending(self, capsys):
        entries = [
            user_text("do the thing"),
            assistant_text("here is my answer"),
            ask_question("t1", "proceed?", OPTS),
        ]
        pending = find_pending_question(entries)
        _emit_full(entries, pending=pending)
        out = capsys.readouterr().out
        assert "do the thing" in out
        assert "LAST ASSISTANT MESSAGE" not in out

    def test_emit_full_flag_skips_tail_events(self, tmp_path, capsys):
        path = tmp_path / "s.jsonl"
        entries = [user_text("instruct me"), assistant_text("z" * 2000)]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        code = emit(path, k=8, full=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "TAIL (last" not in out
        assert "LAST ASSISTANT MESSAGE:" in out


class TestFormatPendingBlock:
    def test_injection_format_has_question_and_options(self):
        payload = {"questions": [{"question": "proceed?", "options": OPTS}]}
        out = format_pending_block(payload, for_injection=True)
        assert "Unresolved Decision" in out
        assert "proceed?" in out
        assert "Ship it" in out

    def test_cli_format_numbers_options(self):
        payload = {"questions": [{"question": "proceed?", "options": OPTS}]}
        out = format_pending_block(payload)
        assert "PENDING QUESTION" in out
        assert "1. Ship it" in out


class TestBuildSearchDirs:
    def test_not_in_worktree_returns_provided_cwd(self):
        dirs = _build_search_dirs("/home/user/repo", real_cwd="/home/user/repo")
        assert len(dirs) == 1
        assert dirs[0] == transcript_dir("/home/user/repo")

    def test_in_worktree_returns_worktree_first(self):
        wt = "/home/user/repo/.claude/worktrees/billing"
        dirs = _build_search_dirs(wt, real_cwd=wt)
        assert len(dirs) == 2
        assert dirs[0] == transcript_dir(wt)
        assert dirs[1] == transcript_dir("/home/user/repo")

    def test_cwd_is_repo_root_but_in_worktree(self, capsys):
        wt = "/home/user/repo/.claude/worktrees/billing"
        dirs = _build_search_dirs("/home/user/repo", real_cwd=wt)
        assert len(dirs) == 2
        assert dirs[0] == transcript_dir(wt)
        assert dirs[1] == transcript_dir("/home/user/repo")
        err = capsys.readouterr().err
        assert "running in worktree" in err

    def test_unrelated_cwd_skips_worktree_logic(self):
        wt = "/home/user/repo/.claude/worktrees/billing"
        dirs = _build_search_dirs("/other/project", real_cwd=wt)
        assert len(dirs) == 1
        assert dirs[0] == transcript_dir("/other/project")

    def test_sibling_worktree_cwd_searched_first(self):
        wt_a = "/home/user/repo/.claude/worktrees/billing"
        wt_b = "/home/user/repo/.claude/worktrees/genie"
        dirs = _build_search_dirs(wt_b, real_cwd=wt_a)
        assert dirs[0] == transcript_dir(wt_b)
        assert dirs[1] == transcript_dir("/home/user/repo")
        assert len(dirs) == 2


class TestResolveAcrossDirs:
    def _write_transcript(self, pdir, stem, ts):
        pdir.mkdir(parents=True, exist_ok=True)
        path = pdir / f"{stem}.jsonl"
        path.write_text(json.dumps({"timestamp": ts}) + "\n")
        return path

    def test_skips_newest_in_first_dir_only(self, tmp_path):
        dir1 = tmp_path / "primary"
        dir2 = tmp_path / "fallback"
        self._write_transcript(dir1, "current", "2026-07-13T10:00:00Z")
        prior = self._write_transcript(dir1, "prior", "2026-07-13T09:00:00Z")

        result = _resolve_across_dirs([dir1, dir2], None)
        assert result == prior

    def test_fallback_dir_returns_newest(self, tmp_path):
        dir1 = tmp_path / "primary"
        dir2 = tmp_path / "fallback"
        dir1.mkdir(parents=True)
        newest = self._write_transcript(dir2, "only-one", "2026-07-13T09:00:00Z")

        result = _resolve_across_dirs([dir1, dir2], None)
        assert result == newest

    def test_selector_matches_across_dirs(self, tmp_path):
        dir1 = tmp_path / "primary"
        dir2 = tmp_path / "fallback"
        dir1.mkdir(parents=True)
        target = self._write_transcript(dir2, "abc12345-full-uuid", "2026-07-13T09:00:00Z")

        result = _resolve_across_dirs([dir1, dir2], "abc12345")
        assert result == target
