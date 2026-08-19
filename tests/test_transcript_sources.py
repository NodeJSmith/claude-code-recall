"""Characterization tests for transcript_sources.py path-safety logic.

These pin *current* behavior (including surprising edge cases) so a later
refactor has a safety net. Comments flag behavior that looks surprising but
is confirmed intentional/observed, not assumed.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ccrecall.transcript_sources import (
    _candidate_subagent_dirs,
    _ChildAction,
    _dedupe_paths,
    _dir_contains_matching_session_transcript,
    _is_safe_transcript_file,
    _is_under,
    _resolved_path,
    _symlinked_project_contains_session_candidate,
    _unsafe_subagent_dirs_contain_session_candidate,
    _walk_subagents_dirs,
    discover_importable_transcript_files,
    discover_project_transcript_files,
    discover_session_transcript_files,
    is_safe_project_dir,
)


class TestIsSafeProjectDir:
    def test_normal_dir_is_safe(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        project_dir.mkdir(parents=True)

        assert is_safe_project_dir(project_dir, projects_dir) is True

    def test_symlinked_dir_is_unsafe(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        real_target = tmp_path / "elsewhere"
        real_target.mkdir()
        symlinked_project = projects_dir / "proj1"
        symlinked_project.symlink_to(real_target, target_is_directory=True)

        assert is_safe_project_dir(symlinked_project, projects_dir) is False

    def test_nonexistent_dir_is_unsafe(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        missing = projects_dir / "does-not-exist"

        assert is_safe_project_dir(missing, projects_dir) is False

    def test_dir_outside_projects_dir_is_unsafe(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        outside = tmp_path / "outside" / "proj1"
        outside.mkdir(parents=True)

        assert is_safe_project_dir(outside, projects_dir) is False

    def test_file_instead_of_dir_is_unsafe(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        as_file = projects_dir / "proj1"
        as_file.write_text("not a directory")

        assert is_safe_project_dir(as_file, projects_dir) is False


class TestIsSafeTranscriptFile:
    def test_normal_file_is_safe(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        transcript = projects_dir / "session.jsonl"
        transcript.write_text("{}")

        assert _is_safe_transcript_file(transcript, projects_dir) is True

    def test_symlinked_file_is_unsafe(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        real_target = tmp_path / "real.jsonl"
        real_target.write_text("{}")
        symlinked_file = projects_dir / "session.jsonl"
        symlinked_file.symlink_to(real_target)

        assert _is_safe_transcript_file(symlinked_file, projects_dir) is False

    def test_nonexistent_file_is_unsafe(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        missing = projects_dir / "missing.jsonl"

        assert _is_safe_transcript_file(missing, projects_dir) is False

    def test_file_outside_projects_dir_is_unsafe(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        outside = tmp_path / "outside.jsonl"
        outside.write_text("{}")

        assert _is_safe_transcript_file(outside, projects_dir) is False

    def test_directory_instead_of_file_is_unsafe(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        as_dir = projects_dir / "session.jsonl"
        as_dir.mkdir()

        assert _is_safe_transcript_file(as_dir, projects_dir) is False


class TestIsUnder:
    def test_child_path_under_base_is_true(self, tmp_path):
        base = tmp_path / "base"
        child = base / "child" / "grandchild"
        child.mkdir(parents=True)

        assert _is_under(child, base) is True

    def test_path_outside_base_is_false(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        assert _is_under(outside, base) is False

    def test_symlinked_path_resolving_outside_base_is_false(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        outside_target = tmp_path / "outside"
        outside_target.mkdir()
        link = base / "link"
        link.symlink_to(outside_target, target_is_directory=True)

        # Resolution follows the symlink to its real (outside) location.
        assert _is_under(link, base) is False

    def test_symlinked_path_resolving_inside_base_is_true(self, tmp_path):
        base = tmp_path / "base"
        real_target = base / "real"
        real_target.mkdir(parents=True)
        link = base / "link"
        link.symlink_to(real_target, target_is_directory=True)

        assert _is_under(link, base) is True


class TestDedupePaths:
    def test_preserves_order(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        c = tmp_path / "c"

        assert _dedupe_paths([c, a, b]) == [c, a, b]

    def test_removes_duplicates(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"

        assert _dedupe_paths([a, b, a, a, b]) == [a, b]

    def test_empty_list(self):
        assert _dedupe_paths([]) == []


class TestWalkSubagentsDirsChildIsolation:
    """Pins the per-child OSError isolation introduced when the per-child try/except
    was split out of `_walk_subagents_dirs`' loop body into `_process_subagents_walk_child`
    (to avoid ruff PERF203). An `OSError` while processing one child no longer aborts the
    rest of that directory's listing — only the failing child is skipped, and siblings are
    still walked. This is narrower isolation than the original `contextlib.suppress(OSError)`,
    which wrapped the whole per-directory loop.
    """

    def test_oserror_on_one_child_does_not_abort_siblings(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "a").mkdir()
        (state_dir / "b").mkdir()
        (state_dir / "c").mkdir()

        visited: list[str] = []

        def non_subagents_policy(child: Path) -> _ChildAction:
            if child.name == "b":
                raise OSError("simulated failure")
            visited.append(child.name)
            return _ChildAction.SKIP

        result = _walk_subagents_dirs(
            state_dir,
            on_subagents_dir=lambda _child: False,
            non_subagents_policy=non_subagents_policy,
            dedupe_by_resolved_path=True,
        )

        assert result is False
        # "a" and "c" were both reached despite "b" raising -- proves the
        # isolation is per-child, not per-directory-listing.
        assert set(visited) == {"a", "c"}


class TestResolvedPath:
    def test_returns_resolved_path(self, tmp_path):
        target = tmp_path / "sub"
        target.mkdir()

        assert _resolved_path(target) == target.resolve()

    @pytest.mark.skipif(sys.version_info < (3, 13), reason="Path.resolve() no longer raises on symlink loops from 3.13")
    def test_self_referential_symlink_resolves_without_raising(self, tmp_path):
        # On Python 3.13+, Path.resolve() no longer raises on a
        # self-referential symlink loop — it returns the path unresolved
        # past the loop point.
        loop = tmp_path / "loop"
        loop.symlink_to(loop)

        assert _resolved_path(loop) is not None

    @pytest.mark.skipif(
        sys.version_info >= (3, 13), reason="Path.resolve() no longer raises on symlink loops from 3.13"
    )
    def test_self_referential_symlink_returns_none_pre_313(self, tmp_path):
        # On 3.11/3.12, Path.resolve() raises RuntimeError for a
        # self-referential symlink loop, which this function is built to
        # suppress, returning None.
        loop = tmp_path / "loop"
        loop.symlink_to(loop)

        assert _resolved_path(loop) is None

    def test_returns_none_when_resolve_raises_oserror(self, tmp_path):
        target = tmp_path / "sub"
        target.mkdir()

        with patch.object(Path, "resolve", side_effect=OSError("boom")):
            assert _resolved_path(target) is None

    def test_returns_none_when_resolve_raises_runtimeerror(self, tmp_path):
        target = tmp_path / "sub"
        target.mkdir()

        with patch.object(Path, "resolve", side_effect=RuntimeError("boom")):
            assert _resolved_path(target) is None


class TestDirContainsMatchingSessionTranscript:
    def test_finds_matching_file_directly(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        (target_dir / "abc-123.jsonl").write_text("{}")

        assert _dir_contains_matching_session_transcript(target_dir, "abc-123") is True

    def test_finds_matching_file_in_subdirectory(self, tmp_path):
        target_dir = tmp_path / "target"
        nested = target_dir / "nested"
        nested.mkdir(parents=True)
        (nested / "abc-123.jsonl").write_text("{}")

        assert _dir_contains_matching_session_transcript(target_dir, "abc-123") is True

    def test_no_match_returns_false(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        (target_dir / "other-session.jsonl").write_text("{}")

        assert _dir_contains_matching_session_transcript(target_dir, "abc-123") is False

    def test_does_not_descend_into_symlinked_subdirectories(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        real_nested = tmp_path / "real_nested"
        real_nested.mkdir()
        (real_nested / "abc-123.jsonl").write_text("{}")
        (target_dir / "nested_link").symlink_to(real_nested, target_is_directory=True)

        assert _dir_contains_matching_session_transcript(target_dir, "abc-123") is False

    def test_matches_dangling_symlink_by_name(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        dangling = target_dir / "abc-123.jsonl"
        dangling.symlink_to(tmp_path / "missing-target.jsonl")

        # A dangling symlink matches by is_symlink() even though it can't be opened.
        assert _dir_contains_matching_session_transcript(target_dir, "abc-123") is True


class TestDiscoverProjectTranscriptFiles:
    def test_normal_project_dir_with_jsonl_files(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        project_dir.mkdir(parents=True)
        (project_dir / "session1.jsonl").write_text("{}")
        (project_dir / "session2.jsonl").write_text("{}")

        result = discover_project_transcript_files(project_dir, projects_dir)

        assert sorted(p.name for p in result.files) == ["session1.jsonl", "session2.jsonl"]
        assert result.had_unsafe_path is False

    def test_project_dir_with_subagents_subdirectory(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        subagents = project_dir / "subagents"
        subagents.mkdir(parents=True)
        (project_dir / "main.jsonl").write_text("{}")
        (subagents / "sub1.jsonl").write_text("{}")

        result = discover_project_transcript_files(project_dir, projects_dir)

        names = sorted(p.name for p in result.files)
        assert names == ["main.jsonl", "sub1.jsonl"]
        assert result.had_unsafe_path is False

    def test_symlinked_project_dir_returns_empty_and_unsafe(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        real_target = tmp_path / "elsewhere"
        real_target.mkdir()
        (real_target / "session.jsonl").write_text("{}")
        symlinked_project = projects_dir / "proj1"
        symlinked_project.symlink_to(real_target, target_is_directory=True)

        result = discover_project_transcript_files(symlinked_project, projects_dir)

        assert result.files == []
        assert result.had_unsafe_path is True

    def test_nonexistent_project_dir_returns_empty(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        missing = projects_dir / "does-not-exist"

        result = discover_project_transcript_files(missing, projects_dir)

        assert result.files == []
        # Not flagged unsafe — this branch is a plain "nothing to find" exit,
        # distinct from the is_safe_project_dir() failure branch below.
        assert result.had_unsafe_path is False

    def test_symlinked_transcript_file_is_skipped_and_flagged(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        project_dir.mkdir(parents=True)
        real_target = tmp_path / "real.jsonl"
        real_target.write_text("{}")
        (project_dir / "session.jsonl").symlink_to(real_target)

        result = discover_project_transcript_files(project_dir, projects_dir)

        assert result.files == []
        assert result.had_unsafe_path is True


class TestDiscoverSessionTranscriptFiles:
    def test_finds_session_across_multiple_project_dirs(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj1 = projects_dir / "proj1"
        proj2 = projects_dir / "proj2"
        proj1.mkdir(parents=True)
        proj2.mkdir(parents=True)
        (proj1 / "other-uuid.jsonl").write_text("{}")
        (proj2 / "target-uuid.jsonl").write_text("{}")

        result = discover_session_transcript_files(projects_dir, "target-uuid")

        assert [p.name for p in result.files] == ["target-uuid.jsonl"]
        assert result.had_unsafe_path is False
        assert result.had_matching_unsafe_path is False

    def test_skips_symlinked_project_dir_and_sets_matching_flag(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        real_target = tmp_path / "elsewhere"
        real_target.mkdir()
        (real_target / "target-uuid.jsonl").write_text("{}")
        symlinked_project = projects_dir / "proj1"
        symlinked_project.symlink_to(real_target, target_is_directory=True)

        result = discover_session_transcript_files(projects_dir, "target-uuid")

        assert result.files == []
        assert result.had_unsafe_path is True
        assert result.had_matching_unsafe_path is True

    def test_skips_symlinked_project_dir_without_matching_session(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        real_target = tmp_path / "elsewhere"
        real_target.mkdir()
        (real_target / "other-uuid.jsonl").write_text("{}")
        symlinked_project = projects_dir / "proj1"
        symlinked_project.symlink_to(real_target, target_is_directory=True)

        result = discover_session_transcript_files(projects_dir, "target-uuid")

        assert result.files == []
        assert result.had_unsafe_path is True
        assert result.had_matching_unsafe_path is False

    def test_finds_subagent_transcript_matching_uuid(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        subagents = project_dir / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "prefix-target-uuid-suffix.jsonl").write_text("{}")

        result = discover_session_transcript_files(projects_dir, "target-uuid")

        assert [p.name for p in result.files] == ["prefix-target-uuid-suffix.jsonl"]
        assert result.had_unsafe_path is False
        assert result.had_matching_unsafe_path is False

    def test_nonexistent_projects_dir_returns_empty(self, tmp_path):
        missing = tmp_path / "does-not-exist"

        result = discover_session_transcript_files(missing, "target-uuid")

        assert result.files == []
        assert result.had_unsafe_path is False

    def test_dangling_symlink_named_for_session_is_unsafe_match(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        project_dir.mkdir(parents=True)
        dangling = project_dir / "target-uuid.jsonl"
        dangling.symlink_to(tmp_path / "missing.jsonl")

        result = discover_session_transcript_files(projects_dir, "target-uuid")

        # exists() is False for a dangling symlink, but is_symlink() is True,
        # so the "exists() or is_symlink()" check still enters the branch,
        # then _is_safe_transcript_file() rejects it (exists() is False).
        assert result.files == []
        assert result.had_unsafe_path is True
        assert result.had_matching_unsafe_path is True

    def test_file_where_project_dir_expected_is_skipped(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "not-a-dir").write_text("stray file")

        result = discover_session_transcript_files(projects_dir, "target-uuid")

        assert result.files == []
        assert result.had_unsafe_path is False
        assert result.had_matching_unsafe_path is False

    @pytest.mark.skipif(
        sys.version_info < (3, 13), reason="pathlib.glob() rejects '**' embedded in a component before 3.13"
    )
    def test_glob_metacharacters_in_session_uuid_over_match(self, tmp_path):
        # session_uuid is interpolated into glob patterns (f"*{session_uuid}*.jsonl")
        # without escaping. A uuid of "*" produces the pattern "***.jsonl", which
        # pathlib's glob() treats as an ordinary wildcard on 3.13+, over-matching
        # every .jsonl in subagent dirs. Pinning that gap so it's visible, not hidden.
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        subagents = project_dir / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "session-aaa.jsonl").write_text("{}")
        (subagents / "session-bbb.jsonl").write_text("{}")

        result = discover_session_transcript_files(projects_dir, "*")

        assert len(result.files) == 2

    @pytest.mark.skipif(
        sys.version_info >= (3, 13), reason="pathlib.glob() rejects '**' embedded in a component before 3.13"
    )
    def test_glob_metacharacters_in_session_uuid_raises_pre_313(self, tmp_path):
        # Same "*" session_uuid as the sibling test above, but on 3.11/3.12
        # pathlib rejects the resulting "***.jsonl" pattern outright instead of
        # over-matching, and discover_session_transcript_files does not catch
        # it, so the ValueError propagates. Pinning the pre-3.13 behavior.
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        subagents = project_dir / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "session-aaa.jsonl").write_text("{}")
        (subagents / "session-bbb.jsonl").write_text("{}")

        with pytest.raises(ValueError, match="can only be an entire path component"):
            discover_session_transcript_files(projects_dir, "*")

    def test_path_traversal_in_session_uuid_is_rejected(self, tmp_path):
        # A session_uuid containing "../" doesn't escape the projects_dir boundary
        # because _is_safe_transcript_file's _is_under check uses resolve().
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        project_dir.mkdir(parents=True)
        outside = tmp_path / "secret.jsonl"
        outside.write_text("{}")

        result = discover_session_transcript_files(projects_dir, "../../secret")

        assert result.files == []

    def test_symlinked_matching_file_inside_safe_subagents_dir_is_unsafe(self, tmp_path):
        # The subagents dir itself is safe (real, under projects_dir), but the
        # matching transcript file inside it is a symlink.
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        subagents = project_dir / "subagents"
        subagents.mkdir(parents=True)
        real_target = tmp_path / "real.jsonl"
        real_target.write_text("{}")
        (subagents / "target-uuid.jsonl").symlink_to(real_target)

        result = discover_session_transcript_files(projects_dir, "target-uuid")

        assert result.files == []
        assert result.had_unsafe_path is True
        assert result.had_matching_unsafe_path is True


class TestDiscoverImportableTranscriptFiles:
    def test_collects_all_jsonl_from_all_project_dirs(self, tmp_path):
        projects_dir = tmp_path / "projects"
        proj1 = projects_dir / "proj1"
        proj2 = projects_dir / "proj2"
        proj1.mkdir(parents=True)
        proj2.mkdir(parents=True)
        (proj1 / "a.jsonl").write_text("{}")
        (proj2 / "b.jsonl").write_text("{}")

        result = discover_importable_transcript_files(projects_dir)

        assert sorted(p.name for p in result.files) == ["a.jsonl", "b.jsonl"]
        assert result.had_unsafe_path is False

    def test_skips_symlinked_project_dirs(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        real_target = tmp_path / "elsewhere"
        real_target.mkdir()
        (real_target / "session.jsonl").write_text("{}")
        symlinked_project = projects_dir / "proj1"
        symlinked_project.symlink_to(real_target, target_is_directory=True)

        result = discover_importable_transcript_files(projects_dir)

        assert result.files == []
        assert result.had_unsafe_path is True

    def test_nonexistent_projects_dir_returns_empty(self, tmp_path):
        missing = tmp_path / "does-not-exist"

        result = discover_importable_transcript_files(missing)

        assert result.files == []
        assert result.had_unsafe_path is False

    def test_file_where_project_dir_expected_is_skipped(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "not-a-dir").write_text("stray file")

        result = discover_importable_transcript_files(projects_dir)

        assert result.files == []
        assert result.had_unsafe_path is False


class TestSymlinkedProjectContainsSessionCandidate:
    def test_detects_session_in_direct_file(self, tmp_path):
        project_dir = tmp_path / "proj1"
        project_dir.mkdir()
        (project_dir / "target-uuid.jsonl").write_text("{}")

        assert _symlinked_project_contains_session_candidate(project_dir, "target-uuid") is True

    def test_detects_session_in_subagents_dir(self, tmp_path):
        project_dir = tmp_path / "proj1"
        subagents = project_dir / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "prefix-target-uuid.jsonl").write_text("{}")

        assert _symlinked_project_contains_session_candidate(project_dir, "target-uuid") is True

    def test_detects_session_in_state_tree(self, tmp_path):
        project_dir = tmp_path / "proj1"
        nested_subagents = project_dir / "state" / "nested" / "subagents"
        nested_subagents.mkdir(parents=True)
        (nested_subagents / "target-uuid.jsonl").write_text("{}")

        assert _symlinked_project_contains_session_candidate(project_dir, "target-uuid") is True

    def test_no_match_returns_false(self, tmp_path):
        project_dir = tmp_path / "proj1"
        project_dir.mkdir()
        (project_dir / "other-uuid.jsonl").write_text("{}")

        assert _symlinked_project_contains_session_candidate(project_dir, "target-uuid") is False


class TestUnsafeSubagentDirsContainSessionCandidate:
    def test_detects_session_in_unsafe_direct_subagents_symlink(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        project_dir.mkdir(parents=True)
        real_subagents = tmp_path / "real_subagents"
        real_subagents.mkdir()
        (real_subagents / "target-uuid.jsonl").write_text("{}")
        (project_dir / "subagents").symlink_to(real_subagents, target_is_directory=True)

        result = _unsafe_subagent_dirs_contain_session_candidate(project_dir, projects_dir, "target-uuid")

        assert result is True

    def test_safe_direct_subagents_dir_not_flagged_here(self, tmp_path):
        # A normal (safe) subagents dir is this function's non-match case —
        # safe dirs are handled by the separate _candidate_subagent_dirs() path.
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        subagents = project_dir / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "target-uuid.jsonl").write_text("{}")

        result = _unsafe_subagent_dirs_contain_session_candidate(project_dir, projects_dir, "target-uuid")

        assert result is False

    def test_detects_session_in_unsafe_state_dir_symlink(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        project_dir.mkdir(parents=True)
        real_state = tmp_path / "real_state"
        real_state.mkdir()
        (real_state / "target-uuid.jsonl").write_text("{}")
        (project_dir / "state").symlink_to(real_state, target_is_directory=True)

        result = _unsafe_subagent_dirs_contain_session_candidate(project_dir, projects_dir, "target-uuid")

        assert result is True

    def test_no_subagents_or_state_returns_false(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        project_dir.mkdir(parents=True)

        result = _unsafe_subagent_dirs_contain_session_candidate(project_dir, projects_dir, "target-uuid")

        assert result is False

    def test_detects_unsafe_subagents_dir_nested_under_safe_state_tree(self, tmp_path):
        # state_dir itself is safe, but a "subagents"-named dir two levels
        # deep is a symlink — the BFS walk must still catch it.
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        nested = project_dir / "state" / "nested"
        nested.mkdir(parents=True)
        real_subagents = tmp_path / "real_nested_subagents"
        real_subagents.mkdir()
        (real_subagents / "target-uuid.jsonl").write_text("{}")
        (nested / "subagents").symlink_to(real_subagents, target_is_directory=True)

        result = _unsafe_subagent_dirs_contain_session_candidate(project_dir, projects_dir, "target-uuid")

        assert result is True

    def test_detects_unsafe_non_subagents_dir_nested_under_safe_state_tree(self, tmp_path):
        # A non-"subagents"-named dir nested in the state tree that is itself
        # a symlink is also checked for a matching transcript.
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        nested = project_dir / "state" / "nested"
        nested.mkdir(parents=True)
        real_dir = tmp_path / "real_other_dir"
        real_dir.mkdir()
        (real_dir / "target-uuid.jsonl").write_text("{}")
        (nested / "other").symlink_to(real_dir, target_is_directory=True)

        result = _unsafe_subagent_dirs_contain_session_candidate(project_dir, projects_dir, "target-uuid")

        assert result is True


class TestCandidateSubagentDirs:
    def test_returns_safe_direct_subagents_dir(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        subagents = project_dir / "subagents"
        subagents.mkdir(parents=True)

        candidates, had_unsafe_path = _candidate_subagent_dirs(project_dir, projects_dir)

        assert candidates == [subagents]
        assert had_unsafe_path is False

    def test_flags_unsafe_symlinked_direct_subagents_dir(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        project_dir.mkdir(parents=True)
        real_target = tmp_path / "real_subagents"
        real_target.mkdir()
        (project_dir / "subagents").symlink_to(real_target, target_is_directory=True)

        candidates, had_unsafe_path = _candidate_subagent_dirs(project_dir, projects_dir)

        assert candidates == []
        assert had_unsafe_path is True

    def test_returns_nested_safe_subagents_dirs_under_state(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        nested_subagents = project_dir / "state" / "nested" / "subagents"
        nested_subagents.mkdir(parents=True)

        candidates, had_unsafe_path = _candidate_subagent_dirs(project_dir, projects_dir)

        assert candidates == [nested_subagents]
        assert had_unsafe_path is False

    def test_flags_unsafe_symlinked_state_dir_and_stops_traversal(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        project_dir.mkdir(parents=True)
        real_state = tmp_path / "real_state"
        (real_state / "subagents").mkdir(parents=True)
        (project_dir / "state").symlink_to(real_state, target_is_directory=True)

        candidates, had_unsafe_path = _candidate_subagent_dirs(project_dir, projects_dir)

        # The whole state dir is unsafe (symlinked) — early-return, no descent
        # into it even though it contains a subagents dir.
        assert candidates == []
        assert had_unsafe_path is True

    def test_no_subagents_or_state_returns_empty(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        project_dir.mkdir(parents=True)

        candidates, had_unsafe_path = _candidate_subagent_dirs(project_dir, projects_dir)

        assert candidates == []
        assert had_unsafe_path is False

    def test_flags_unsafe_subagents_dir_nested_two_levels_under_state(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        nested = project_dir / "state" / "nested"
        nested.mkdir(parents=True)
        real_subagents = tmp_path / "real_nested_subagents"
        real_subagents.mkdir()
        (nested / "subagents").symlink_to(real_subagents, target_is_directory=True)

        candidates, had_unsafe_path = _candidate_subagent_dirs(project_dir, projects_dir)

        assert candidates == []
        assert had_unsafe_path is True

    def test_flags_unsafe_non_subagents_dir_nested_under_state_without_descending(self, tmp_path):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "proj1"
        nested = project_dir / "state" / "nested"
        nested.mkdir(parents=True)
        real_dir = tmp_path / "real_other_dir"
        (real_dir / "subagents").mkdir(parents=True)
        (nested / "other").symlink_to(real_dir, target_is_directory=True)

        candidates, had_unsafe_path = _candidate_subagent_dirs(project_dir, projects_dir)

        # The symlinked dir is flagged unsafe but never traversed into, so the
        # subagents dir inside it is never discovered as a candidate.
        assert candidates == []
        assert had_unsafe_path is True
