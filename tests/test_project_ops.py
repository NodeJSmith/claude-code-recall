"""Tests for the shared project_ops module."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from conftest import FIXTURE_DIR

from ccrecall.project_ops import key_could_match_excluded, upsert_project


class TestUpsertProjectWithCwd:
    """Verify upsert_project with direct cwd creates a project correctly."""

    def test_upsert_project_with_cwd(self, memory_db):
        """upsert_project should create a project using the provided cwd path."""

        cursor = memory_db.cursor()
        project_id, used_lossy = upsert_project(cursor, "-home-user-myrepo", cwd="/home/user/myrepo")
        memory_db.commit()

        assert project_id is not None
        assert isinstance(project_id, int)
        assert not used_lossy

        cursor.execute("SELECT path, name FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "/home/user/myrepo", "Path should match provided cwd"
        assert row[1] == "myrepo", "Name should be derived from path basename"

    def test_upsert_project_with_cwd_idempotent(self, memory_db):
        """Calling upsert_project twice with same key returns same project_id."""

        cursor = memory_db.cursor()
        id1, _ = upsert_project(cursor, "-home-user-myrepo", cwd="/home/user/myrepo")
        memory_db.commit()
        id2, _ = upsert_project(cursor, "-home-user-myrepo", cwd="/home/user/myrepo")
        memory_db.commit()

        assert id1 == id2, "Idempotent call should return same project_id"

        cursor.execute("SELECT COUNT(*) FROM projects WHERE key = ?", ("-home-user-myrepo",))
        assert cursor.fetchone()[0] == 1, "Should have exactly one project row"

    def test_upsert_project_updates_path_when_better_data(self, memory_db):
        """If project exists with stale path, upsert updates it with cwd."""

        cursor = memory_db.cursor()
        # Insert with lossy hyphen path
        cursor.execute(
            "INSERT INTO projects (path, key, name) VALUES (?, ?, ?)",
            ("/home/user/my-repo", "-home-user-my-repo", "my-repo"),
        )
        memory_db.commit()

        # Upsert with real cwd (same key but different path from cwd metadata)
        project_id, _ = upsert_project(cursor, "-home-user-my-repo", cwd="/home/user/my-repo")
        memory_db.commit()

        assert project_id is not None
        cursor.execute("SELECT path FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        # Path should be updated to the cwd value
        assert row[0] == "/home/user/my-repo"

    def test_upsert_project_with_worktree_cwd(self, memory_db):
        """Worktree cwd suffix should be normalized away."""

        cursor = memory_db.cursor()
        project_id, _ = upsert_project(
            cursor,
            "-home-user-myrepo",
            cwd="/home/user/myrepo/.claude/worktrees/my-feature",
        )
        memory_db.commit()

        cursor.execute("SELECT path FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        # normalize_cwd strips the worktree suffix
        assert row[0] == "/home/user/myrepo", "Worktree suffix should be stripped from cwd"


class TestUpsertProjectProbesJsonl:
    """Verify upsert_project uses JSONL-probe strategy when cwd is absent."""

    def test_upsert_project_probes_jsonl(self, memory_db):
        """When project_dir is given (no cwd), probe first JSONL for cwd metadata."""

        # The fixture has cwd metadata in it; let's use a real fixture directory
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "-home-user-node-banana"
            project_dir.mkdir()

            # The fixture provides cwd metadata for project-path derivation.
            shutil.copy(FIXTURE_DIR / "linear_3_exchange.jsonl", project_dir / "sess.jsonl")

            cursor = memory_db.cursor()
            # Use the encoded directory name as project_key, no cwd
            project_key = "-home-user-node-banana"
            project_id, used_lossy = upsert_project(cursor, project_key, project_dir=project_dir)
            memory_db.commit()

        assert project_id is not None
        assert not used_lossy

        cursor.execute("SELECT path, name FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        assert row is not None
        # path should come from the JSONL cwd (probed), not lossy hyphen reconstruction
        # linear_3_exchange.jsonl has cwd="/Users/samarthgupta/repos/forks/node-banana"
        assert row[0] == "/Users/samarthgupta/repos/forks/node-banana", (
            "Path should come from probed JSONL cwd, not lossy hyphen reconstruction"
        )
        assert row[1] == "node-banana"

    def test_upsert_project_does_not_probe_subagent_jsonl(self, memory_db):
        """Subagent transcripts are never a path source; a subagent-only project falls back to lossy."""

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "-home-user-node-banana"
            subagents_dir = project_dir / "subagents"
            subagents_dir.mkdir(parents=True)

            shutil.copy(FIXTURE_DIR / "linear_3_exchange.jsonl", subagents_dir / "sess.jsonl")

            cursor = memory_db.cursor()
            project_id, used_lossy = upsert_project(cursor, "-home-user-node-banana", project_dir=project_dir)
            memory_db.commit()

        assert used_lossy
        cursor.execute("SELECT path FROM projects WHERE id = ?", (project_id,))
        assert cursor.fetchone()[0] == "/home/user/node/banana"

    def test_upsert_project_falls_back_to_key_when_no_jsonl(self, memory_db):
        """When project_dir has no JSONL, fall back to lossy hyphen reconstruction."""

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "-home-user-myproject"
            project_dir.mkdir()

            cursor = memory_db.cursor()
            project_id, used_lossy = upsert_project(cursor, "-home-user-myproject", project_dir=project_dir)
            memory_db.commit()

        assert project_id is not None
        assert used_lossy

        cursor.execute("SELECT path, name FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        assert row is not None
        # Falls back to parse_project_key (lossy) when no JSONL available
        assert "myproject" in row[0], "Fallback path should contain project name from key reconstruction"

    def test_upsert_project_falls_back_to_key_with_hyphenated_name(self, memory_db):
        """Fallback path mangles hyphenated project names (lossy reconstruction)."""

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "-home-user-src-secret-client"
            project_dir.mkdir()

            cursor = memory_db.cursor()
            project_id, used_lossy = upsert_project(cursor, "-home-user-src-secret-client", project_dir=project_dir)
            memory_db.commit()

        assert used_lossy
        cursor.execute("SELECT name FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        # Lossy reconstruction turns secret-client into secret/client → name="client"
        assert row[0] == "client"

    def test_upsert_project_does_not_probe_top_level_symlink_project_dir(self, memory_db):
        """Symlink project dirs are rejected before JSONL probing."""

        with tempfile.TemporaryDirectory() as tmpdir:
            real_project_dir = Path(tmpdir) / "real-project"
            real_project_dir.mkdir()
            shutil.copy(FIXTURE_DIR / "linear_3_exchange.jsonl", real_project_dir / "sess.jsonl")
            symlink_project_dir = Path(tmpdir) / "-home-user-node-banana"
            symlink_project_dir.symlink_to(real_project_dir, target_is_directory=True)

            cursor = memory_db.cursor()
            project_id, _ = upsert_project(cursor, "-home-user-node-banana", project_dir=symlink_project_dir)
            memory_db.commit()

        cursor.execute("SELECT path FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "/home/user/node/banana"


class TestKeyCouldMatchExcluded:
    """Test the conservative key-suffix matcher for exclude_projects on the fallback path."""

    def test_hyphenated_name_matches_key_suffix(self):
        assert key_could_match_excluded("-home-u-src-secret-client", ["secret-client"])

    def test_unhyphenated_name_matches_key_suffix(self):
        assert key_could_match_excluded("-home-user-myproject", ["myproject"])

    def test_no_match_when_name_not_in_key(self):
        assert not key_could_match_excluded("-home-user-myproject", ["other-project"])

    def test_empty_exclude_list(self):
        assert not key_could_match_excluded("-home-user-myproject", [])

    def test_multiple_excludes_any_match(self):
        assert key_could_match_excluded("-home-u-src-secret-client", ["unrelated", "secret-client"])

    def test_partial_overlap_does_not_match(self):
        # "client" appears in the key but the full entry "not-secret-client" does not
        assert not key_could_match_excluded("-home-u-src-secret-client", ["not-secret-client"])

    def test_name_with_dots_matches(self):
        # A project named "my.project" encodes dots as hyphens in the key
        assert key_could_match_excluded("-home-user-my-project", ["my.project"])

    def test_exact_key_is_excluded_name(self):
        # Edge case: key is just the encoded project name with no parent dirs
        assert key_could_match_excluded("-secret-client", ["secret-client"])


class TestLossyFallbackExclusionChain:
    """Verify the full chain: lossy upsert + key-suffix match catches hyphenated names."""

    def test_lossy_fallback_reports_used_and_suffix_matches(self, memory_db):
        """When probe fails, upsert reports lossy fallback, and the key-suffix check
        catches what the exact-name match missed.
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "-home-u-src-secret-client"
            project_dir.mkdir()

            cursor = memory_db.cursor()
            _, used_lossy = upsert_project(cursor, "-home-u-src-secret-client", project_dir=project_dir)
            memory_db.commit()

            assert used_lossy

            cursor.execute("SELECT name FROM projects WHERE key = ?", ("-home-u-src-secret-client",))
            project_name = cursor.fetchone()[0]
            assert project_name == "client"  # lossy — not "secret-client"

            # Exact-name check misses it, but key-suffix catches it
            assert project_name not in ["secret-client"]
            assert key_could_match_excluded("-home-u-src-secret-client", ["secret-client"])

    def test_accurate_probe_does_not_trigger_suffix_check(self, memory_db):
        """When cwd is accurate, used_lossy_fallback is False — the suffix check
        should not fire, preventing false-positive exclusion of e.g. 'team-app'
        when only 'app' is excluded.
        """

        cursor = memory_db.cursor()
        project_id, used_lossy = upsert_project(cursor, "-home-user-repos-team-app", cwd="/home/user/repos/team-app")
        memory_db.commit()

        assert not used_lossy

        cursor.execute("SELECT name FROM projects WHERE id = ?", (project_id,))
        project_name = cursor.fetchone()[0]
        assert project_name == "team-app"

        # Exact-name check correctly does not match
        assert project_name not in ["app"]
        # The suffix check would match (false positive), but it must not fire
        # because used_lossy_fallback is False
        assert key_could_match_excluded("-home-user-repos-team-app", ["app"])


def _write_transcript(path: Path, cwd: str | None, mtime: float) -> None:
    """Write a minimal one-entry transcript (with cwd when given) and set its mtime."""
    entry = {"uuid": f"u-{path.stem}", "type": "user", "message": {"role": "user", "content": "hi"}}
    if cwd is not None:
        entry["cwd"] = cwd
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _probe(memory_db, project_dir: Path) -> tuple[str, bool]:
    cursor = memory_db.cursor()
    project_id, used_lossy = upsert_project(cursor, project_dir.name, project_dir=project_dir)
    cursor.execute("SELECT path FROM projects WHERE id = ?", (project_id,))
    return cursor.fetchone()[0], used_lossy


class TestProbeSourceRule:
    """Probe source rule (#221): the first cwd of the newest top-level session transcript."""

    def test_newest_top_level_transcript_wins(self, memory_db, tmp_path):
        project_dir = tmp_path / "-home-u-repo"
        _write_transcript(project_dir / "old.jsonl", "/home/u/old-location/repo", 1000)
        _write_transcript(project_dir / "new.jsonl", "/home/u/new-location/repo", 2000)

        assert _probe(memory_db, project_dir) == ("/home/u/new-location/repo", False)

    def test_filename_breaks_mtime_ties(self, memory_db, tmp_path):
        project_dir = tmp_path / "-home-u-repo"
        _write_transcript(project_dir / "aaa.jsonl", "/home/u/a/repo", 1000)
        _write_transcript(project_dir / "zzz.jsonl", "/home/u/z/repo", 1000)

        assert _probe(memory_db, project_dir) == ("/home/u/z/repo", False)

    def test_newer_subagent_transcripts_are_ignored(self, memory_db, tmp_path):
        project_dir = tmp_path / "-home-u-repo"
        _write_transcript(project_dir / "sess.jsonl", "/home/u/repo", 1000)
        _write_transcript(project_dir / "sess" / "subagents" / "agent-1.jsonl", "/home/u/repo/sub", 3000)
        _write_transcript(project_dir / "subagents" / "agent-2.jsonl", "/home/u/repo/sub", 3000)
        _write_transcript(project_dir / "agent-3.jsonl", "/home/u/repo/sub", 3000)

        assert _probe(memory_db, project_dir) == ("/home/u/repo", False)

    def test_walks_past_newest_file_without_cwd(self, memory_db, tmp_path):
        project_dir = tmp_path / "-home-u-repo"
        _write_transcript(project_dir / "has-cwd.jsonl", "/home/u/repo", 1000)
        _write_transcript(project_dir / "no-cwd.jsonl", None, 2000)

        assert _probe(memory_db, project_dir) == ("/home/u/repo", False)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    def test_unreadable_file_is_skipped_for_next_older(self, memory_db, tmp_path):
        project_dir = tmp_path / "-home-u-repo"
        _write_transcript(project_dir / "readable.jsonl", "/home/u/repo", 1000)
        unreadable = project_dir / "unreadable.jsonl"
        _write_transcript(unreadable, "/home/u/elsewhere/repo", 2000)
        unreadable.chmod(0)
        try:
            assert _probe(memory_db, project_dir) == ("/home/u/repo", False)
        finally:
            unreadable.chmod(0o644)

    def test_non_string_cwd_is_skipped_for_next_older(self, memory_db, tmp_path):
        project_dir = tmp_path / "-home-u-repo"
        _write_transcript(project_dir / "good.jsonl", "/home/u/repo", 1000)
        bad = project_dir / "bad.jsonl"
        bad.write_text(json.dumps({"uuid": "u-bad", "type": "user", "cwd": 42}) + "\n", encoding="utf-8")
        os.utime(bad, (2000, 2000))

        assert _probe(memory_db, project_dir) == ("/home/u/repo", False)

    def test_no_cwd_anywhere_falls_back_to_lossy(self, memory_db, tmp_path):
        project_dir = tmp_path / "-home-u-repo"
        _write_transcript(project_dir / "a.jsonl", None, 1000)

        assert _probe(memory_db, project_dir) == ("/home/u/repo", True)


class TestPathWriteRules:
    """Identity is the key; an accurate path replaces, a lossy one only inserts (#221)."""

    def test_accurate_path_replaces_stored_accurate_path(self, memory_db):
        cursor = memory_db.cursor()
        project_id, _ = upsert_project(cursor, "-home-u-repo", cwd="/home/u/old/repo-renamed")
        upsert_project(cursor, "-home-u-repo", cwd="/home/u/new/repo")

        cursor.execute("SELECT path, name FROM projects WHERE id = ?", (project_id,))
        assert cursor.fetchone() == ("/home/u/new/repo", "repo")

    def test_lossy_path_never_overwrites_existing_row(self, memory_db, tmp_path):
        cursor = memory_db.cursor()
        project_id, _ = upsert_project(cursor, "-home-u-otf-api", cwd="/home/u/otf-api")
        empty_dir = tmp_path / "-home-u-otf-api"
        empty_dir.mkdir()

        same_id, used_lossy = upsert_project(cursor, "-home-u-otf-api", project_dir=empty_dir)

        assert same_id == project_id
        assert used_lossy, "the lossy flag still reports how this call derived its path"
        cursor.execute("SELECT path, name FROM projects WHERE id = ?", (project_id,))
        assert cursor.fetchone() == ("/home/u/otf-api", "otf-api")

    def test_moved_repo_path_held_by_another_row_does_not_raise(self, memory_db):
        """The #221 crash: a moved repo's new path already belongs to an orphan row."""
        cursor = memory_db.cursor()
        orphan_id, _ = upsert_project(cursor, "-home-u-new-repo", cwd="/home/u/new/repo")
        moved_id, _ = upsert_project(cursor, "-home-u-old-repo", cwd="/home/u/old/repo")

        upsert_project(cursor, "-home-u-old-repo", cwd="/home/u/new/repo")

        cursor.execute("SELECT id, key, path FROM projects ORDER BY id")
        assert cursor.fetchall() == [
            (orphan_id, "-home-u-new-repo", "/home/u/new/repo"),
            (moved_id, "-home-u-old-repo", "/home/u/new/repo"),
        ]

    def test_insert_sharing_a_path_never_rekeys_the_existing_row(self, memory_db):
        cursor = memory_db.cursor()
        first_id, _ = upsert_project(cursor, "-home-u-personal-telegram-bot", cwd="/home/u/personal/telegram/bot")
        second_id, _ = upsert_project(cursor, "-home-u-personal-telegram-bot-x", cwd="/home/u/personal/telegram/bot")

        assert second_id != first_id
        cursor.execute("SELECT key FROM projects WHERE id = ?", (first_id,))
        assert cursor.fetchone()[0] == "-home-u-personal-telegram-bot"
