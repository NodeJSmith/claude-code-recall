import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

from ccrecall.file_hashing import transcript_file_hash
from ccrecall.llm_summarizer import (
    PacketBuildError,
    _build_capability_check_argv,
    _parse_source_entries,
    branch_content_file_paths,
    branch_packet,
    build_claude_argv,
    build_prompt,
    capability_fingerprint,
    discover_current_session_source_files,
    get_claude_version,
    invoke_claude,
    read_capability_sidecar,
    reap_stale_packets,
    resolve_current_session_source_files,
    resolve_historical_source_files,
    run_capability_check,
    verify_capability_sidecar,
    write_capability_sidecar,
)
from ccrecall.summary_enrichment import (
    STATUS_AUTH_REQUIRED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CAPABILITY_UNVERIFIED,
    STATUS_CLAUDE_UNAVAILABLE,
    STATUS_ERROR,
    STATUS_INVALID_OUTPUT,
    STATUS_OK,
    STATUS_RATE_LIMITED,
    STATUS_SOURCE_CHANGED,
    STATUS_SOURCE_INCOMPLETE,
    STATUS_SOURCE_UNVERIFIED,
    STATUS_TIMEOUT,
    STATUS_UNSAFE_SOURCE_PATH,
    STATUS_UNSUPPORTED_CLI,
)


def _u(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, label))


def _entry(message_uuid: str, parent_uuid: str | None, ts: str, role: str, content) -> dict:
    return {
        "uuid": message_uuid,
        "parentUuid": parent_uuid,
        "type": role,
        "timestamp": ts,
        "message": {"role": role, "content": content},
    }


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")


class TestClaudeInvocation:
    def test_build_claude_argv_uses_safe_read_only_boundary(self, tmp_path):

        packet_dir = tmp_path / "packet"
        packet_dir.mkdir()
        settings = {
            "llm_summary_model": "sonnet",
            "llm_summary_effort": "medium",
            "llm_summary_max_budget_usd": 1.75,
        }

        argv = build_claude_argv(packet_dir, settings, "prompt text")

        assert argv[0:3] == ["claude", "-p", "--safe-mode"]
        assert "--disable-slash-commands" in argv
        assert "--strict-mcp-config" in argv
        assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
        assert argv[argv.index("--tools") + 1] == "Read"
        assert argv[argv.index("--allowedTools") + 1] == "Read"
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        assert argv[argv.index("--output-format") + 1] == "json"
        assert argv[argv.index("--max-budget-usd") + 1] == "1.75"
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert argv[argv.index("--effort") + 1] == "medium"
        assert "--no-session-persistence" in argv
        assert argv[argv.index("--add-dir") + 1] == str(packet_dir)
        assert argv[-1] == "prompt text"

    def test_build_prompt_directs_latest_state_history_rationale_failures_and_evidenced_next_steps(self, tmp_path):

        packet_dir = tmp_path / "packet"
        packet_dir.mkdir()

        prompt = build_prompt(packet_dir)

        assert f"Branch outline path: {packet_dir / 'branch-outline.json'}" in prompt
        assert "Read branch-outline.json and deterministic-summary.json first." in prompt
        assert (
            "Use the outline to locate relevant detailed transcript entries, especially the last exchanges and any middle-branch decision or failure points."
            in prompt
        )
        assert (
            "where_we_left_off must describe the latest evidenced state, including blockers or verification status when known."
            in prompt
        )
        assert "how_we_got_here must explain the causal path to that state, not repeat the latest-state text." in prompt
        assert "Include a key decision only when its rationale is evidenced." in prompt
        assert "Include an attempted path only when it was evidenced as failed, abandoned, or inconclusive." in prompt
        assert (
            "When the branch ends with an evidenced unresolved action, blocker, or handoff, include at least one specific continuation hint. Do not add a generic next step when no such evidence exists."
            in prompt
        )
        assert "Do not invent decisions, rationale, failures, unresolved tasks, or generic next steps." in prompt
        assert "Every factual section and list item must cite source_uuids from the allowlist." in prompt

    def test_invoke_claude_uses_argv_list_without_shell_and_isolated_cwd(self, tmp_path, monkeypatch):

        packet_dir = tmp_path / "packet"
        packet_dir.mkdir()
        settings = {
            "llm_summary_model": "sonnet",
            "llm_summary_effort": "medium",
            "llm_summary_max_budget_usd": 1.0,
            "llm_summary_timeout_seconds": 30,
        }
        calls = {}

        def fake_run(argv, **kwargs):
            calls["argv"] = argv
            calls["kwargs"] = kwargs
            payload = {
                "title": {"text": "ok", "source_uuids": [_u("1")]},
                "where_we_left_off": {"text": "done", "source_uuids": [_u("1")]},
                "how_we_got_here": {"text": "path", "source_uuids": [_u("1")]},
                "key_decisions": [],
                "attempted_paths": [],
                "open_questions": [],
                "files_and_reasons": [],
                "continuation_hints": [],
                "confidence": "high",
            }
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

        result = invoke_claude(
            packet_dir,
            settings,
            "prompt",
            active_branch_uuids={_u("1")},
            valid_file_paths=set(),
            run=fake_run,
        )

        assert result.status == STATUS_OK
        assert isinstance(calls["argv"], list)
        assert calls["kwargs"].get("shell", False) is False
        assert calls["kwargs"]["cwd"] == packet_dir
        assert calls["kwargs"]["capture_output"] is True
        assert calls["kwargs"]["text"] is True
        assert calls["kwargs"]["timeout"] == 30

    @pytest.mark.parametrize(
        ("outcome", "expected_status"),
        [
            (FileNotFoundError("claude"), STATUS_CLAUDE_UNAVAILABLE),
            (subprocess.TimeoutExpired("claude", 1), STATUS_TIMEOUT),
            (
                subprocess.CompletedProcess(["claude"], 2, stdout="", stderr="error: unknown option --json-schema"),
                STATUS_UNSUPPORTED_CLI,
            ),
            (
                subprocess.CompletedProcess(["claude"], 2, stdout="", stderr="Please login to Claude Code"),
                STATUS_AUTH_REQUIRED,
            ),
            (subprocess.CompletedProcess(["claude"], 2, stdout="", stderr="rate limit exceeded"), STATUS_RATE_LIMITED),
            (
                subprocess.CompletedProcess(["claude"], 2, stdout="", stderr="max budget exceeded"),
                STATUS_BUDGET_EXCEEDED,
            ),
            (subprocess.CompletedProcess(["claude"], 2, stdout="not-json", stderr=""), STATUS_INVALID_OUTPUT),
            (subprocess.CompletedProcess(["claude"], 2, stdout="", stderr="something odd happened"), STATUS_ERROR),
        ],
    )
    def test_invoke_claude_classifies_expected_failures(self, tmp_path, outcome, expected_status):

        packet_dir = tmp_path / "packet"
        packet_dir.mkdir()
        settings = {
            "llm_summary_model": "sonnet",
            "llm_summary_effort": "medium",
            "llm_summary_max_budget_usd": 1.0,
            "llm_summary_timeout_seconds": 30,
        }

        def fake_run(*_args, **_kwargs):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        result = invoke_claude(
            packet_dir,
            settings,
            "prompt",
            active_branch_uuids={_u("1")},
            valid_file_paths=set(),
            run=fake_run,
        )

        assert result.status == expected_status
        assert result.response_body is None
        if result.diagnostic is not None:
            assert len(result.diagnostic) <= 240

    def test_invoke_claude_surfaces_budget_threshold_diagnostic_without_rewriting_it(self, tmp_path):

        packet_dir = tmp_path / "packet"
        packet_dir.mkdir()
        settings = {
            "llm_summary_model": "sonnet",
            "llm_summary_effort": "medium",
            "llm_summary_max_budget_usd": 1.0,
            "llm_summary_timeout_seconds": 30,
        }

        def fake_run(*_args, **_kwargs):
            return subprocess.CompletedProcess(
                ["claude"],
                2,
                stdout="",
                stderr="Budget threshold reached after exceeding the configured stop threshold.",
            )

        result = invoke_claude(
            packet_dir,
            settings,
            "prompt",
            active_branch_uuids={_u("1")},
            valid_file_paths=set(),
            run=fake_run,
        )

        assert result.status == STATUS_BUDGET_EXCEEDED
        assert result.diagnostic is not None
        assert "threshold" in result.diagnostic.lower()

    def test_get_claude_version_reads_version_at_boundary(self):

        def fake_run(argv, **kwargs):
            assert argv == ["claude", "--version"]
            assert kwargs == {"capture_output": True, "text": True, "timeout": 30}
            return subprocess.CompletedProcess(argv, 0, stdout="1.2.3\n", stderr="")

        version, diagnostic = get_claude_version(run=fake_run)

        assert version == "1.2.3"
        assert diagnostic is None

    def test_get_claude_version_reports_missing_binary(self):

        def fake_run(*_args, **_kwargs):
            raise FileNotFoundError("claude")

        version, diagnostic = get_claude_version(run=fake_run)

        assert version is None
        assert diagnostic == "claude"


class TestCapabilitySidecar:
    def test_security_fingerprint_ignores_model_effort_and_budget(self, tmp_path):

        settings_variants = [
            {
                "llm_summary_timeout_seconds": 30,
                "llm_summary_model": "sonnet",
                "llm_summary_effort": "medium",
                "llm_summary_max_budget_usd": 1.0,
            },
            {
                "llm_summary_timeout_seconds": 30,
                "llm_summary_model": "haiku",
                "llm_summary_effort": "high",
                "llm_summary_max_budget_usd": 5.0,
            },
            {
                "llm_summary_timeout_seconds": 30,
                "llm_summary_model": "opus",
                "llm_summary_effort": "low",
                "llm_summary_max_budget_usd": 0.25,
            },
        ]

        def ok_run(*_args, **_kwargs):
            return subprocess.CompletedProcess(["claude"], 0, stdout="{}", stderr="")

        fingerprints = set()
        for index, settings in enumerate(settings_variants):
            sidecar = tmp_path / f"capability-{index}.json"
            result = run_capability_check(
                settings,
                sidecar_path=sidecar,
                projects_dir=tmp_path / "projects",
                claude_version="1.2.3",
                run=ok_run,
            )
            sidecar_data = read_capability_sidecar(sidecar)

            assert result.status == STATUS_OK
            assert sidecar_data is not None
            fingerprints.add(sidecar_data["fingerprint"])

        assert len(fingerprints) == 1

    def test_readiness_rejects_missing_stale_and_mismatched_sidecar(self, tmp_path):
        sidecar = tmp_path / "capability.json"
        fingerprint = capability_fingerprint(packet_dir=tmp_path / "packet")

        status, _reason = verify_capability_sidecar(sidecar, claude_version="1.2.3", fingerprint=fingerprint)
        assert status == STATUS_CAPABILITY_UNVERIFIED
        assert read_capability_sidecar(sidecar) is None

        write_capability_sidecar(
            sidecar,
            status=STATUS_OK,
            claude_version="1.2.2",
            fingerprint=fingerprint,
        )
        status, _reason = verify_capability_sidecar(sidecar, claude_version="1.2.3", fingerprint=fingerprint)
        assert status == STATUS_CAPABILITY_UNVERIFIED

        write_capability_sidecar(
            sidecar,
            status=STATUS_OK,
            claude_version="1.2.3",
            fingerprint="other",
        )
        status, _reason = verify_capability_sidecar(sidecar, claude_version="1.2.3", fingerprint=fingerprint)
        assert status == STATUS_CAPABILITY_UNVERIFIED

    def test_budget_exceeded_in_sidecar_remains_distinct_until_successful_rerun(self, tmp_path):

        sidecar = tmp_path / "capability.json"
        fingerprint = capability_fingerprint(packet_dir=tmp_path / "packet")

        def fail_run(*_args, **_kwargs):
            return subprocess.CompletedProcess(["claude"], 2, stdout="", stderr="max budget threshold exceeded")

        failed = run_capability_check(
            {"llm_summary_timeout_seconds": 30},
            sidecar_path=sidecar,
            projects_dir=tmp_path / "projects",
            claude_version="1.2.3",
            run=fail_run,
        )

        assert failed.status == STATUS_BUDGET_EXCEEDED

        status, _reason = verify_capability_sidecar(sidecar, claude_version="1.2.3", fingerprint=fingerprint)
        assert status == STATUS_BUDGET_EXCEEDED

        unchanged_fingerprint = capability_fingerprint(packet_dir=tmp_path / "different-packet")
        status, _reason = verify_capability_sidecar(
            sidecar,
            claude_version="1.2.3",
            fingerprint=unchanged_fingerprint,
        )
        assert status == STATUS_BUDGET_EXCEEDED

        def ok_run(*_args, **_kwargs):
            return subprocess.CompletedProcess(["claude"], 0, stdout="{}", stderr="")

        succeeded = run_capability_check(
            {"llm_summary_timeout_seconds": 30},
            sidecar_path=sidecar,
            projects_dir=tmp_path / "projects",
            claude_version="1.2.3",
            run=ok_run,
        )

        assert succeeded.status == STATUS_OK

        status, _reason = verify_capability_sidecar(sidecar, claude_version="1.2.3", fingerprint=fingerprint)
        assert status == STATUS_OK

    def test_capability_fingerprint_tracks_capability_check_argv_shape(self, tmp_path):

        packet_dir = tmp_path / "packet"
        argv = _build_capability_check_argv(packet_dir)
        normalized = ["<packet-dir>" if arg == str(packet_dir) else arg for arg in argv[1:-1]]

        assert capability_fingerprint(packet_dir=packet_dir) == capability_fingerprint(
            packet_dir=tmp_path / "other-packet"
        )
        assert (
            capability_fingerprint(packet_dir=packet_dir)
            == hashlib.sha256("\0".join(normalized).encode("utf-8")).hexdigest()
        )

    def test_capability_check_fails_if_new_importable_transcript_appears(self, tmp_path):

        projects_dir = tmp_path / "projects"
        project = projects_dir / "proj"
        project.mkdir(parents=True)
        sidecar = tmp_path / "capability.json"
        settings = {"llm_summary_timeout_seconds": 30}
        session_uuid = _u("capability-session")
        created_transcript = project / f"{session_uuid}.jsonl"

        def fake_run(_argv, **_kwargs):
            created_transcript.write_text("{}\n", encoding="utf-8")
            return subprocess.CompletedProcess(["claude"], 0, stdout="{}", stderr="")

        result = run_capability_check(
            settings,
            sidecar_path=sidecar,
            projects_dir=projects_dir,
            claude_version="1.2.3",
            run=fake_run,
        )

        assert result.status == STATUS_CAPABILITY_UNVERIFIED
        assert created_transcript.exists()

    def test_capability_check_fails_if_existing_importable_transcript_changes_in_place(self, tmp_path):

        projects_dir = tmp_path / "projects"
        project = projects_dir / "proj"
        project.mkdir(parents=True)
        sidecar = tmp_path / "capability.json"
        settings = {"llm_summary_timeout_seconds": 30}
        session_uuid = _u("capability-session-existing")
        transcript = project / f"{session_uuid}.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")

        def fake_run(_argv, **_kwargs):
            transcript.write_text("{}\n{}\n", encoding="utf-8")
            return subprocess.CompletedProcess(["claude"], 0, stdout="{}", stderr="")

        result = run_capability_check(
            settings,
            sidecar_path=sidecar,
            projects_dir=projects_dir,
            claude_version="1.2.3",
            run=fake_run,
        )

        assert result.status == STATUS_CAPABILITY_UNVERIFIED

    @pytest.mark.parametrize(
        "relative_path",
        [
            Path("proj") / "state" / "subagents" / "agent-capability-session.jsonl",
            Path("proj") / "state" / "nested" / "deeper" / "subagents" / "agent-capability-session.jsonl",
        ],
    )
    def test_capability_check_fails_if_new_subagent_or_parser_resolvable_transcript_appears(
        self, tmp_path, relative_path
    ):

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        sidecar = tmp_path / "capability.json"
        settings = {"llm_summary_timeout_seconds": 30}
        session_uuid = _u("capability-session")
        created_path = projects_dir / str(relative_path).replace("capability-session", session_uuid)

        def fake_run(_argv, **_kwargs):
            created_path.parent.mkdir(parents=True, exist_ok=True)
            created_path.write_text("{}\n", encoding="utf-8")
            return subprocess.CompletedProcess(["claude"], 0, stdout="{}", stderr="")

        result = run_capability_check(
            settings,
            sidecar_path=sidecar,
            projects_dir=projects_dir,
            claude_version="1.2.3",
            run=fake_run,
        )

        assert result.status == STATUS_CAPABILITY_UNVERIFIED


class TestSourceResolution:
    def test_current_session_source_discovery_finds_direct_and_subagent_and_rejects_symlink(self, tmp_path):

        session_uuid = _u("session")
        projects_dir = tmp_path / "projects"
        project_a = projects_dir / "a"
        project_b = projects_dir / "b"
        direct_dir = project_a / "nested"
        subagents = project_b / "state" / "subagents"
        direct_dir.mkdir(parents=True)
        subagents.mkdir(parents=True)
        good_direct = project_a / f"{session_uuid}.jsonl"
        good_direct.write_text("{}\n", encoding="utf-8")
        good_subagent = subagents / f"agent-{session_uuid}.jsonl"
        good_subagent.write_text("{}\n", encoding="utf-8")
        symlink_target = tmp_path / "outside.jsonl"
        symlink_target.write_text("{}\n", encoding="utf-8")
        (project_a / f"{session_uuid}-symlink.jsonl").symlink_to(symlink_target)

        found = discover_current_session_source_files(projects_dir, session_uuid)

        assert found == [good_direct, good_subagent]

    def test_current_session_source_discovery_skips_unrelated_project_tree_dirs_while_supporting_known_layouts(
        self, tmp_path, monkeypatch
    ):

        session_uuid = _u("session-no-rglob")
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "a"
        project_dir.mkdir(parents=True)
        unrelated_dir = project_dir / "node_modules"
        unrelated_dir.mkdir()
        direct = project_dir / f"{session_uuid}.jsonl"
        direct.write_text("{}\n", encoding="utf-8")
        subagent = project_dir / "state" / "nested" / "subagents" / f"agent-{session_uuid}.jsonl"
        subagent.parent.mkdir(parents=True)
        subagent.write_text("{}\n", encoding="utf-8")
        original_iterdir = Path.iterdir

        def fail_rglob(self, pattern):
            raise AssertionError(f"rglob should not run on hook-path discovery: {self} {pattern}")

        def guarded_iterdir(self):
            if self == unrelated_dir:
                raise AssertionError(f"unexpected full-tree walk into unrelated dir: {self}")
            return original_iterdir(self)

        monkeypatch.setattr(Path, "rglob", fail_rglob)
        monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

        assert discover_current_session_source_files(projects_dir, session_uuid) == [direct, subagent]

    def test_current_session_source_discovery_finds_arbitrarily_nested_subagents_dirs(self, tmp_path):

        session_uuid = _u("session-nested-subagents")
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "a"
        project_dir.mkdir(parents=True)
        direct = project_dir / f"{session_uuid}.jsonl"
        direct.write_text("{}\n", encoding="utf-8")
        nested_subagent = project_dir / "state" / "nested" / "deeper" / "subagents" / f"agent-{session_uuid}.jsonl"
        nested_subagent.parent.mkdir(parents=True)
        nested_subagent.write_text("{}\n", encoding="utf-8")

        assert discover_current_session_source_files(projects_dir, session_uuid) == [direct, nested_subagent]

    def test_current_session_source_discovery_rejects_top_level_symlink_project_dirs(self, tmp_path):

        session_uuid = _u("session-project-symlink")
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        real_project = tmp_path / "real-project"
        real_project.mkdir()
        (real_project / f"{session_uuid}.jsonl").write_text("{}\n", encoding="utf-8")
        (projects_dir / "linked-project").symlink_to(real_project, target_is_directory=True)

        result = resolve_current_session_source_files(projects_dir, session_uuid)

        assert result.status == STATUS_UNSAFE_SOURCE_PATH
        assert result.files == []

    def test_current_session_source_discovery_rejects_non_regular_candidates(self, tmp_path):

        session_uuid = _u("session-dir")
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "a"
        project_dir.mkdir(parents=True)
        (project_dir / f"{session_uuid}.jsonl").mkdir()
        subagents = project_dir / "state" / "subagents"
        subagents.mkdir(parents=True)
        (subagents / f"agent-{session_uuid}.jsonl").mkdir()

        assert discover_current_session_source_files(projects_dir, session_uuid) == []

    def test_current_session_source_resolution_reports_unsafe_paths(self, tmp_path):

        session_uuid = _u("session-unsafe")
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "a"
        project_dir.mkdir(parents=True)
        (project_dir / f"{session_uuid}.jsonl").mkdir()

        result = resolve_current_session_source_files(projects_dir, session_uuid)

        assert result.status == STATUS_UNSAFE_SOURCE_PATH
        assert result.files == []

    def test_current_session_source_resolution_rejects_mixed_safe_and_unsafe_candidates_for_same_session(
        self, tmp_path
    ):

        session_uuid = _u("session-mixed-safe-unsafe")
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "a"
        project_dir.mkdir(parents=True)
        (project_dir / f"{session_uuid}.jsonl").write_text("{}\n", encoding="utf-8")
        subagents = project_dir / "state" / "subagents"
        subagents.mkdir(parents=True)
        (subagents / f"agent-{session_uuid}.jsonl").mkdir()

        result = resolve_current_session_source_files(projects_dir, session_uuid)

        assert result.status == STATUS_UNSAFE_SOURCE_PATH
        assert result.files == []

    def test_current_session_source_resolution_detects_nested_subagent_in_unsafe_state_tree(self, tmp_path):

        session_uuid = _u("session-unsafe-state")
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "a"
        project_dir.mkdir(parents=True)
        unsafe_state = tmp_path / "unsafe-state" / "nested" / "subagents"
        unsafe_state.mkdir(parents=True)
        (unsafe_state / f"agent-{session_uuid}.jsonl").write_text("{}\n", encoding="utf-8")
        (project_dir / "state").symlink_to(unsafe_state.parent.parent, target_is_directory=True)

        result = resolve_current_session_source_files(projects_dir, session_uuid)

        assert result.status == STATUS_UNSAFE_SOURCE_PATH
        assert result.files == []

    def test_current_session_source_resolution_detects_nested_subagent_below_symlinked_state_descendant(self, tmp_path):

        session_uuid = _u("session-unsafe-state-descendant")
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "a"
        (project_dir / "state").mkdir(parents=True)
        unsafe_subagents = tmp_path / "unsafe-state" / "nested" / "subagents"
        unsafe_subagents.mkdir(parents=True)
        (unsafe_subagents / f"agent-{session_uuid}.jsonl").write_text("{}\n", encoding="utf-8")
        (project_dir / "state" / "linked").symlink_to(unsafe_subagents.parent.parent, target_is_directory=True)

        result = resolve_current_session_source_files(projects_dir, session_uuid)

        assert result.status == STATUS_UNSAFE_SOURCE_PATH
        assert result.files == []

    def test_branch_content_file_paths_collects_tool_input_prose_and_result_references(self, tmp_path):

        (tmp_path / "src").mkdir()
        (tmp_path / "docs").mkdir()
        (tmp_path / "results").mkdir()
        (tmp_path / "src" / "prose_only.py").write_text("", encoding="utf-8")
        (tmp_path / "docs" / "reference.md").write_text("", encoding="utf-8")
        (tmp_path / "results" / "final-report.txt").write_text("", encoding="utf-8")
        session_file = tmp_path / "session.jsonl"
        user_uuid = _u("paths-user")
        assistant_uuid = _u("paths-assistant")
        result_uuid = _u("paths-result")
        _write_jsonl(
            session_file,
            [
                _entry(
                    user_uuid,
                    None,
                    "2026-08-07T10:00:00Z",
                    "user",
                    [{"type": "text", "text": "Please inspect `src/prose_only.py` before changing anything."}],
                ),
                {
                    "uuid": assistant_uuid,
                    "parentUuid": user_uuid,
                    "type": "assistant",
                    "timestamp": "2026-08-07T10:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "I also compared it with docs/reference.md."},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "src/input_only.py"}},
                        ],
                    },
                },
                {
                    "uuid": result_uuid,
                    "parentUuid": assistant_uuid,
                    "type": "user",
                    "timestamp": "2026-08-07T10:00:02Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": [{"type": "text", "text": "Saved summary to results/final-report.txt."}],
                            }
                        ],
                    },
                },
            ],
        )

        assert branch_content_file_paths(
            [session_file], {user_uuid, assistant_uuid, result_uuid}, project_root=tmp_path
        ) == {
            "src/prose_only.py",
            "docs/reference.md",
            "src/input_only.py",
            "results/final-report.txt",
        }

    def test_branch_content_file_paths_accepts_root_level_readme_from_prose_and_tool_result(self, tmp_path):

        (tmp_path / "README.md").write_text("", encoding="utf-8")
        session_file = tmp_path / "session-readme.jsonl"
        user_uuid = _u("paths-root-prose-user")
        assistant_uuid = _u("paths-root-prose-assistant")
        result_uuid = _u("paths-root-prose-result")
        _write_jsonl(
            session_file,
            [
                _entry(
                    user_uuid,
                    None,
                    "2026-08-07T10:00:00Z",
                    "user",
                    [{"type": "text", "text": "Please update `README.md` before we wrap up."}],
                ),
                _entry(
                    assistant_uuid,
                    user_uuid,
                    "2026-08-07T10:00:01Z",
                    "assistant",
                    [{"type": "text", "text": "I checked the surrounding docs as well."}],
                ),
                {
                    "uuid": result_uuid,
                    "parentUuid": assistant_uuid,
                    "type": "user",
                    "timestamp": "2026-08-07T10:00:02Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": [{"type": "text", "text": "Saved final notes to README.md."}],
                            }
                        ],
                    },
                },
            ],
        )

        assert branch_content_file_paths(
            [session_file], {user_uuid, assistant_uuid, result_uuid}, project_root=tmp_path
        ) == {"README.md"}

    def test_branch_content_file_paths_keeps_existing_root_level_repo_file(self, tmp_path):

        (tmp_path / "example.com").write_text("", encoding="utf-8")
        session_file = tmp_path / "session-existing-root-file.jsonl"
        user_uuid = _u("paths-existing-root-file")
        _write_jsonl(
            session_file,
            [
                _entry(
                    user_uuid,
                    None,
                    "2026-08-07T10:00:00Z",
                    "user",
                    [{"type": "text", "text": "Check example.com before we continue."}],
                )
            ],
        )

        assert branch_content_file_paths([session_file], {user_uuid}, project_root=tmp_path) == {"example.com"}

    def test_branch_content_file_paths_requires_file_like_final_segment_for_slash_paths(self, tmp_path):

        (tmp_path / "config").mkdir()
        (tmp_path / "docs").mkdir()
        (tmp_path / "Dockerfile").write_text("", encoding="utf-8")
        (tmp_path / "NOTICE").write_text("", encoding="utf-8")
        (tmp_path / "config" / ".gitignore").write_text("", encoding="utf-8")
        (tmp_path / "docs" / "Makefile").write_text("", encoding="utf-8")
        session_file = tmp_path / "session-extensionless-paths.jsonl"
        user_uuid = _u("paths-extensionless-user")
        assistant_uuid = _u("paths-extensionless-assistant")
        result_uuid = _u("paths-extensionless-result")
        _write_jsonl(
            session_file,
            [
                _entry(
                    user_uuid,
                    None,
                    "2026-08-07T10:00:00Z",
                    "user",
                    [
                        {
                            "type": "text",
                            "text": "Review Dockerfile and config/.gitignore, but do not mistake and/or for a path.",
                        }
                    ],
                ),
                {
                    "uuid": assistant_uuid,
                    "parentUuid": user_uuid,
                    "type": "assistant",
                    "timestamp": "2026-08-07T10:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "I also checked docs/Makefile and skipped this/or that."},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "ops/Justfile"}},
                        ],
                    },
                },
                {
                    "uuid": result_uuid,
                    "parentUuid": assistant_uuid,
                    "type": "user",
                    "timestamp": "2026-08-07T10:00:02Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": [{"type": "text", "text": "Updated NOTICE after checking docs/v2."}],
                            }
                        ],
                    },
                },
            ],
        )

        assert branch_content_file_paths(
            [session_file], {user_uuid, assistant_uuid, result_uuid}, project_root=tmp_path
        ) == {
            "Dockerfile",
            "config/.gitignore",
            "docs/Makefile",
            "ops/Justfile",
            "NOTICE",
        }

    def test_branch_content_file_paths_drops_absolute_and_local_tool_input_paths(self, tmp_path):

        (tmp_path / "src").mkdir()
        session_file = tmp_path / "session-unsafe-tool-paths.jsonl"
        assistant_uuid = _u("paths-unsafe-assistant")
        _write_jsonl(
            session_file,
            [
                {
                    "uuid": assistant_uuid,
                    "parentUuid": None,
                    "type": "assistant",
                    "timestamp": "2026-08-07T10:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/absolute.py"}},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "./local.py"}},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "../escape.py"}},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "~/home.py"}},
                            {"type": "tool_use", "name": "Glob", "input": {"path": "src"}},
                            {"type": "tool_use", "name": "Read", "input": {"paths": ["docs", "src/kept.py"]}},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "src/kept.py"}},
                        ],
                    },
                }
            ],
        )

        assert branch_content_file_paths([session_file], {assistant_uuid}, project_root=tmp_path) == {"src/kept.py"}

    def test_branch_content_file_paths_rejects_absolute_and_dotted_non_file_prose_tokens(self, tmp_path):

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "real.py").write_text("", encoding="utf-8")
        (tmp_path / "README.md").write_text("", encoding="utf-8")
        (tmp_path / "release-1.2.md").write_text("", encoding="utf-8")
        session_file = tmp_path / "session-prose-paths.jsonl"
        user_uuid = _u("paths-prose-user")
        result_uuid = _u("paths-prose-result")
        _write_jsonl(
            session_file,
            [
                _entry(
                    user_uuid,
                    None,
                    "2026-08-07T10:00:00Z",
                    "user",
                    [
                        {
                            "type": "text",
                            "text": "Compare src/real.py with README.md, ignore /tmp/absolute.py, ./local.py, ../escape.py, ~/home.py, C:/worktree/file.py, v1.2.3, and docs/v2.",
                        }
                    ],
                ),
                {
                    "uuid": result_uuid,
                    "parentUuid": user_uuid,
                    "type": "user",
                    "timestamp": "2026-08-07T10:00:01Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "content": [{"type": "text", "text": "Wrote release-1.2.md and skipped 1.2.3."}],
                            }
                        ],
                    },
                },
            ],
        )

        assert branch_content_file_paths([session_file], {user_uuid, result_uuid}, project_root=tmp_path) == {
            "README.md",
            "release-1.2.md",
            "src/real.py",
        }

    def test_historical_sources_classify_changed_and_unverified(self, tmp_path):

        session_uuid = _u("history")
        changed = tmp_path / f"{session_uuid}.jsonl"
        changed.write_text("{}\n", encoding="utf-8")
        unresolved = tmp_path / f"agent-{session_uuid}.jsonl"
        unresolved.write_text("{}\n", encoding="utf-8")

        changed_result = resolve_historical_source_files(
            session_uuid,
            [
                {
                    "file_path": str(changed),
                    "file_hash": "stale",
                    "file_size": changed.stat().st_size,
                    "file_mtime": changed.stat().st_mtime,
                }
            ],
        )
        assert changed_result.status == STATUS_SOURCE_CHANGED

        unresolved_result = resolve_historical_source_files(
            session_uuid,
            [
                {
                    "file_path": str(unresolved),
                    "file_hash": None,
                    "file_size": None,
                    "file_mtime": None,
                }
            ],
        )
        assert unresolved_result.status == STATUS_SOURCE_UNVERIFIED

    def test_historical_sources_reject_non_regular_and_symlink_candidates(self, tmp_path):

        session_uuid = _u("history-non-regular")
        directory_candidate = tmp_path / f"{session_uuid}.jsonl"
        directory_candidate.mkdir()
        symlink_target = tmp_path / f"target-{session_uuid}.jsonl"
        symlink_target.write_text("{}\n", encoding="utf-8")
        symlink_candidate = tmp_path / f"agent-{session_uuid}.jsonl"
        symlink_candidate.symlink_to(symlink_target)

        result = resolve_historical_source_files(
            session_uuid,
            [
                {
                    "file_path": str(directory_candidate),
                    "file_hash": "hash",
                    "file_size": 0,
                    "file_mtime": 0,
                },
                {
                    "file_path": str(symlink_candidate),
                    "file_hash": "hash",
                    "file_size": symlink_target.stat().st_size,
                    "file_mtime": symlink_target.stat().st_mtime,
                },
            ],
        )

        assert result.status == STATUS_UNSAFE_SOURCE_PATH
        assert result.files == []

    def test_historical_sources_exclude_unsafe_candidates_when_valid_sources_remain(self, tmp_path):

        session_uuid = _u("history-mixed-safe-unsafe")
        safe_source = tmp_path / f"{session_uuid}.jsonl"
        safe_source.write_text("{}\n", encoding="utf-8")
        safe_stat = safe_source.stat()
        symlink_target = tmp_path / f"target-{session_uuid}.jsonl"
        symlink_target.write_text("{}\n", encoding="utf-8")
        unsafe_source = tmp_path / f"agent-{session_uuid}.jsonl"
        unsafe_source.symlink_to(symlink_target)

        result = resolve_historical_source_files(
            session_uuid,
            [
                {
                    "file_path": str(safe_source),
                    "file_hash": None,
                    "file_size": safe_stat.st_size,
                    "file_mtime": safe_stat.st_mtime,
                },
                {
                    "file_path": str(unsafe_source),
                    "file_hash": None,
                    "file_size": None,
                    "file_mtime": None,
                },
            ],
        )

        assert result.status == STATUS_OK
        assert result.files == [safe_source]

    def test_historical_sources_prioritize_unsafe_over_unverified(self, tmp_path):

        session_uuid = _u("history-unsafe-over-unverified")
        unverified = tmp_path / f"{session_uuid}.jsonl"
        unverified.write_text("{}\n", encoding="utf-8")
        symlink_target = tmp_path / f"target-{session_uuid}.jsonl"
        symlink_target.write_text("{}\n", encoding="utf-8")
        unsafe_source = tmp_path / f"agent-{session_uuid}.jsonl"
        unsafe_source.symlink_to(symlink_target)

        result = resolve_historical_source_files(
            session_uuid,
            [
                {
                    "file_path": str(unverified),
                    "file_hash": None,
                    "file_size": None,
                    "file_mtime": None,
                },
                {
                    "file_path": str(unsafe_source),
                    "file_hash": None,
                    "file_size": None,
                    "file_mtime": None,
                },
            ],
        )

        assert result.status == STATUS_UNSAFE_SOURCE_PATH
        assert result.files == []

    def test_historical_sources_accept_tuple_rows_from_db_boundary(self, tmp_path):

        session_uuid = _u("history-tuple")
        source = tmp_path / f"{session_uuid}.jsonl"
        source.write_text("{}\n", encoding="utf-8")
        source_stat = source.stat()

        result = resolve_historical_source_files(
            session_uuid,
            [(str(source), None, source_stat.st_size, source_stat.st_mtime)],
        )

        assert result.status == STATUS_OK
        assert result.files == [source]

    @pytest.mark.parametrize(
        "row",
        [
            lambda source: {
                "file_path": str(source),
                "file_hash": None,
                "file_size": source.stat().st_size,
                "file_mtime": None,
            },
            lambda source: {
                "file_path": str(source),
                "file_hash": None,
                "file_size": None,
                "file_mtime": source.stat().st_mtime,
            },
            lambda source: {
                "file_path": str(source),
                "file_hash": transcript_file_hash(source),
                "file_size": None,
                "file_mtime": None,
            },
        ],
    )
    def test_historical_sources_accept_each_available_proof_field_independently(self, tmp_path, row):

        session_uuid = _u("history-partial-proof")
        source = tmp_path / f"{session_uuid}.jsonl"
        source.write_text("{}\n", encoding="utf-8")

        result = resolve_historical_source_files(session_uuid, [row(source)])

        assert result.status == STATUS_OK
        assert result.files == [source]

    def test_historical_sources_reject_mismatched_partial_stat_proof(self, tmp_path):

        session_uuid = _u("history-partial-mismatch")
        source = tmp_path / f"{session_uuid}.jsonl"
        source.write_text("{}\n", encoding="utf-8")

        result = resolve_historical_source_files(
            session_uuid,
            [{"file_path": str(source), "file_hash": None, "file_size": source.stat().st_size + 1, "file_mtime": None}],
        )

        assert result.status == STATUS_SOURCE_CHANGED
        assert result.files == []


class TestPacketBuilding:
    def test_packet_build_requires_complete_uuid_coverage_and_rejects_conflicting_duplicates(self, tmp_path):

        session_uuid = _u("packet")
        u1 = _u("u1")
        a1 = _u("a1")
        transcript_a = tmp_path / f"{session_uuid}.jsonl"
        transcript_b = tmp_path / f"agent-{session_uuid}.jsonl"
        _write_jsonl(
            transcript_a,
            [
                _entry(u1, None, "2026-08-07T10:00:00Z", "user", "Investigate the sync bug"),
                _entry(
                    a1,
                    u1,
                    "2026-08-07T10:00:01Z",
                    "assistant",
                    [{"type": "text", "text": "I am checking it."}],
                ),
            ],
        )
        _write_jsonl(
            transcript_b,
            [
                _entry(
                    a1,
                    u1,
                    "2026-08-07T10:00:01Z",
                    "assistant",
                    [{"type": "text", "text": "Different content."}],
                )
            ],
        )

        with (
            pytest.raises(PacketBuildError, match=STATUS_SOURCE_CHANGED),
            branch_packet(
                packet_parent=tmp_path / "packets",
                session_uuid=session_uuid,
                branch_metadata={"branch_id": 1, "leaf_uuid": a1, "source_transcript_paths": []},
                active_branch_uuids={u1, a1},
                source_files=[transcript_a, transcript_b],
                deterministic_summary={"topic": "sync bug"},
            ),
        ):
            pass

        _write_jsonl(
            transcript_b,
            [_entry(a1, u1, "2026-08-07T10:00:01Z", "assistant", [{"type": "text", "text": "I am checking it."}])],
        )

        with (
            pytest.raises(PacketBuildError, match=STATUS_SOURCE_INCOMPLETE),
            branch_packet(
                packet_parent=tmp_path / "packets",
                session_uuid=session_uuid,
                branch_metadata={"branch_id": 1, "leaf_uuid": a1, "source_transcript_paths": []},
                active_branch_uuids={u1, a1, _u("missing")},
                source_files=[transcript_a, transcript_b],
                deterministic_summary={"topic": "sync bug"},
            ),
        ):
            pass

    def test_packet_build_deduplicates_identical_duplicate_entries_and_cleans_up_on_failure(self, tmp_path):

        session_uuid = _u("packet-identical")
        u1 = _u("u1-identical")
        a1 = _u("a1-identical")
        transcript_a = tmp_path / f"{session_uuid}.jsonl"
        transcript_b = tmp_path / f"agent-{session_uuid}.jsonl"
        assistant_entry = _entry(
            a1,
            u1,
            "2026-08-07T10:00:01Z",
            "assistant",
            [{"type": "text", "text": "I am checking it."}],
        )
        _write_jsonl(
            transcript_a,
            [
                _entry(u1, None, "2026-08-07T10:00:00Z", "user", "Investigate the sync bug"),
                assistant_entry,
            ],
        )
        _write_jsonl(transcript_b, [assistant_entry])

        with branch_packet(
            packet_parent=tmp_path / "packets",
            session_uuid=session_uuid,
            branch_metadata={"branch_id": 1, "leaf_uuid": a1, "source_transcript_paths": []},
            active_branch_uuids={u1, a1},
            source_files=[transcript_a, transcript_b],
            deterministic_summary={"topic": "sync bug"},
        ) as packet_dir:
            transcript_rows = [
                json.loads(line) for line in (packet_dir / "branch-transcript.jsonl").read_text().splitlines()
            ]

        assert [row["uuid"] for row in transcript_rows] == [u1, a1]

        packet_parent = tmp_path / "cleanup-packets"
        with (
            pytest.raises(PacketBuildError, match=STATUS_SOURCE_INCOMPLETE),
            branch_packet(
                packet_parent=packet_parent,
                session_uuid=session_uuid,
                branch_metadata={"branch_id": 1, "leaf_uuid": a1, "source_transcript_paths": []},
                active_branch_uuids={u1, a1, _u("missing-cleanup")},
                source_files=[transcript_a, transcript_b],
                deterministic_summary={"topic": "sync bug"},
            ),
        ):
            pass

        assert packet_parent.exists()
        assert list(packet_parent.iterdir()) == []

    def test_packet_projection_outline_permissions_and_cleanup(self, tmp_path):

        session_uuid = _u("projected")
        u1 = _u("u1")
        a1 = _u("a1")
        t1 = _u("tool-result")
        transcript = tmp_path / f"{session_uuid}.jsonl"
        _write_jsonl(
            transcript,
            [
                _entry(u1, None, "2026-08-07T10:00:00Z", "user", "Please diagnose the failure and keep notes."),
                _entry(
                    a1,
                    u1,
                    "2026-08-07T10:00:01Z",
                    "assistant",
                    [
                        {"type": "text", "text": "I am reading the logs."},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "src/app.py"}},
                    ],
                ),
                _entry(
                    t1,
                    a1,
                    "2026-08-07T10:00:02Z",
                    "user",
                    [{"type": "tool_result", "tool_use_id": "tool-1", "content": "Traceback here", "is_error": True}],
                ),
            ],
        )

        packet_parent = tmp_path / "packets"
        packet_dir_holder = {}
        with branch_packet(
            packet_parent=packet_parent,
            session_uuid=session_uuid,
            branch_metadata={
                "branch_id": 7,
                "leaf_uuid": t1,
                "project": "demo",
                "cwd": "/tmp/demo",
                "git_branch": "main",
                "started_at": "2026-08-07T10:00:00Z",
                "ended_at": "2026-08-07T10:00:02Z",
                "exchange_count": 1,
                "files_modified": ["src/app.py"],
                "tool_counts": {"Read": 1},
                "commits": [],
                "source_transcript_paths": [str(transcript)],
            },
            active_branch_uuids={u1, a1, t1},
            source_files=[transcript],
            deterministic_summary={"topic": "sync bug"},
        ) as packet_dir:
            packet_dir_holder["path"] = packet_dir
            assert packet_dir.parent == packet_parent
            assert stat.S_IMODE(packet_parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(packet_dir.stat().st_mode) == 0o700
            transcript_rows = [
                json.loads(line) for line in (packet_dir / "branch-transcript.jsonl").read_text().splitlines()
            ]
            assert [row["uuid"] for row in transcript_rows] == [u1, a1, t1]
            assert transcript_rows[0]["request_text"] == "Please diagnose the failure and keep notes."
            assert transcript_rows[1]["assistant_text"] == "I am reading the logs."
            assert transcript_rows[1]["tool_invocations"] == [
                {"name": "Read", "summary": "[Read: src/app.py]", "file_signals": ["src/app.py"]}
            ]
            assert transcript_rows[2]["tool_results"] == [{"is_error": True, "text": "Traceback here"}]
            outline = json.loads((packet_dir / "branch-outline.json").read_text(encoding="utf-8"))
            assert outline[0]["exchange_order"] == 1
            assert outline[0]["user_uuid"] == u1
            assert outline[0]["assistant_uuids"] == [a1]
            assert outline[0]["result_uuids"] == [t1]
            assert outline[0]["user_preview"] == "Please diagnose the failure and keep notes."
            assert outline[0]["assistant_preview"] == "I am reading the logs."
            assert outline[0]["result_preview"] == "Traceback here"
            assert outline[0]["tool_signals"] == ["Read"]
            assert outline[0]["file_signals"] == ["src/app.py"]
            assert (packet_dir / "allowed-uuids.txt").read_text(encoding="utf-8").splitlines() == [u1, a1, t1]
            assert json.loads((packet_dir / "deterministic-summary.json").read_text(encoding="utf-8")) == {
                "topic": "sync bug"
            }

        assert not packet_dir_holder["path"].exists()

    def test_branch_packet_cleanup_failure_logs_path_and_error_class_only(self, tmp_path, monkeypatch, caplog):

        session_uuid = _u("cleanup-warning")
        u1 = _u("cleanup-warning-u1")
        a1 = _u("cleanup-warning-a1")
        transcript = tmp_path / f"{session_uuid}.jsonl"
        _write_jsonl(
            transcript,
            [
                _entry(u1, None, "2026-08-07T10:00:00Z", "user", "SENSITIVE-PACKET-PAYLOAD"),
                _entry(a1, u1, "2026-08-07T10:00:01Z", "assistant", "Ack"),
            ],
        )

        original_rmtree = shutil.rmtree
        packet_dir_holder: dict[str, Path] = {}

        def fake_rmtree(path, *args, **kwargs):
            candidate = Path(path)
            if packet_dir_holder.get("path") == candidate:
                raise OSError("SENSITIVE-PACKET-PAYLOAD")
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr("ccrecall.llm_summarizer.shutil.rmtree", fake_rmtree)

        with (
            caplog.at_level(logging.WARNING),
            branch_packet(
                packet_parent=tmp_path / "packets",
                session_uuid=session_uuid,
                branch_metadata={"branch_id": 1, "leaf_uuid": a1, "source_transcript_paths": []},
                active_branch_uuids={u1, a1},
                source_files=[transcript],
                deterministic_summary={"topic": "SENSITIVE-PACKET-PAYLOAD"},
            ) as packet_dir,
        ):
            packet_dir_holder["path"] = packet_dir

        assert str(packet_dir_holder["path"]) in caplog.text
        assert "OSError" in caplog.text
        assert "SENSITIVE-PACKET-PAYLOAD" not in caplog.text

    def test_capability_check_cleanup_failure_logs_path_and_error_class_only(self, tmp_path, monkeypatch, caplog):

        packet_dir = tmp_path / "capability-packet"
        cwd_dir = tmp_path / "capability-cwd"
        packet_dir.mkdir()
        cwd_dir.mkdir()
        mkdtemp_paths = [str(packet_dir), str(cwd_dir)]
        original_rmtree = shutil.rmtree

        monkeypatch.setattr("ccrecall.llm_summarizer.tempfile.mkdtemp", lambda prefix, dir=None: mkdtemp_paths.pop(0))

        def fake_rmtree(path, *args, **kwargs):
            candidate = Path(path)
            if candidate in {packet_dir, cwd_dir}:
                raise OSError("SENSITIVE-CAPABILITY-PAYLOAD")
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr("ccrecall.llm_summarizer.shutil.rmtree", fake_rmtree)

        settings = {"llm_summary_timeout_seconds": 30}

        with caplog.at_level(logging.WARNING):
            result = run_capability_check(
                settings,
                sidecar_path=tmp_path / "capability.json",
                projects_dir=tmp_path / "projects",
                claude_version="1.2.3",
                run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="{}", stderr=""),
            )

        assert result.status == STATUS_OK
        assert str(packet_dir) in caplog.text
        assert str(cwd_dir) in caplog.text
        assert "OSError" in caplog.text
        assert "SENSITIVE-CAPABILITY-PAYLOAD" not in caplog.text

    def test_packet_parser_keeps_source_line_numbers_through_shared_parsing_boundary(self, tmp_path):

        session_uuid = _u("line-numbered-parse")
        transcript = tmp_path / f"{session_uuid}.jsonl"
        u1 = _u("line-numbered-parse-u1")
        a1 = _u("line-numbered-parse-a1")
        transcript.write_text(
            "\n".join(
                [
                    "",
                    json.dumps(_entry(u1, None, "2026-08-07T10:00:00Z", "user", "First")),
                    "not json",
                    json.dumps(_entry(a1, u1, "2026-08-07T10:00:01Z", "assistant", "Second")),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        rows = _parse_source_entries(transcript)

        assert [(line_number, entry["uuid"]) for line_number, entry in rows] == [(2, u1), (4, a1)]

    def test_packet_files_are_owner_only(self, tmp_path):

        session_uuid = _u("packet-perms")
        u1 = _u("packet-perms-u1")
        transcript = tmp_path / f"{session_uuid}.jsonl"
        _write_jsonl(
            transcript,
            [_entry(u1, None, "2026-08-07T10:00:00Z", "user", "Summarize this branch")],
        )

        with branch_packet(
            packet_parent=tmp_path / "packets",
            session_uuid=session_uuid,
            branch_metadata={"branch_id": 1, "leaf_uuid": u1, "source_transcript_paths": [str(transcript)]},
            active_branch_uuids={u1},
            source_files=[transcript],
            deterministic_summary={"topic": "summary"},
        ) as packet_dir:
            for name in [
                "branch-transcript.jsonl",
                "branch-outline.json",
                "branch-metadata.json",
                "deterministic-summary.json",
                "allowed-uuids.txt",
            ]:
                assert stat.S_IMODE((packet_dir / name).stat().st_mode) == 0o600

    def test_reap_stale_packet_directories_only_removes_dead_old_packets(self, tmp_path):

        packet_parent = tmp_path / "packets"
        stale = packet_parent / "stale"
        live = packet_parent / "live"
        stale.mkdir(parents=True)
        live.mkdir(parents=True)
        (stale / "manifest.json").write_text(
            json.dumps({"pid": 999999, "created_at": "2026-01-01T00:00:00Z"}),
            encoding="utf-8",
        )
        (live / "manifest.json").write_text(
            json.dumps({"pid": os.getpid(), "created_at": "2026-01-01T00:00:00Z"}),
            encoding="utf-8",
        )

        reaped = reap_stale_packets(packet_parent, min_age_seconds=0)

        assert stale in reaped
        assert not stale.exists()
        assert live.exists()
