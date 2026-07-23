"""Tests for ccrecall.content — message content extraction and tool detection."""

import json

from ccrecall.content import (
    _MAX_EXTRACT_ITEMS,
    TOOL_CONTENT_CAP,
    TOOL_FIELD_CAP,
    extract_commits,
    extract_files_modified,
    extract_text_content,
    is_task_notification,
    is_teammate_message,
    is_tool_result,
    parse_origin,
)

# extract_text_content


class TestExtractTextContent:
    def test_plain_string(self):
        text, has_tool, has_think, summary, tool_content = extract_text_content("Hello world")
        assert text == "Hello world"
        assert has_tool is False
        assert has_think is False
        assert summary is None
        assert tool_content == ""

    def test_string_with_command_artifacts(self):
        raw = "prefix <command-name>foo</command-name> middle <command-args>bar</command-args> end"
        text, _, _, _, _ = extract_text_content(raw)
        assert "<command-name>" not in text
        assert "<command-args>" not in text
        assert "prefix" in text
        assert "middle" in text
        assert "end" in text

    def test_string_with_local_command_stdout(self):
        raw = "before <local-command-stdout>some output</local-command-stdout> after"
        text, _, _, _, _ = extract_text_content(raw)
        assert "<local-command-stdout>" not in text
        assert "before" in text
        assert "after" in text

    def test_string_with_command_message(self):
        raw = "start <command-message>msg content</command-message> finish"
        text, _, _, _, _ = extract_text_content(raw)
        assert "<command-message>" not in text

    def test_list_with_text_blocks(self):
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": "World"},
        ]
        text, has_tool, has_think, summary, tool_content = extract_text_content(content)
        assert text == "Hello\nWorld"
        assert has_tool is False
        assert has_think is False
        assert summary is None
        assert tool_content == ""

    def test_list_with_tool_use(self):
        content = [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "name": "Read", "input": {"file": "test.py"}},
            {"type": "tool_use", "name": "Read", "input": {"file": "other.py"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]
        text, has_tool, _has_think, summary, tool_content = extract_text_content(content)
        assert text == "Let me check."
        assert has_tool is True
        assert summary is not None
        counts = json.loads(summary)
        assert counts == {"Read": 2, "Bash": 1}
        assert tool_content == "[Read: test.py]\n[Read: other.py]\n[Bash: ls]"

    def test_list_with_thinking(self):
        content = [
            {"type": "thinking", "thinking": "Let me reason..."},
            {"type": "text", "text": "The answer is 42."},
        ]
        text, has_tool, has_think, summary, tool_content = extract_text_content(content)
        assert text == "The answer is 42."
        assert has_tool is False
        assert has_think is True
        assert summary is None
        assert tool_content == ""

    def test_list_with_all_types(self):
        content = [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Here's what I found."},
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "foo"}},
        ]
        text, has_tool, has_think, summary, tool_content = extract_text_content(content)
        assert text == "Here's what I found."
        assert has_tool is True
        assert has_think is True
        assert json.loads(summary) == {"Grep": 1}
        assert tool_content == "[Grep: foo]"

    def test_list_with_tool_use_no_name(self):
        """tool_use without a name should still flag has_tool_use but not appear in summary."""
        content = [{"type": "tool_use", "input": {}}]
        _, has_tool, _, summary, tool_content = extract_text_content(content)
        assert has_tool is True
        assert summary is None  # No tool name -> no counts -> None
        assert tool_content == "[]"  # empty name, no input fields

    def test_empty_string(self):
        text, _has_tool, _has_think, summary, tool_content = extract_text_content("")
        assert text == ""
        assert summary is None
        assert tool_content == ""

    def test_none_input(self):
        text, has_tool, has_think, summary, tool_content = extract_text_content(None)
        assert text == ""
        assert has_tool is False
        assert has_think is False
        assert summary is None
        assert tool_content == ""

    def test_unexpected_type(self):
        text, has_tool, _has_think, _summary, tool_content = extract_text_content(42)
        assert text == ""
        assert has_tool is False
        assert tool_content == ""

    def test_empty_list(self):
        text, has_tool, _has_think, summary, tool_content = extract_text_content([])
        assert text == ""
        assert has_tool is False
        assert summary is None
        assert tool_content == ""


# extract_text_content — tool_content extraction (generic field-join)


class TestToolContentExtraction:
    def test_bash_with_description(self):
        content = [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "npm install", "description": "Install dependencies"},
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Bash: npm install Install dependencies]"

    def test_bash_without_description(self):
        content = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Bash: ls -la]"

    def test_multiline_command_produces_single_line_marker(self):
        content = [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "echo hi\necho bye"},
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Bash: echo hi echo bye]"
        assert "\n" not in tool_content

    def test_ask_user_question(self):
        content = [
            {
                "type": "tool_use",
                "name": "AskUserQuestion",
                "input": {
                    "questions": [
                        {
                            "question": "What would you like to do?",
                            "options": [
                                {"label": "Approve", "description": "Approve as-is"},
                                {"label": "Revise", "description": "Revise the plan"},
                            ],
                        }
                    ]
                },
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == (
            "[AskUserQuestion: What would you like to do? Approve Approve as-is Revise Revise the plan]"
        )

    def test_agent(self):
        content = [
            {
                "type": "tool_use",
                "name": "Agent",
                "input": {"subagent_type": "researcher", "prompt": "Investigate the proposed change"},
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Agent: researcher Investigate the proposed change]"

    def test_skill(self):
        content = [
            {
                "type": "tool_use",
                "name": "Skill",
                "input": {"skill": "mine-define", "args": "add rate limiting"},
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Skill: mine-define add rate limiting]"

    def test_edit(self):
        content = [
            {
                "type": "tool_use",
                "name": "Edit",
                "input": {
                    "file_path": "src/ccrecall/content.py",
                    "old_string": "old_value",
                    "new_string": "new_value",
                },
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Edit: src/ccrecall/content.py old_value new_value]"

    def test_read(self):
        content = [{"type": "tool_use", "name": "Read", "input": {"file_path": "src/ccrecall/content.py"}}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Read: src/ccrecall/content.py]"

    def test_grep(self):
        content = [{"type": "tool_use", "name": "Grep", "input": {"pattern": "extract_text_content"}}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Grep: extract_text_content]"

    def test_glob(self):
        content = [{"type": "tool_use", "name": "Glob", "input": {"pattern": "**/*.py"}}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Glob: **/*.py]"

    def test_write(self):
        content = [
            {
                "type": "tool_use",
                "name": "Write",
                "input": {"file_path": "src/new.py", "content": "print('hi')"},
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Write: src/new.py print('hi')]"

    def test_multi_edit(self):
        content = [
            {
                "type": "tool_use",
                "name": "MultiEdit",
                "input": {
                    "file_path": "src/a.py",
                    "edits": [{"old_string": "a", "new_string": "b"}],
                },
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[MultiEdit: src/a.py a b]"

    def test_unknown_tool_type(self):
        """A tool_use block with a name not covered by any dispatch table still
        produces a marker — generic field-join extraction has no dispatch table."""
        content = [
            {
                "type": "tool_use",
                "name": "SomeNewTool",
                "input": {"custom_field": "some value here"},
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[SomeNewTool: some value here]"

    def test_unknown_tool_type_no_string_fields(self):
        content = [{"type": "tool_use", "name": "SomeNewTool", "input": {"count": 5, "enabled": True}}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[SomeNewTool]"

    def test_multiple_tool_uses_newline_joined(self):
        content = [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Read: a.py]\n[Bash: ls]"


# extract_text_content — malformed tool_use input never raises


class TestToolContentMalformedInput:
    def test_missing_input_key(self):
        content = [{"type": "tool_use", "name": "Bash"}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Bash]"

    def test_input_is_string_not_dict(self):
        content = [{"type": "tool_use", "name": "Bash", "input": "not a dict"}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Bash]"

    def test_input_is_none(self):
        content = [{"type": "tool_use", "name": "Bash", "input": None}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Bash]"

    def test_none_value_where_string_expected(self):
        content = [{"type": "tool_use", "name": "Bash", "input": {"command": None}}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[Bash]"

    def test_nested_list_wrong_element_types(self):
        """AskUserQuestion's questions field holding ints instead of dicts must not raise."""
        content = [
            {
                "type": "tool_use",
                "name": "AskUserQuestion",
                "input": {"questions": [1, 2, 3]},
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[AskUserQuestion]"

    def test_nested_dict_with_none_values(self):
        content = [
            {
                "type": "tool_use",
                "name": "AskUserQuestion",
                "input": {"questions": [{"question": None, "options": None}]},
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[AskUserQuestion]"

    def test_questions_field_not_a_list(self):
        """questions is an int instead of a list — must not raise."""
        content = [{"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": 42}}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[AskUserQuestion]"

    def test_missing_name(self):
        content = [{"type": "tool_use", "input": {"command": "ls"}}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == "[: ls]"

    def test_non_string_name_list(self):
        """name: [1, 2] must not raise TypeError on tool_counts.get()."""
        content = [{"type": "tool_use", "name": [1, 2], "input": {"command": "ls"}}]
        _, _, _, summary, tool_content = extract_text_content(content)
        assert tool_content == "[: ls]"
        assert summary is None

    def test_non_string_name_dict(self):
        """name: {"a": 1} must not raise TypeError on tool_counts.get()."""
        content = [{"type": "tool_use", "name": {"a": 1}, "input": {"command": "ls"}}]
        _, _, _, summary, tool_content = extract_text_content(content)
        assert tool_content == "[: ls]"
        assert summary is None


# extract_text_content — wide-input traversal cap


class TestToolContentWideInputCap:
    def test_wide_list_capped(self):
        """A list with more items than _MAX_EXTRACT_ITEMS must not collect unbounded strings."""
        wide_input = {"items": [f"item-{i}" for i in range(_MAX_EXTRACT_ITEMS * 3)]}
        content = [{"type": "tool_use", "name": "Bulk", "input": wide_input}]
        _, _, _, _, tool_content = extract_text_content(content)
        marker_inner = tool_content[len("[Bulk: ") : -1]
        items_in_marker = [s for s in marker_inner.split(" ") if s.startswith("item-")]
        assert len(items_in_marker) <= _MAX_EXTRACT_ITEMS

    def test_wide_dict_capped(self):
        """A dict with more keys than _MAX_EXTRACT_ITEMS must not collect unbounded strings."""
        wide_input = {f"field_{i}": f"val-{i}" for i in range(_MAX_EXTRACT_ITEMS * 3)}
        content = [{"type": "tool_use", "name": "Bulk", "input": wide_input}]
        _, _, _, _, tool_content = extract_text_content(content)
        assert len(tool_content) > 0


# extract_text_content — cap truncation


class TestToolContentCapTruncation:
    def test_field_capped_at_200_chars(self):
        content = [
            {
                "type": "tool_use",
                "name": "Agent",
                "input": {"subagent_type": "researcher", "prompt": "x" * 1000},
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        assert tool_content == f"[Agent: researcher {'x' * TOOL_FIELD_CAP}]"

    def test_block_capped_at_300_chars_total(self):
        content = [
            {
                "type": "tool_use",
                "name": "Agent",
                "input": {"subagent_type": "a" * 1000, "prompt": "b" * 1000},
            }
        ]
        _, _, _, _, tool_content = extract_text_content(content)
        # Each field is capped to TOOL_FIELD_CAP chars first, then the joined
        # block is capped to TOOL_CONTENT_CAP chars total.
        inner = tool_content[len("[Agent: ") : -1]
        assert len(inner) == TOOL_CONTENT_CAP
        assert inner.startswith("a" * TOOL_FIELD_CAP)


# is_tool_result


class TestIsToolResult:
    def test_tool_result_content(self):
        content = [{"type": "tool_result", "tool_use_id": "abc", "content": "ok"}]
        assert is_tool_result(content) is True

    def test_normal_text_content(self):
        content = [{"type": "text", "text": "Hello"}]
        assert is_tool_result(content) is False

    def test_string_content(self):
        assert is_tool_result("Hello") is False

    def test_empty_list(self):
        assert is_tool_result([]) is False

    def test_none(self):
        assert is_tool_result(None) is False


# is_task_notification


class TestIsTaskNotification:
    def test_string_content(self):
        content = "<task-notification>\n<task-id>abc</task-id>\n<result>done</result>\n</task-notification>"
        assert is_task_notification(content) is True

    def test_list_content(self):
        content = [
            {
                "type": "text",
                "text": "<task-notification>\n<task-id>abc</task-id>\n</task-notification>",
            }
        ]
        assert is_task_notification(content) is True

    def test_normal_user_message(self):
        assert is_task_notification("Hello, how are you?") is False

    def test_tool_result_content(self):
        content = [{"type": "tool_result", "tool_use_id": "abc", "content": "ok"}]
        assert is_task_notification(content) is False

    def test_empty_string(self):
        assert is_task_notification("") is False

    def test_empty_list(self):
        assert is_task_notification([]) is False

    def test_none(self):
        assert is_task_notification(None) is False

    def test_whitespace_prefix(self):
        content = "  \n  <task-notification>\n<task-id>abc</task-id>\n</task-notification>"
        assert is_task_notification(content) is True

    def test_partial_match(self):
        content = "I received a <task-notification> in the middle"
        assert is_task_notification(content) is False


# is_teammate_message


class TestIsTeammateMessage:
    def test_string_teammate_message(self):
        content = '<teammate-message teammate_id="batch-ops" color="purple" summary="Task complete">\nTask #4 is complete.\n</teammate-message>'
        assert is_teammate_message(content) is True

    def test_idle_notification(self):
        content = '<teammate-message teammate_id="test-sync" color="yellow">\n{"type":"idle_notification","from":"test-sync","timestamp":"2026-02-14T17:35:47.648Z","idleReason":"available"}\n</teammate-message>'
        assert is_teammate_message(content) is True

    def test_list_content(self):
        content = [
            {
                "type": "text",
                "text": '<teammate-message teammate_id="x">\nDone.\n</teammate-message>',
            }
        ]
        assert is_teammate_message(content) is True

    def test_regular_user_message(self):
        assert is_teammate_message("Hello, how are you?") is False

    def test_task_notification_not_teammate(self):
        content = "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"
        assert is_teammate_message(content) is False

    def test_empty_string(self):
        assert is_teammate_message("") is False

    def test_none(self):
        assert is_teammate_message(None) is False

    def test_whitespace_prefix(self):
        content = '  \n  <teammate-message teammate_id="x">\nDone.\n</teammate-message>'
        assert is_teammate_message(content) is True

    def test_partial_match(self):
        content = "I received a <teammate-message> in the middle"
        assert is_teammate_message(content) is False


# extract_files_modified


class TestExtractFilesModified:
    def test_edit_and_write(self):
        content = [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b.py"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/c/d.py"}},
        ]
        assert extract_files_modified(content) == ["/a/b.py", "/c/d.py"]

    def test_multi_edit(self):
        content = [
            {
                "type": "tool_use",
                "name": "MultiEdit",
                "input": {"file_path": "/e/f.py"},
            },
        ]
        assert extract_files_modified(content) == ["/e/f.py"]

    def test_non_file_tools_ignored(self):
        content = [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
        ]
        assert extract_files_modified(content) == []

    def test_string_content(self):
        assert extract_files_modified("hello") == []

    def test_missing_file_path(self):
        content = [{"type": "tool_use", "name": "Edit", "input": {"old_string": "a"}}]
        assert extract_files_modified(content) == []


# extract_commits


class TestExtractCommits:
    def test_git_commit_double_quotes(self):
        content = [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": 'git commit -m "Fix bug in parser"'},
            },
        ]
        assert extract_commits(content) == ["Fix bug in parser"]

    def test_git_commit_single_quotes(self):
        content = [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "git commit -m 'Add new feature'"},
            },
        ]
        assert extract_commits(content) == ["Add new feature"]

    def test_non_commit_bash(self):
        content = [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
        ]
        assert extract_commits(content) == []

    def test_non_bash_tool(self):
        content = [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}},
        ]
        assert extract_commits(content) == []

    def test_long_commit_message_truncated(self):
        long_msg = "x" * 200
        content = [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": f'git commit -m "{long_msg}"'},
            },
        ]
        commits = extract_commits(content)
        assert len(commits) == 1
        assert len(commits[0]) == 100

    def test_string_content(self):
        assert extract_commits("hello") == []


# channel XML stripping


class TestChannelStripping:
    def test_strips_channel_xml(self):
        raw = '<channel source="plugin:telegram:telegram" chat_id="12345" user_name="alice">\nHello from Telegram!\n</channel>'
        text, _, _, _, _ = extract_text_content(raw)
        assert text == "Hello from Telegram!"
        assert "<channel" not in text

    def test_strips_channel_with_nested_content(self):
        raw = '<channel source="plugin:discord:discord" chat_id="99" user_name="bob">\nCan you help me with this code?\n```python\nprint("hi")\n```\n</channel>'
        text, _, _, _, _ = extract_text_content(raw)
        assert "Can you help me with this code?" in text
        assert 'print("hi")' in text
        assert "<channel" not in text

    def test_no_channel_tag_unchanged(self):
        raw = "Just a normal message"
        text, _, _, _, _ = extract_text_content(raw)
        assert text == "Just a normal message"


# parse_origin


class TestParseOrigin:
    def test_telegram_origin(self):
        entry = {"origin": {"kind": "channel", "server": "plugin:telegram:telegram"}}
        assert parse_origin(entry) == "telegram"

    def test_discord_origin(self):
        entry = {"origin": {"kind": "channel", "server": "plugin:discord:discord-bot"}}
        assert parse_origin(entry) == "discord"

    def test_slack_origin(self):
        entry = {"origin": {"kind": "channel", "server": "plugin:slack:slack-connector"}}
        assert parse_origin(entry) == "slack"

    def test_no_origin(self):
        entry = {"type": "user"}
        assert parse_origin(entry) is None

    def test_empty_origin(self):
        entry = {"origin": {}}
        assert parse_origin(entry) is None

    def test_no_fallback_to_kind_bare_server(self):
        # server without plugin: prefix has no extractable platform name
        entry = {"origin": {"kind": "channel", "server": "custom"}}
        assert parse_origin(entry) is None

    def test_origin_no_server(self):
        # kind fallback was removed (it leaked task-notification into origin)
        entry = {"origin": {"kind": "webhook"}}
        assert parse_origin(entry) is None
