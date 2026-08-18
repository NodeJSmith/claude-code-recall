# Context: Dedupe backfill run() scaffolding and transcript tree-walk logic

## Problem & Motivation

Two unrelated areas have accumulated structural duplication: `backfill_embeddings.run()` (274 lines) and `backfill_tool_content.run()` (206 lines) each hand-roll a near-identical batch-loop body (~131 lines and ~108 lines respectively) around otherwise-different per-item logic, and `transcript_sources.py` has 5 tree-walk functions that each re-implement directory BFS + symlink-safety bookkeeping. Both refactors are behavior-preserving — the goal is reduced reader load, not new functionality.

## Key Decisions

1. Extract only the genuinely-identical batch-loop scaffolding for the backfill refactor (outer while-loop, `--limit` cutoff, stuck-batch *detection*, batch trailer dispatch) into a new `src/ccrecall/hooks/backfill_runner.py`. Do NOT unify per-item exception handling, no-progress *recovery action* (abort vs exclude-and-continue), or rate/ETA computation — these differ for real domain reasons and forcing one shape adds more indirection than it removes.
2. Extract two shared BFS primitives for the transcript_sources refactor: a "subagents-dir walker" (callback per `subagents`-named directory found while walking `state/`) and a "directory file-search" (glob-match `*{uuid}*.jsonl` at every level of an arbitrary directory). Three of the five duplicated functions become thin callbacks over the first primitive; one becomes the second primitive itself.
3. Preserve the existing visited-path dedup inconsistency in transcript_sources.py exactly: `_symlinked_project_contains_session_candidate` dedupes by *resolved* path, `_unsafe_subagent_dirs_contain_session_candidate`/`_candidate_subagent_dirs` dedupe by *raw* path. Make this a parameter of the shared walker, not a silent unification.

## Constraints

- Both refactors are behavior-preserving. No new functionality, no new CLI flags, no new config.
- Do not change the public signatures of `discover_session_transcript_files`, `discover_project_transcript_files`, `discover_importable_transcript_files`, or `is_safe_project_dir` — 3 downstream callers depend on them exactly as-is (`src/ccrecall/hooks/import_conversations.py`, `src/ccrecall/hooks/sync_current.py`, `src/ccrecall/project_ops.py`).
- Do not change `backfill_embeddings.run()`'s or `backfill_tool_content.run()`'s CLI-visible behavior: exit codes, stdout/stderr message text and format (both plain-text and `--json`), or the OOM-prevention / savepoint / watermark invariants described in each module's docstring.
- Do not "fix" the visited-path dedup inconsistency noted above — it's out of scope for this refactor.
- Use the existing test suites (`tests/test_backfill_embeddings.py`, `tests/test_backfill_tool_content.py`, `tests/test_transcript_sources.py`) as the regression pin. Add a characterization test first if you find a behavior path they don't cover, per `refactoring-discipline.md` — don't just proceed without one.
- No `from __future__ import annotations`, no lazy imports, no `Optional[X]` (use `X | None`) — see repo CLAUDE.md conventions.
