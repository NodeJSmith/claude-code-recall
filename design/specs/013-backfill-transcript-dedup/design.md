# Design: Dedupe backfill run() scaffolding and transcript tree-walk logic

**Date:** 2026-08-18
**Status:** archived
**Mode:** sketch

## Problem

Two unrelated areas of the codebase have accumulated structural duplication that makes them harder to maintain consistently: (1) `backfill_embeddings.run()` (274 lines) and `backfill_tool_content.run()` (206 lines) each hand-roll a near-identical batch-loop body — ~131 lines and ~108 lines respectively — with duplicated scaffolding (limit handling, stuck-batch detection, commit/sleep trailer) around otherwise-different per-item logic, and (2) `transcript_sources.py` has 5 separate tree-walking functions that each re-implement directory BFS + symlink-safety bookkeeping with 4-5 levels of nesting. Both are tracked as issues #138 and #139; this sketch covers both since they're combined on this branch and are each medium, well-precedented refactors.

## Goals

- Extract the genuinely-identical scaffolding from the two backfill `run()` functions into a shared driver, without forcing unification of the parts that legitimately differ (per-item error taxonomy, no-progress recovery action, rate computation).
- Collapse `transcript_sources.py`'s 5 tree-walk functions onto one or two shared traversal primitives, preserving exact current behavior including its existing (and non-obvious) inconsistencies.
- Zero behavior change in both cases — verified by the existing test suites, not just green CI.

## Functional Requirements

- **FR#1** `backfill_embeddings.run()` and `backfill_tool_content.run()` share one driver for the outer batch loop (select → break-if-empty → `--limit` cutoff → stuck-batch detection → dispatch to per-item processing → batch trailer), each supplying its own per-item processing, exception handling, and progress-message content via callbacks.
- **FR#2** The no-progress ("stuck batch") *detection* (`current_ids == last_batch_ids`) is shared; the *recovery action* (embeddings aborts the whole run, tool_content excludes the stuck ids and continues) stays a per-caller policy, not collapsed into one behavior.
- **FR#3** The batch trailer (`conn.commit()`, then domain-specific extras — `reclaim_memory` + fixed sleep for embeddings, sleep-only for tool_content) is invoked via a shared hook, preserving that embeddings reclaims memory between batches and tool_content does not.
- **FR#4** `transcript_sources.py`'s tree-walk functions share one parameterized BFS primitive for "walk the `state/` tree looking for `subagents`-named directories, doing X when one is found" (currently duplicated across `_symlinked_project_contains_session_candidate`'s state-dir portion, `_unsafe_subagent_dirs_contain_session_candidate`, and `_candidate_subagent_dirs`), and a second shared primitive for "BFS an arbitrary directory for uuid-matching files" (currently `_dir_contains_matching_session_transcript`, reused inside the first primitive's callback).
- **FR#5** The 4 public entry points (`discover_session_transcript_files`, `discover_project_transcript_files`, `discover_importable_transcript_files`, `is_safe_project_dir`) keep their existing signatures and behavior. Three of them are verified against their current callers (`hooks/import_conversations.py`, `hooks/sync_current.py`, `project_ops.py`); `discover_importable_transcript_files` has no caller outside `tests/test_transcript_sources.py`, so it's verified via its own tests instead.
- **FR#6** Two existing per-caller behavioral inconsistencies in the `state/`-tree walk are preserved exactly as-is, not silently unified, when the three call sites collapse onto the shared walker:
  - **Dedup strategy**: `_symlinked_project_contains_session_candidate` dedupes its `visited` set by *resolved* path via `_resolved_path()`; `_unsafe_subagent_dirs_contain_session_candidate` and `_candidate_subagent_dirs` dedupe by the *raw* `Path` object (no `.resolve()`).
  - **Non-`subagents` child-dir gate and fallback action**: when the walk meets a child directory that is *not* named `subagents`, the three callers diverge on what gates further recursion and what happens when that gate fails. `_symlinked_project_contains_session_candidate` has no `projects_dir` parameter at all — it recurses into any child that `is_dir() and not child.is_symlink()`, no containment check. `_unsafe_subagent_dirs_contain_session_candidate` gates on `child.is_symlink() or not _is_under(child, projects_dir)`: on failure it does *not* recurse — it instead searches inside that child via `_dir_contains_matching_session_transcript` and returns `True` on a match. `_candidate_subagent_dirs` gates on the same condition but on failure does *not* recurse and does *not* search — it only sets `had_unsafe_path = True` and drops that branch. `tests/test_transcript_sources.py::test_detects_unsafe_non_subagents_dir_nested_under_safe_state_tree` and `::test_flags_unsafe_non_subagents_dir_nested_under_state_without_descending` pin this distinction — treat both as part of the primitive's per-caller parameterization, not just the dedup strategy.

## Acceptance Criteria

- **AC#1** `uv run pytest tests/test_backfill_embeddings.py tests/test_backfill_tool_content.py` passes with no test-file changes required beyond import-path updates (both suites currently pass on main; this pins FR#1-#3).
- **AC#2** `uv run pytest tests/test_transcript_sources.py` passes unchanged (this pins FR#4-#6; the existing 737-line/12-class suite is the safety net — no new tests required, but add characterization coverage first if the comb or executor finds a gap while extracting the primitive).
- **AC#3** `uv run pytest` (full suite) passes.
- **AC#4** `uvx prek run --all-files` passes (no lazy imports, no `Optional[X]`, lint/type-check clean).
- **AC#5** `grep -c "^def \|^    def " src/ccrecall/transcript_sources.py` (or equivalent visual check) shows the 5 duplicated tree-walk functions reduced to the shared primitive(s) plus thin per-caller wrappers — a structural, reviewer-verifiable reduction, not just a passing test suite.

## Approach

### Backfill run() scaffolding (issue #138)

New module `src/ccrecall/hooks/backfill_runner.py`, alongside the existing `backfill_query.py` (constants/selection) and `backfill_status.py` (status reporting) that both `run()` functions already import from — same layering, new concern.

Owns:
- `run_batch_loop(*, select_batch, process_batch, no_progress_policy, after_batch, is_limit_reached)` — the outer `while True: select → break-if-empty → stuck check → process_batch(rows) → after_batch()` skeleton that both `run()` functions currently duplicate line-for-line. `no_progress_policy` distinguishes "abort" (embeddings) from "exclude and continue" (tool_content) — the *detection* is shared, the *action* is not (FR#2).
- Each `run()` keeps its own per-item loop, SAVEPOINT handling, exception taxonomy (embeddings: `ValueError/OverflowError/UnicodeError` → content-error sentinel, `Exception` → abort; tool_content: `LockExhaustedError/OSError/json.JSONDecodeError/ValueError/TypeError/KeyError` → per-session skip-and-continue) and progress-message text entirely inside its `process_batch` callback. **Do not** try to unify the per-item exception handling or the rate/ETA computation (embeddings uses a windowed `deque` rate over message-count `work_done`; tool_content uses a flat `total_updated/elapsed` average) — these reflect real differences (embeddings' per-branch cost varies by message count, tool_content's per-session cost is roughly uniform) and forcing one shape would add a policy-flag abstraction harder to read than the current two straight-line functions. This is a deliberate scope boundary, not an oversight — flag if the comb or reviewer pushes for full unification.
- `format_duration` and the eta-string one-liner (`format_duration(remaining/rate) if rate > 0 else "?"`, identical in both files) can move into `backfill_runner.py` or stay in `backfill_status.py` (`format_duration` already lives there) — executor's call, either is fine as long as it's not duplicated a third time.

Both `run()` functions call `try_acquire_pid_file`/settings/logger setup, the `status` short-circuit, and `os.nice` identically too, but these are 3-4 lines each and don't justify extraction (laziness-protocol: don't force an abstraction for a one-liner).

### transcript_sources.py tree-walk (issue #139)

Two shared primitives replace the current 5 functions' duplicated BFS:

1. **Subagents-dir walker** — BFS `state_dir`, calling a caller-supplied callback for every child directory literally named `"subagents"` (whether or not the walk recurses further), and applying a caller-supplied policy to every *other* child directory: recurse, or take a per-caller fallback action when a safety gate fails (see FR#6's second bullet — this is a real three-way divergence, not just dedup). `_symlinked_project_contains_session_candidate`'s state-tree portion, `_unsafe_subagent_dirs_contain_session_candidate`, and `_candidate_subagent_dirs` become thin callbacks over this: "return True if uuid-glob matches inside this dir, and recurse unconditionally into anything else" (search, no containment gate), "check safety then delegate to the file-search primitive below, else recurse" (search-or-recurse), and "check safety then collect-or-flag, else recurse" (collect-or-recurse) respectively.
2. **Directory file-search** — BFS an arbitrary directory (not gated on the `"subagents"` name) glob-matching `*{uuid}*.jsonl` at every level; this is `_dir_contains_matching_session_transcript` today, reused as the "search inside this subagents dir" building block for primitive 1's search-mode callbacks.

The per-call-site pre-checks that run *before* entering the `state/` BFS — the direct `project_dir / f"{uuid}.jsonl"` check, the direct `project_dir / "subagents"` check, and the `state_dir` existence check — are near-identical text across the three callers but stay in each thin wrapper rather than folding into the primitive; they're guard clauses around the walk, not part of the walk itself, and unifying them would require the primitive to know about the direct-file-check semantics that only one of the three callers has.

Preserve FR#6's two inconsistencies (dedup strategy, and the non-`subagents`-child gate/fallback policy) exactly — make both a parameter of the shared walker rather than picking one behavior and silently changing another caller's. Do not "fix" either as a drive-by; they're out of scope, and for the dedup strategy specifically, the existing test suite doesn't pin which direction would change (it does pin the gate/fallback divergence — see the two tests named in FR#6).

`is_safe_project_dir`, `_is_safe_transcript_file`, `_is_under`, `_resolved_path`, `_dedupe_paths` stay as-is — no duplication there to remove.

## Dependencies and Assumptions

Both refactors accept the existing test suites (2965 combined lines across the three files touched) as the sole regression pin rather than writing new characterization tests first — they're recent, comprehensive (12 test classes for transcript_sources alone), and were confirmed present and passing on this branch before starting. If either executor finds a behavior path the current suite doesn't cover while extracting, add a characterization test for it before changing that path, per `refactoring-discipline.md`.

## Changed Files

- create: `src/ccrecall/hooks/backfill_runner.py` — shared batch-loop driver (FR#1-#3)
- modify: `src/ccrecall/hooks/backfill_embeddings.py` — `run()` delegates to `run_batch_loop`
- modify: `src/ccrecall/hooks/backfill_tool_content.py` — `run()` delegates to `run_batch_loop`
- modify: `src/ccrecall/transcript_sources.py` — 5 tree-walk functions collapse onto 2 shared primitives (FR#4-#6)
- modify (if needed): `tests/test_backfill_embeddings.py`, `tests/test_backfill_tool_content.py`, `tests/test_transcript_sources.py` — only if a gap in current coverage surfaces during extraction; no behavior-change edits
