# ccrecall Audit Attack Plan

Sequenced for maximum early impact with minimal cross-issue interference. Each wave can land as its own PR or set of PRs.

## Wave 1: Quick wins (low risk, immediate value)

**#143 — Dead code cleanup**
Remove `db_base.get_connection`, `fusion.rrf()`, and the dead `summary_source_hash` NULL assignments. No behavior change, no migration needed. The dead schema columns stay until the next `branches` table rebuild — just stop writing to them.

**#145 — Logging gaps (priority targets only)**
Add `log = logging.getLogger(LOGGER_NAME)` to the worst dark-operation modules first: `search_vector.py` (5 silent error swallows), `embeddings.py` (silent load failures), `session_tail.py` (silent parse skips). Remaining modules can follow incrementally. This is pure addition — no behavior changes, no refactoring.

**#144 — Test infrastructure duplication**
Fold `FIXTURE_DIR` and `_make_conn` into conftest. Mechanical, zero-risk cleanup that makes the test suite easier to maintain for everything that follows.

## Wave 2: Safety net (tests before refactoring)

**#139 — transcript_sources.py tests**
Write dedicated unit tests for the path-safety functions. This is security-relevant code with zero coverage — it needs tests *before* anyone touches it (and #140 touches adjacent worktree logic). Don't refactor the tree-walk duplication yet; just pin current behavior.

**#142 — message_ops.py tests + CLI smoke tests**
Pin `message_ops.py` dedup behavior with unit tests. Add at minimum smoke tests for the 9 untested CLI commands. These pins protect against regressions in waves 3-4.

## Wave 3: Structural (the big decompositions)

**#137 — Split db.py**
Extract connection management from vec/embedding DDL so `get_connection()` can be imported without pulling numpy. This is the highest-impact structural change — it fixes the hot-path import violation and lowers db.py's churn rate by narrowing its responsibility. Do this before other refactors because many modules import db.py, so the split affects import paths project-wide.

**#140 — Split session_tail.py**
Decompose into path resolution, pending-question detection, and tail rendering. Depends on the transcript_sources tests from wave 2 (adjacent concern, shared worktree logic). Smaller blast radius than #137.

**#138 — Decompose monolith functions**
Extract named steps from `embed_branch_chunks`, and extract shared batch-loop/progress infrastructure from the two backfill `run()` functions. Depends on wave 2 tests pinning behavior. The backfill shared infrastructure may also benefit from the db.py split in #137.

## Wave 4: Polish

**#141 — Consolidate duplicated patterns**
Introduce `ScopeFilter` dataclass, shared `has_tool_counts` helper, shared memory-reclaim helper. Easier to do after #137 (db.py split) since some of the duplication crosses the db/search boundary.

**#146 — CLI flag consistency**
Standardize `-n` naming, clarify `--verbose` vs `--debug`, add missing `--db` flags. Do last because waves 1-3 may change the CLI surface. This is also the most opinionated change — flag renames are breaking for existing users/scripts.

**#145 — Logging (remaining modules)**
Finish rolling out logging to the remaining ~20 modules. Lower priority since the dark-operation modules were handled in wave 1.

## Dependencies

```
Wave 1: #143, #145-partial, #144  (independent, can parallelize)
         |
Wave 2: #139, #142                (independent, can parallelize)
         |
Wave 3: #137 → #140 → #138       (sequential — db split first, then decompositions)
         |
Wave 4: #141, #146, #145-rest     (independent, can parallelize)
```

Waves 1 and 2 can overlap if different people/sessions work on them. Wave 3 is sequential because each decomposition changes import paths that the next one touches. Wave 4 is cleanup that benefits from everything above being stable.
