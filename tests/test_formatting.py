"""Tests for ccrecall.formatting — time formatting, project paths, session rendering."""

import json
import re
from pathlib import Path

from ccrecall.formatting import (
    MAX_FILES_DISPLAYED,
    build_envelope,
    extract_project_name,
    format_card_json,
    format_card_markdown,
    format_markdown_session,
    format_result_list_markdown,
    format_snippet_json,
    format_snippet_markdown,
    format_time,
    format_time_full,
    get_project_key,
    normalize_cwd,
    normalize_project_key,
    normalize_scores,
    parse_project_key,
)


class TestFormatTime:
    def test_valid_iso_timestamp(self):
        result = format_time("2025-01-15T14:30:00Z")
        # Should produce HH:MM in local timezone
        assert re.match(r"\d{2}:\d{2}$", result), f"Expected HH:MM format, got {result!r}"

    def test_none_returns_placeholder(self):
        assert format_time(None) == "??:??"

    def test_empty_string_returns_placeholder(self):
        assert format_time("") == "??:??"

    def test_malformed_string_fallback(self):
        result = format_time("not-a-timestamp")
        # Should return first 16 chars as fallback
        assert result == "not-a-timestamp"

    def test_custom_format(self):
        result = format_time("2025-01-15T14:30:00Z", "%Y-%m-%d")
        assert "2025" in result
        assert "01" in result
        assert "15" in result


class TestFormatTimeFull:
    def test_valid_timestamp(self):
        result = format_time_full("2025-01-15T14:30:00Z")
        # Should contain YYYY-MM-DD
        assert "2025" in result
        assert "01-15" in result


class TestProjectKey:
    def test_get_project_key(self):
        assert get_project_key("/Users/sam/project") == "-Users-sam-project"

    def test_get_project_key_with_dots(self):
        assert get_project_key("/home/user/.config") == "-home-user--config"

    def test_parse_project_key_roundtrip(self):
        """parse_project_key(get_project_key(path)) should roundtrip for paths without dashes."""
        original = "/Users/sam/project"
        key = get_project_key(original)
        reconstructed = parse_project_key(key)
        assert reconstructed == original, f"Expected exact roundtrip, got {reconstructed!r}"

    def test_parse_project_key_lossy_with_dashes(self):
        """parse_project_key is lossy for paths containing dashes (dashes become /)."""
        original = "/home/user/my-project"
        key = get_project_key(original)
        reconstructed = parse_project_key(key)
        # Dashes in "my-project" become "/" — this is a known limitation
        assert reconstructed != original, "Paths with dashes should NOT roundtrip"
        assert reconstructed.startswith("/")

    def test_get_project_key_worktree_resolves_to_base(self):
        """Worktree paths should resolve to the same key as the base repo."""
        base = "/Users/sam/repos/myproject"
        worktree = "/Users/sam/repos/myproject/.claude/worktrees/feature-branch"
        assert get_project_key(worktree) == get_project_key(base)

    def test_get_project_key_worktree_with_dots_in_name(self):
        """Worktree names with dots should still resolve correctly."""
        base = "/Users/sam/repos/myproject"
        worktree = "/Users/sam/repos/myproject/.claude/worktrees/fix.auth.bug"
        assert get_project_key(worktree) == get_project_key(base)

    def test_get_project_key_non_worktree_unchanged(self):
        """Regular paths should be unaffected by worktree resolution."""
        path = "/Users/sam/repos/myproject"
        assert get_project_key(path) == "-Users-sam-repos-myproject"

    def test_get_project_key_worktree_in_dotconfig(self):
        """A .claude/worktrees/ path under .config should still resolve."""
        base = "/home/user/.config/tool"
        worktree = "/home/user/.config/tool/.claude/worktrees/test-wt"
        assert get_project_key(worktree) == get_project_key(base)

    def test_normalize_project_key_strips_worktree(self):
        """Encoded worktree keys should normalize to base repo key."""
        base_key = "-Users-sam-repos-myproject"
        worktree_key = "-Users-sam-repos-myproject--claude-worktrees-feature-branch"
        assert normalize_project_key(worktree_key) == base_key

    def test_normalize_project_key_no_op_for_regular(self):
        """Regular keys should pass through unchanged."""
        key = "-Users-sam-repos-myproject"
        assert normalize_project_key(key) == key

    def test_normalize_matches_get_project_key(self):
        """normalize_project_key on encoded worktree should match get_project_key on base path."""
        base_path = "/Users/sam/repos/myproject"
        worktree_dir_name = "-Users-sam-repos-myproject--claude-worktrees-feat"
        assert normalize_project_key(worktree_dir_name) == get_project_key(base_path)

    def test_get_project_key_rfind_uses_last_marker(self):
        """If .claude/worktrees/ appears multiple times, rfind strips only the last one."""
        path = "/tmp/.claude/worktrees/repo/.claude/worktrees/feat"
        # rfind strips the last worktree suffix, leaving the first as part of the base path
        assert get_project_key(path) == "-tmp--claude-worktrees-repo"

    def test_normalize_project_key_rfind_uses_last_marker(self):
        """Encoded key with multiple worktree markers uses last occurrence."""
        key = "-tmp--claude-worktrees-repo--claude-worktrees-feat"
        assert normalize_project_key(key) == "-tmp--claude-worktrees-repo"

    def test_normalize_cwd_strips_worktree(self):
        """normalize_cwd returns base repo path from a worktree path."""
        wt = "/Users/sam/repos/myproject/.claude/worktrees/feat"
        assert normalize_cwd(wt) == "/Users/sam/repos/myproject"

    def test_normalize_cwd_noop_for_regular(self):
        """normalize_cwd passes through non-worktree paths unchanged."""
        path = "/Users/sam/repos/myproject"
        assert normalize_cwd(path) == path

    def test_normalize_cwd_project_name_is_base(self):
        """After normalize_cwd, Path().name should be the base repo name, not the worktree name."""
        wt = "/Users/sam/repos/myproject/.claude/worktrees/feat"
        assert Path(normalize_cwd(wt)).name == "myproject"

    def test_parse_project_key_adds_leading_slash(self):
        result = parse_project_key("-Users-sam-project")
        assert result.startswith("/")

    # ── Windows path support ──

    def test_get_project_key_windows_backslash(self):
        """Windows paths with backslashes should produce the same key format."""
        assert get_project_key("C:\\Users\\sam\\project") == "C--Users-sam-project"

    def test_get_project_key_windows_matches_posix_equivalent(self):
        """Windows forward-slash and backslash paths should produce identical keys."""
        assert get_project_key("C:/Users/sam/project") == get_project_key("C:\\Users\\sam\\project")

    def test_normalize_cwd_windows_worktree(self):
        """Windows worktree paths should resolve to base repo path."""
        wt = "C:\\Users\\sam\\repos\\myproject\\.claude\\worktrees\\feat"
        assert normalize_cwd(wt) == "C:/Users/sam/repos/myproject"

    def test_normalize_cwd_windows_non_worktree(self):
        """Windows non-worktree paths normalize backslashes to forward slashes."""
        assert normalize_cwd("C:\\Users\\sam\\repos\\proj") == "C:/Users/sam/repos/proj"

    def test_get_project_key_windows_worktree_resolves_to_base(self):
        """Windows worktree path should produce same key as base."""
        base = "C:\\Users\\sam\\repos\\myproject"
        worktree = "C:\\Users\\sam\\repos\\myproject\\.claude\\worktrees\\feat"
        assert get_project_key(worktree) == get_project_key(base)

    def test_parse_project_key_windows_drive_letter(self):
        """Keys from Windows paths should reconstruct with drive letter prefix."""
        result = parse_project_key("C--Users-sam-project")
        assert result == "C:/Users/sam/project"

    def test_round_trip_windows(self):
        """get_project_key → parse_project_key round-trip for Windows paths."""
        key = get_project_key("C:\\Users\\sam\\project")
        assert key == "C--Users-sam-project"
        assert parse_project_key(key) == "C:/Users/sam/project"

    def test_parse_project_key_unix_unchanged(self):
        """Unix keys should still reconstruct with leading /."""
        result = parse_project_key("-Users-sam-project")
        assert result == "/Users/sam/project"

    def test_extract_project_name(self):
        assert extract_project_name("/Users/sam/my-project") == "my-project"

    def test_extract_project_name_trailing_slash(self):
        # Path().name handles trailing slashes
        assert extract_project_name("/Users/sam/project") == "project"


class TestFormatMarkdownSession:
    def test_minimal_session(self):
        session = {
            "uuid": "abcdef12-3456-7890-abcd-ef1234567890",
            "project": "test-project",
            "started_at": "2025-01-15T14:30:00Z",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ],
        }
        md = format_markdown_session(session)
        assert "## test-project" in md
        assert "abcdef12" in md  # First 8 chars of UUID
        assert "**User:** Hello" in md
        assert "**Assistant:** Hi there" in md
        assert md.endswith("---\n")

    def test_verbose_with_files_and_commits(self):
        session = {
            "uuid": "abcdef12-3456-7890-abcd-ef1234567890",
            "project": "proj",
            "started_at": "2025-01-15T14:30:00Z",
            "git_branch": "feature-x",
            "files_modified": ["/a.py", "/b.py"],
            "commits": ["Fix bug"],
            "messages": [],
        }
        md = format_markdown_session(session, verbose=True)
        assert "Branch: feature-x" in md
        assert "### Files Modified" in md
        assert "`/a.py`" in md
        assert "### Commits" in md
        assert "Fix bug" in md

    def test_non_verbose_hides_files(self):
        session = {
            "uuid": "abcdef12",
            "project": "proj",
            "started_at": None,
            "files_modified": ["/a.py"],
            "commits": ["Fix"],
            "messages": [],
        }
        md = format_markdown_session(session, verbose=False)
        assert "### Files Modified" not in md
        assert "### Commits" not in md

    def test_many_files_truncated(self):
        session = {
            "uuid": "abcdef12",
            "project": "proj",
            "started_at": None,
            "files_modified": [f"/file{i}.py" for i in range(15)],
            "messages": [],
        }
        md = format_markdown_session(session, verbose=True)
        assert "...and 5 more" in md

    def test_notification_message_labeled(self):
        session = {
            "uuid": "abcdef12",
            "project": "proj",
            "started_at": None,
            "messages": [
                {"role": "user", "content": "Start research"},
                {"role": "assistant", "content": "Launching agents."},
                {
                    "role": "user",
                    "content": "<task-notification>result</task-notification>",
                    "is_notification": 1,
                },
                {"role": "assistant", "content": "Agent done."},
            ],
        }
        md = format_markdown_session(session)
        assert "**Subagent Result:**" in md
        assert "**User:** Start research" in md
        assert "**Assistant:** Launching agents." in md

    def test_non_notification_user_not_labeled_as_subagent(self):
        session = {
            "uuid": "abcdef12",
            "project": "proj",
            "started_at": None,
            "messages": [
                {"role": "user", "content": "Hello", "is_notification": 0},
            ],
        }
        md = format_markdown_session(session)
        assert "**User:** Hello" in md
        assert "Subagent Result" not in md

    def test_tool_content_appended_after_prose(self):
        session = {
            "uuid": "abcdef12",
            "project": "proj",
            "started_at": None,
            "messages": [
                {"role": "user", "content": "list the files"},
                {
                    "role": "assistant",
                    "content": "Here you go.",
                    "tool_content": "[Bash: ls -la]",
                },
            ],
        }
        md = format_markdown_session(session)
        assert "**Assistant:** Here you go.\n[Bash: ls -la]" in md

    def test_tool_only_turn_renders_tool_content(self):
        """Empty prose content with non-empty tool_content still renders the tool markers."""
        session = {
            "uuid": "abcdef12",
            "project": "proj",
            "started_at": None,
            "messages": [
                {"role": "user", "content": "list the files"},
                {"role": "assistant", "content": "", "tool_content": "[Bash: ls -la]"},
            ],
        }
        md = format_markdown_session(session)
        assert "[Bash: ls -la]" in md

    def test_missing_tool_content_key_omits_line(self):
        """Messages without a tool_content key render unchanged (backward compat)."""
        session = {
            "uuid": "abcdef12",
            "project": "proj",
            "started_at": None,
            "messages": [
                {"role": "assistant", "content": "No tools used."},
            ],
        }
        md = format_markdown_session(session)
        assert "**Assistant:** No tools used." in md


# Fixtures


def _make_card(score_raw=0.03, score=0.87, topic="some topic"):
    """Return a minimal valid Track A card dict."""
    return {
        "score": score,
        "score_raw": score_raw,
        "session_uuid": "ef098861-8904-4f1d-a368-4f806ba059d7",
        "handle": "ef098861",
        "project": "ccrecall",
        "git_branch": "review-format",
        "started_at": "2026-06-25T07:30:00Z",
        "ended_at": "2026-06-25T13:09:42Z",
        "topic": topic,
        "exchange_count": 41,
        "files_modified": ["src/ccrecall/search_conversations.py", "src/ccrecall/formatting.py"],
        "commits": ["chore(main): release 0.11.1"],
        "tool_counts": {"Read": 40, "Bash": 22},
    }


def _make_snippet(score_raw=0.84, score=0.91, matched_role="assistant", match_terms=None):
    """Return a minimal valid Track B snippet dict."""
    return {
        "score": score,
        "score_raw": score_raw,
        "session_uuid": "ef098861-8904-4f1d-a368-4f806ba059d7",
        "handle": "ef098861",
        "project": "ccrecall",
        "git_branch": "review-format",
        "exchange_index": 19,
        "matched_role": matched_role,
        "timestamp": "2026-06-25T13:02:11Z",
        "user": "does B need its own message-level index?",
        "assistant": "the existing messages_fts already covers message-level keyword search",
        "match_terms": match_terms if match_terms is not None else ["messages_fts", "snippet"],
    }


# Track A card — markdown


class TestFormatCardMarkdown:
    def test_score_in_heading(self):
        card = _make_card(score=0.87)
        md = format_card_markdown(card)
        assert md.startswith("## 0.87  ")

    def test_project_branch_date_in_heading(self):
        card = _make_card()
        md = format_card_markdown(card)
        assert "ccrecall" in md
        assert "review-format" in md
        assert "2026-06-25" in md

    def test_topic_line(self):
        card = _make_card(topic="redesign search result format")
        md = format_card_markdown(card)
        assert "Topic:  redesign search result format" in md

    def test_counts_line(self):
        card = _make_card()
        md = format_card_markdown(card)
        assert "41 exchanges" in md
        assert "2 files" in md
        assert "1 commits" in md

    def test_handle_and_tail_hint(self):
        card = _make_card()
        md = format_card_markdown(card)
        assert "Handle: ef098861" in md
        assert "→ ccrecall tail ef098861" in md

    def test_no_body_text_no_messages(self):
        """Card must never contain exchange/message body content."""
        card = _make_card()
        # Inject a messages field — card renderer must ignore it
        card_with_messages = {**card, "messages": [{"role": "user", "content": "secret content"}]}
        md = format_card_markdown(card_with_messages)
        assert "secret content" not in md
        assert "### Conversation" not in md

    def test_flat_size_short_vs_long_session(self):
        """Card size is independent of session length (flat, bounded by design)."""
        short = _make_card()
        long_session = {**_make_card(), "exchange_count": 500}
        md_short = format_card_markdown(short)
        md_long = format_card_markdown(long_session)
        # Both cards should be within a few bytes of each other (only count differs)
        assert abs(len(md_short) - len(md_long)) < 50

    def test_null_score_omits_score_prefix(self):
        """When score is None, the heading has no score prefix."""
        card = _make_card(score=None)
        md = format_card_markdown(card)
        first_line = md.split("\n")[0]
        assert first_line.startswith("## ccrecall")
        assert "None" not in first_line

    def test_verbose_expands_files(self):
        """verbose=True expands the files_modified list."""
        card = _make_card()
        md_verbose = format_card_markdown(card, verbose=True)
        assert "search_conversations.py" in md_verbose
        assert "formatting.py" in md_verbose

    def test_verbose_files_bounded(self):
        """verbose card caps the files list so a many-file session stays bounded."""
        card = _make_card()
        card["files_modified"] = [f"src/file_{i}.py" for i in range(50)]
        md_verbose = format_card_markdown(card, verbose=True)
        files_line = next(line for line in md_verbose.splitlines() if line.startswith("Files:"))
        # Only the last MAX_FILES_DISPLAYED files are listed, plus an "...and N more" tail.
        assert files_line.count("src/file_") == MAX_FILES_DISPLAYED
        assert "...and 40 more" in files_line

    def test_verbose_expands_commits(self):
        """verbose=True expands the commits list."""
        card = _make_card()
        md_verbose = format_card_markdown(card, verbose=True)
        assert "release 0.11.1" in md_verbose

    def test_verbose_expands_tool_counts(self):
        """verbose=True expands the tool_counts dict."""
        card = _make_card()
        md_verbose = format_card_markdown(card, verbose=True)
        assert "Read" in md_verbose
        assert "Bash" in md_verbose

    def test_non_verbose_hides_lists(self):
        """verbose=False (default) does not expand file/commit/tool lists."""
        card = _make_card()
        md = format_card_markdown(card, verbose=False)
        assert "search_conversations.py" not in md
        assert "release 0.11.1" not in md

    def test_uncached_branch_no_topic(self):
        """Card with topic=None renders gracefully with fallback topic, no crash."""
        card = _make_card(topic=None)
        md = format_card_markdown(card)
        assert "Topic:" in md
        assert "None" not in md

    def test_does_not_mutate_input(self):
        """Renderer does not modify the input dict."""
        card = _make_card()
        original_topic = card["topic"]
        format_card_markdown(card)
        assert card["topic"] == original_topic


# Track A card — JSON


class TestFormatCardJson:
    def test_all_required_fields_present(self):
        card = _make_card()
        obj = format_card_json(card)
        required = {
            "score",
            "score_raw",
            "session_uuid",
            "handle",
            "project",
            "git_branch",
            "started_at",
            "ended_at",
            "topic",
            "exchange_count",
            "files_modified",
            "commits",
            "tool_counts",
        }
        assert required.issubset(obj.keys())

    def test_no_extra_fields_beyond_contract(self):
        """JSON object must not carry fields outside the contract shape."""
        card = _make_card()
        # Add a field that's not part of the contract
        card_with_extra = {**card, "messages": [{"role": "user", "content": "body"}]}
        obj = format_card_json(card_with_extra)
        assert "messages" not in obj

    def test_exchange_count_none_matches_markdown_zero(self):
        """A None exchange_count (uncached degrade path) renders 0 in both markdown and JSON.

        dict.get(key, default) does not substitute the default for a present None,
        so JSON must coerce None->0 to keep parity with markdown's "0 exchanges".
        """
        card = _make_card()
        card["exchange_count"] = None
        md = format_card_markdown(card)
        obj = format_card_json(card)
        assert "0 exchanges" in md
        assert obj["exchange_count"] == 0

    def test_files_modified_is_list(self):
        obj = format_card_json(_make_card())
        assert isinstance(obj["files_modified"], list)

    def test_commits_is_list(self):
        obj = format_card_json(_make_card())
        assert isinstance(obj["commits"], list)

    def test_tool_counts_is_dict(self):
        obj = format_card_json(_make_card())
        assert isinstance(obj["tool_counts"], dict)

    def test_json_serializable(self):
        """JSON object must serialize without error."""
        obj = format_card_json(_make_card())
        serialized = json.dumps(obj)
        assert json.loads(serialized)["handle"] == "ef098861"

    def test_score_null_preserved(self):
        """score=None is preserved in JSON form."""
        card = _make_card(score=None)
        obj = format_card_json(card)
        assert obj["score"] is None

    def test_does_not_mutate_input(self):
        card = _make_card()
        orig_files = list(card["files_modified"])
        format_card_json(card)
        assert card["files_modified"] == orig_files


# Track B snippet — markdown


class TestFormatSnippetMarkdown:
    def test_score_prefix(self):
        snippet = _make_snippet(score=0.91)
        md = format_snippet_markdown(snippet)
        assert md.startswith("0.91  ")

    def test_locator_parts(self):
        snippet = _make_snippet()
        md = format_snippet_markdown(snippet)
        assert "ccrecall/review-format" in md
        assert "ef098861" in md
        assert "exchange 19" in md

    def test_user_line(self):
        snippet = _make_snippet()
        md = format_snippet_markdown(snippet)
        assert "  User: does B need its own message-level index?" in md

    def test_asst_line(self):
        snippet = _make_snippet()
        md = format_snippet_markdown(snippet)
        assert "  Asst: the existing messages_fts already covers" in md

    def test_tail_hint(self):
        snippet = _make_snippet()
        md = format_snippet_markdown(snippet)
        assert "→ ccrecall tail ef098861" in md

    def test_null_score_omits_score_prefix(self):
        snippet = _make_snippet(score=None)
        md = format_snippet_markdown(snippet)
        first_line = md.split("\n")[0]
        assert not first_line.startswith("None")
        assert "ccrecall/review-format" in first_line

    def test_vector_path_null_matched_role_accepted(self):
        """matched_role=None (vector path) renders without error."""
        snippet = _make_snippet(matched_role=None)
        md = format_snippet_markdown(snippet)
        assert "ccrecall/review-format" in md

    def test_vector_path_empty_match_terms_accepted(self):
        """match_terms=[] (vector path) renders without error."""
        snippet = _make_snippet(match_terms=[])
        md = format_snippet_markdown(snippet)
        assert "User:" in md

    def test_no_full_transcript(self):
        """Snippet must not inline a full session — only the bounded exchange."""
        snippet = _make_snippet()
        md = format_snippet_markdown(snippet)
        # The snippet user text is bounded — no unrelated messages
        assert "### Conversation" not in md

    def test_does_not_mutate_input(self):
        snippet = _make_snippet()
        orig_user = snippet["user"]
        format_snippet_markdown(snippet)
        assert snippet["user"] == orig_user


# Track B snippet — JSON


class TestFormatSnippetJson:
    def test_all_required_fields_present(self):
        snippet = _make_snippet()
        obj = format_snippet_json(snippet)
        required = {
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
        assert required.issubset(obj.keys())

    def test_match_terms_is_list(self):
        obj = format_snippet_json(_make_snippet())
        assert isinstance(obj["match_terms"], list)

    def test_vector_path_null_matched_role(self):
        """matched_role=None is preserved in JSON output."""
        obj = format_snippet_json(_make_snippet(matched_role=None))
        assert obj["matched_role"] is None

    def test_vector_path_empty_match_terms(self):
        """match_terms=[] is preserved in JSON output."""
        obj = format_snippet_json(_make_snippet(match_terms=[]))
        assert obj["match_terms"] == []

    def test_json_serializable(self):
        obj = format_snippet_json(_make_snippet())
        serialized = json.dumps(obj)
        assert json.loads(serialized)["exchange_index"] == 19

    def test_does_not_mutate_input(self):
        snippet = _make_snippet()
        orig_terms = list(snippet["match_terms"])
        format_snippet_json(snippet)
        assert snippet["match_terms"] == orig_terms


# Score normalization


class TestNormalizeScores:
    def test_multi_result_min_max_two_decimals(self):
        """Multi-result: score is min-max normalized to two decimal places."""
        results = [
            {"score_raw": 0.04},
            {"score_raw": 0.02},
            {"score_raw": 0.03},
        ]
        normed = normalize_scores(results)
        scores = [r["score"] for r in normed]
        # All scores should be in [0.0, 1.0]
        assert all(0.0 <= s <= 1.0 for s in scores)
        # Max raw gets score=1.0, min raw gets score=0.0
        assert max(scores) == 1.0
        assert min(scores) == 0.0
        # Two decimal places
        for s in scores:
            assert s == round(s, 2)

    def test_single_result_score_null(self):
        """Single-result: score is None (degenerate normalization)."""
        results = [{"score_raw": 0.03}]
        normed = normalize_scores(results)
        assert normed[0]["score"] is None

    def test_single_result_score_raw_preserved(self):
        """Single-result: score_raw is still present."""
        results = [{"score_raw": 0.03}]
        normed = normalize_scores(results)
        assert normed[0]["score_raw"] == 0.03

    def test_empty_returns_empty(self):
        assert normalize_scores([]) == []

    def test_preserves_other_fields(self):
        """Normalization does not drop other fields from each result."""
        results = [{"score_raw": 0.04, "handle": "abc"}, {"score_raw": 0.02, "handle": "def"}]
        normed = normalize_scores(results)
        assert normed[0]["handle"] == "abc"
        assert normed[1]["handle"] == "def"

    def test_does_not_mutate_input(self):
        """Input dicts are not modified."""
        results = [{"score_raw": 0.04, "x": 1}, {"score_raw": 0.02, "x": 2}]
        original_keys = set(results[0].keys())
        normalize_scores(results)
        assert set(results[0].keys()) == original_keys
        assert "score" not in results[0]

    def test_degenerate_equal_scores_null(self):
        """Multiple results with equal score_raw → score is None (0/0 degenerate)."""
        results = [{"score_raw": 0.03}, {"score_raw": 0.03}]
        normed = normalize_scores(results)
        for r in normed:
            assert r["score"] is None


# Envelope builder


class TestBuildEnvelope:
    def test_ranked_envelope_fields(self):
        results = [_make_card(score_raw=0.04), _make_card(score_raw=0.02)]
        envelope = build_envelope("test query", ranked=True, results=results)
        assert envelope["query"] == "test query"
        assert envelope["ranked"] is True
        assert envelope["count"] == 2
        assert len(envelope["results"]) == 2

    def test_unranked_envelope_null_scores(self):
        """ranked=False → all score and score_raw set to None."""
        results = [_make_card(score_raw=0.04), _make_card(score_raw=0.02)]
        envelope = build_envelope("test query", ranked=False, results=results)
        assert envelope["ranked"] is False
        for r in envelope["results"]:
            assert r["score"] is None
            assert r["score_raw"] is None

    def test_ranked_envelope_has_normalized_scores(self):
        """ranked=True envelope contains results with normalized score."""
        results = [
            {**_make_card(), "score_raw": 0.04},
            {**_make_card(), "score_raw": 0.02},
        ]
        envelope = build_envelope("q", ranked=True, results=results)
        scores = [r["score"] for r in envelope["results"]]
        assert 1.0 in scores  # highest raw gets 1.0
        assert 0.0 in scores  # lowest raw gets 0.0

    def test_count_matches_results_length(self):
        results = [_make_card(), _make_card(), _make_card()]
        envelope = build_envelope("q", ranked=True, results=results)
        assert envelope["count"] == len(envelope["results"]) == 3

    def test_json_serializable(self):
        envelope = build_envelope("q", ranked=True, results=[_make_card()])
        serialized = json.dumps(envelope)
        parsed = json.loads(serialized)
        assert parsed["count"] == 1


# Markdown result list with unranked marker


class TestFormatResultListMarkdown:
    def test_ranked_no_marker(self):
        """Ranked result list has no unranked marker."""
        md = format_result_list_markdown(ranked=True, result_markdowns=["## card1", "## card2"])
        assert "keyword fallback" not in md
        assert "## card1" in md

    def test_unranked_marker_line(self):
        """Unranked result list prepends the marker line."""
        md = format_result_list_markdown(ranked=False, result_markdowns=["## card1"])
        assert "(keyword fallback — unranked, ordered by recency)" in md

    def test_unranked_marker_before_results(self):
        """The unranked marker appears before the first result."""
        md = format_result_list_markdown(ranked=False, result_markdowns=["## card1"])
        marker_pos = md.index("keyword fallback")
        card_pos = md.index("## card1")
        assert marker_pos < card_pos

    def test_empty_results(self):
        """Empty result list with ranked=True returns empty string."""
        md = format_result_list_markdown(ranked=True, result_markdowns=[])
        assert md == ""

    def test_unranked_empty_results(self):
        """Empty result list with ranked=False still includes marker."""
        md = format_result_list_markdown(ranked=False, result_markdowns=[])
        assert "keyword fallback" in md
