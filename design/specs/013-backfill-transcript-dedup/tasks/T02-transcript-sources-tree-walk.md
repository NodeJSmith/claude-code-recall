---
task_id: "T02"
title: "Collapse transcript_sources.py's 5 tree-walk functions onto 2 shared BFS primitives"
status: "planned"
depends_on: []
implements: ["FR#4", "FR#5", "FR#6"]
---

## Target Files

- modify: `src/ccrecall/transcript_sources.py`
- modify (only if a coverage gap surfaces): `tests/test_transcript_sources.py`

## Prompt

Read `src/ccrecall/transcript_sources.py` in full (279 lines) and `tests/test_transcript_sources.py` in full (737 lines, 12 test classes — your regression pin). The file has 5 functions that each re-implement directory BFS + symlink-safety checks:

- `_symlinked_project_contains_session_candidate(project_dir, session_uuid) -> bool`
- `_dir_contains_matching_session_transcript(path, session_uuid) -> bool`
- `_unsafe_subagent_dirs_contain_session_candidate(project_dir, projects_dir, session_uuid) -> bool`
- `_candidate_subagent_dirs(project_dir, projects_dir) -> tuple[list[Path], bool]`
- `discover_session_transcript_files(projects_dir, session_uuid) -> SessionTranscriptDiscovery` (the top-level orchestrator; uses the three above)

Extract two shared primitives per design.md's Approach section:

**Primitive 1 — subagents-dir walker.** BFS the `state/` subtree under a `project_dir`, and for every child directory literally named `"subagents"` encountered (at any depth), invoke a caller-supplied callback with that directory — whether or not the walk recurses further past it. For every *other* child directory, apply a caller-supplied policy — this is NOT simply "recurse into any non-symlink subdirectory"; the three current callers diverge on both what gates recursion and what happens when the gate fails:
  - `_symlinked_project_contains_session_candidate`'s `state_dir` walk (lines 44-59): callback for `subagents` children = "does a uuid-glob match inside this dir? if so, signal found." Non-`subagents` children: **no containment gate at all** (this function has no `projects_dir` param) — recurses into anything where `child.is_dir() and not child.is_symlink()`.
  - `_unsafe_subagent_dirs_contain_session_candidate` (lines 95-115): callback for `subagents` children = "is this dir unsafe (`child.is_symlink() or not _is_under(child, projects_dir)`)? if so, delegate to Primitive 2 to search inside it for a uuid match and signal found on a hit." Non-`subagents` children: same safety gate — on failure, do **not** recurse; instead search inside via Primitive 2 and signal found on a match. On success (safe), recurse.
  - `_candidate_subagent_dirs` (lines 173-213): callback for `subagents` children = "is this dir safe? if so, collect it; if not, flag `had_unsafe_path = True`." Non-`subagents` children: same safety gate — on failure, do **not** recurse and do **not** search; only set `had_unsafe_path = True` and drop that branch. On success, recurse.

  Design the primitive's per-child policy (e.g. a callback returning one of RECURSE / FALLBACK-SEARCH / FLAG-AND-SKIP, or an equivalent shape you find cleaner) so each of these three distinct behaviors is expressible without the primitive hardcoding any of them. `tests/test_transcript_sources.py::test_detects_unsafe_non_subagents_dir_nested_under_safe_state_tree` and `::test_flags_unsafe_non_subagents_dir_nested_under_state_without_descending` pin the fallback-search-vs-flag-only divergence — run these specifically after wiring this primitive.

  The pre-checks that run *before* entering the `state/` BFS in each of the three functions — the direct `project_dir / f"{uuid}.jsonl"` check (only in `_symlinked_project_contains_session_candidate`), the direct `project_dir / "subagents"` check, and the `state_dir` existence check — stay in each thin wrapper, not inside the primitive; they're guard clauses around the walk, not the walk itself.

**Primitive 2 — directory file-search.** BFS an arbitrary directory (no name-gating) glob-matching `*{session_uuid}*.jsonl` at every level, short-circuiting on the first match. This is exactly today's `_dir_contains_matching_session_transcript` — keep it as its own function (it's also called directly, not just as Primitive 1's helper) but confirm Primitive 1's search-mode callbacks reuse it rather than re-implementing the same BFS inline.

**Preserve FR#6 exactly** — both of its inconsistencies, not just the first: (1) `_symlinked_project_contains_session_candidate` currently dedupes its `visited` set by *resolved* path (via `_resolved_path()`, which suppresses `OSError`/`RuntimeError`), while `_unsafe_subagent_dirs_contain_session_candidate` and `_candidate_subagent_dirs` dedupe by the *raw* `Path` object (no `.resolve()` call); (2) the non-`subagents`-child gate/fallback divergence described above. When these three collapse onto Primitive 1, make both a parameter (e.g. `dedupe_by_resolved_path: bool` for the first, and the per-child policy callback described above for the second) so each caller keeps its current behavior. Do not standardize on either — for the dedup strategy that would be an unrequested behavior change the existing tests may not catch either way (symlink cycles are the case where it matters); for the gate/fallback divergence, the two named tests above WILL catch it.

Do not change `is_safe_project_dir`, `_is_safe_transcript_file`, `_is_under`, `_resolved_path`, `_dedupe_paths`, `discover_project_transcript_files`, or `discover_importable_transcript_files` — no duplication to remove there. Do not change the signature or behavior of `discover_session_transcript_files` — it's called from `src/ccrecall/hooks/sync_current.py:94` and must keep returning the same `SessionTranscriptDiscovery` shape for the same inputs.

Run `uv run pytest tests/test_transcript_sources.py -v` after each extraction step (one primitive at a time), not just once at the end — this file's logic has enough subtle variants (per-uuid matching vs. all-files, symlink-unsafe-but-still-search semantics) that a single big-bang rewrite risks masking which change broke what.

## Verify

- [ ] FR#4: `_symlinked_project_contains_session_candidate`, `_unsafe_subagent_dirs_contain_session_candidate`, and `_candidate_subagent_dirs` are now thin wrappers around a shared walking primitive (reviewer can point at the shared function each calls).
- [ ] FR#5: `discover_session_transcript_files(projects_dir, session_uuid)`, `discover_project_transcript_files(project_dir, projects_dir)`, and `is_safe_project_dir(project_dir, projects_dir)` have unchanged signatures — confirm by re-reading their call sites in `src/ccrecall/hooks/import_conversations.py`, `src/ccrecall/hooks/sync_current.py`, `src/ccrecall/project_ops.py` and confirming no changes were needed there. `discover_importable_transcript_files(projects_dir)` has no caller outside the test suite (`tests/test_transcript_sources.py`) — confirm its signature is unchanged by re-reading its own definition and test usages instead, since there's no external call site to check.
- [ ] FR#6: Both preserved inconsistencies are verifiable in the diff: (a) the resolved-path vs. raw-path dedup distinction still exists between the three collapsed call sites (grep for `.resolve()` usage, confirm it's still gated the same way per caller); (b) `uv run pytest tests/test_transcript_sources.py::TestUnsafeSubagentDirsContainSessionCandidate::test_detects_unsafe_non_subagents_dir_nested_under_safe_state_tree tests/test_transcript_sources.py::TestCandidateSubagentDirs::test_flags_unsafe_non_subagents_dir_nested_under_state_without_descending -v` both pass (adjust the `::Class::test` path if the actual test lives in a different class — confirm via the test file's class list first).
- [ ] AC#2: `uv run pytest tests/test_transcript_sources.py` passes with 0 failures, no test file edits beyond an added characterization test if a gap was found.
- [ ] AC#5: `grep -c "^def \|^    def " src/ccrecall/transcript_sources.py` before/after shows a real reduction (5 tree-walk functions → primitives + thin wrappers, not just renamed).
- [ ] AC#3 (partial — full suite verified in the final gate): `uv run pytest` passes.
- [ ] AC#4 (partial): `uvx prek run --all-files` passes for the files this task touches.
