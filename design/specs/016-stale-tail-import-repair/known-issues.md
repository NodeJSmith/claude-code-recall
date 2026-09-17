# Known Issues

Durable issues discovered during orchestration that were intentionally not fixed in this run.

## KI-001: No dedicated test for repair_sessions()'s project_id-is-None defensive branch

Status: resolved — stale; test_project_id_none_candidate_is_unrepairable_not_failed (added in f54a58e) already covers this branch, asserting sessions_unrepairable rather than sessions_failed as originally described below
Run: 136
Source: impl-review
Reason not fixed now: out-of-scope
Observed in: T02 (commit 8a611e1)
Affected files:
- src/ccrecall/hooks/import_repair.py

Issue:
`repair_sessions()` (lines ~79-90) has a defensive branch that treats a candidate with a `None`
`project_id` as an immediate `sessions_failed` outcome, skipping the file loop and
`reclassify_session` entirely. This branch has no dedicated test exercising it — it's covered by
code inspection and the spec reviewer's schema verification (`sessions.project_id` is nullable),
but no test constructs a candidate with `project_id=None` and asserts the failure-counting
behavior.

Why deferred:
This is a rare edge case (a session row whose `project_id` column is NULL) not directly required
by any FR/AC in the design, and the implementation review classified it as a minor WARN, not a
blocking gap. Adding dedicated coverage is a small, low-risk addition better done as routine
test-hardening rather than expanding this orchestration run's scope.

Recommended follow-up:
Add a test to `tests/test_import_repair.py` that constructs a candidate tuple with
`project_id=None` and asserts `repair_sessions()` counts it under `sessions_failed`, logs the
condition, and does not call `reclassify_session` for it.

Acceptance criteria:
- A new test in `tests/test_import_repair.py` covers the `project_id is None` branch and passes.

## KI-002: find_repairable_sessions() snapshots classification once, with no per-candidate re-validation before force-reimport

Status: open
Run: 136
Source: challenge
Reason not fixed now: out-of-scope
Observed in: challenge findings (Finding 8)
Affected files:
- src/ccrecall/ingestion_status.py

Issue:
`find_repairable_sessions()` computes every candidate's classification against a single
`Instant.now()` snapshot and returns the whole list up front. `repair_sessions()` then iterates
that list and force-reimports each candidate without re-checking whether it's still classified as
`stale_tail`/`ingestion_gap` immediately before its (comparatively expensive) force-reimport runs.
Between the snapshot and a given candidate's turn in the loop — which can be arbitrarily far apart
in a large batch — the on-disk transcript could have changed underneath the classification (e.g. a
session that was `stale_tail` at snapshot time gets written to again and would now classify as
`pending_tail`, or a session's source file is deleted mid-run).

Why deferred:
Low severity — force-reimport is idempotent (`sync_session`'s UUID-dedup insert is a safe no-op on
already-fixed content) and `reclassify_session` already re-checks the outcome after the fact, so a
stale snapshot cannot corrupt data or misreport a repair as successful when it wasn't. Adding a
per-candidate re-check before each force-reimport would mean re-parsing every candidate's
transcript(s) a second time (once for classification, once for the actual reimport), doubling parse
cost for a case that self-corrects via the post-repair reclassification anyway. At this tool's
manual/personal-scale invocation pattern (not a frequently-scheduled automated job), the extra
parse cost isn't worth it for what is otherwise an unconsidered-but-low-risk race.

Recommended follow-up:
If `--repair-gaps` ever moves to automated/repeated invocation (see the design's Non-Goals section
on "no cross-invocation persistence of known unrepairable sessions" — a related latency concern),
revisit whether a lightweight per-candidate freshness check (e.g. a stat comparison against the
`sources` snapshot, cheaper than a full re-parse) is worth adding before each force-reimport.

Acceptance criteria:
- None — this is a documented, accepted risk, not a tracked fix.

## KI-003: `cmd_import`/`cmd_status`'s `db` parameter doesn't reuse the shared `_DB` type alias

Status: resolved — fixed during known issues walkthrough
Run: 136
Source: clean-code
Reason not fixed now: out-of-scope
Observed in: pre-existing (unchanged by this branch's diff against 387156d)
Affected files:
- src/ccrecall/cli/commands.py

Issue:
`cmd_import` (line 128) and `cmd_status` (line 147) each redefine
`Annotated[Path, Parameter(help="Database path.")]` inline for their `db` parameter instead of
reusing the `_DB` type alias already defined at module scope for exactly this purpose. Every other
`db` parameter in the file (`cmd_backfill_summaries`, `cmd_backfill_tool_content`, `cmd_recent`,
`cmd_search`, `cmd_search_messages`) uses `_DB`.

Why deferred:
Confirmed via `git diff 387156d...HEAD -- src/ccrecall/cli/commands.py`: these two lines are
unchanged context in this diff (only `cmd_import` gained a new `repair_gaps` parameter and its
`import_mod.run(...)` call site changed). The duplication predates this branch and touching it
would expand the diff beyond this feature's scope.

Recommended follow-up:
Replace both inline `Annotated[Path, Parameter(help="Database path.")]` occurrences with `_DB`.

Acceptance criteria:
- `cmd_import` and `cmd_status` both use `_DB` for their `db` parameter; no behavior change.

## KI-004: Duplicated `session`/`project`/`path` filter help strings across `cmd_recent`/`cmd_search`/`cmd_search_messages`

Status: resolved — fixed during known issues walkthrough
Run: 136
Source: clean-code
Reason not fixed now: out-of-scope
Observed in: pre-existing (unchanged by this branch's diff against 387156d)
Affected files:
- src/ccrecall/cli/commands.py

Issue:
The help strings `"Filter by session UUID (prefix match)."`, `"Filter by project name(s),
comma-separated."`, and `"Filter by cwd substring (e.g. worktree name)."` are duplicated verbatim
across `cmd_recent` (~356-358), `cmd_search` (~403-405), and `cmd_search_messages` (~458-460). The
file already solves this drift risk for `_BEFORE`/`_AFTER`/`_VERBOSE`/`_NOTIFS`/`_DB` via shared
module-scope `Annotated` aliases, but not for these three.

Why deferred:
None of these lines fall inside a diff hunk on this branch (confirmed via
`git diff 387156d...HEAD -U0 -- src/ccrecall/cli/commands.py`) and this feature's task scope never
touched `cmd_recent`/`cmd_search`/`cmd_search_messages`.

Recommended follow-up:
Introduce `_SESSION`, `_PROJECT`, `_PATH` shared `Annotated` aliases (matching the existing
`_BEFORE`/`_AFTER` pattern) and use them in all three commands.

Acceptance criteria:
- `cmd_recent`, `cmd_search`, `cmd_search_messages` all reference shared aliases for these three
  filters instead of duplicating the `Annotated`/help text inline.

## KI-005: `ingestion_status.py` cache-fingerprint helpers take an untyped `cursor` parameter

Status: resolved — fixed during known issues walkthrough
Run: 136
Source: clean-code
Reason not fixed now: out-of-scope
Observed in: pre-existing (unchanged by this branch's diff against 387156d)
Affected files:
- src/ccrecall/ingestion_status.py

Issue:
`_db_coverage_fingerprint`, `_cached_ok_fingerprint`, and `_record_ok_fingerprint` all take a bare
`cursor` parameter with no type annotation, while the rest of the module (`classify_sessions`,
`find_repairable_sessions`, `reclassify_session`) annotates its `Connection`/typed parameters.

Why deferred:
Confirmed via `git diff 387156d...HEAD -U0 -- src/ccrecall/ingestion_status.py`: none of these
three `def` signature lines are touched by this diff — only the body of
`_db_coverage_fingerprint` and the newly added `classify_sessions`/`summarize_ingestion` code
around them changed. Annotating pre-existing signatures is unrelated to this feature's scope.

Recommended follow-up:
Add `cursor: sqlite3.Cursor` annotations to all three functions for consistency with the rest of
the module.

Acceptance criteria:
- All three functions annotate their `cursor` parameter; no behavior change.

## KI-006: `import_project()` exceeds the 50-line function guideline

Status: open
Run: 136
Source: clean-code
Reason not fixed now: out-of-scope
Observed in: pre-existing (unchanged by this branch's diff against 387156d)
Affected files:
- src/ccrecall/hooks/import_conversations.py

Issue:
`import_project()` (~110 lines) combines project upsert/exclusion logic, session-file grouping,
and the per-file SAVEPOINT/exception-handling loop in one function body, well past the file's own
50-line guideline.

Why deferred:
Confirmed via `git diff 387156d...HEAD -- src/ccrecall/hooks/import_conversations.py`: this
diff's hunks are anchored in `import_session`, `run`, and `_run` — `import_project` itself is not
touched at all (only shifted in line number by insertions above it). Decomposing it is unrelated
to this feature and would expand the diff into an unrelated pre-existing function.

Recommended follow-up:
Decompose `import_project()` into smaller helpers (e.g. separate the project upsert/exclusion
check, the file-grouping step, and the per-file import loop) as a standalone refactor with its own
pinned-behavior tests.

Acceptance criteria:
- `import_project()` is decomposed with no behavior change, verified by the existing test suite.

## KI-007: New repair/ingestion functions exceed the 50-line guideline

Status: filed (#210)
Run: 136
Source: clean-code
Reason not fixed now: behavior-change
Observed in: this branch (commit f54a58e and earlier on this branch)
Affected files:
- src/ccrecall/hooks/import_repair.py
- src/ccrecall/hooks/import_conversations.py
- src/ccrecall/session_ops.py
- src/ccrecall/ingestion_status.py

Issue:
Several functions newly added or substantially grown by this feature exceed the 50-line
guideline: `import_repair.py:repair_sessions()` (~165 lines, combining per-candidate SAVEPOINT
acquisition, two distinct exception-handling branches, before/after message counting,
reclassification, and progress logging inline), `import_conversations.py:_run()` (~125 lines,
mixing PID-guard acquisition, settings/DB setup, import branching, timing instrumentation, and
the `repair_gaps` post-pass), `session_ops.py:_sync_branches_and_messages()` (~95 lines, doing
project resolution, message dedup, tool-content repair, message insertion, and per-branch sync),
and `ingestion_status.py:classify_sessions()`/`summarize_ingestion()` (~59/~66 lines each).

Why deferred:
All four are in scope of this diff (new or substantially modified), but decomposing them —
especially `repair_sessions()`, whose nested try/except/SAVEPOINT structure implements a
carefully-reasoned durability model documented in the module's own docstring — is a structural
refactor that risks subtly changing exception-handling or transaction-boundary behavior. That is
exactly the kind of judgment-call change this orchestration run's clean-code pass is scoped to
leave alone rather than risk under time pressure; per `rules/common/refactoring-discipline.md`, a
change like this needs its own pinned-behavior characterization pass before restructuring, not a
same-pass mechanical fix.

Recommended follow-up:
Decompose each function in a dedicated follow-up refactor: pin current behavior with
characterization tests first, then extract per-candidate/per-branch helpers (e.g.
`_repair_one_candidate` for `repair_sessions()`) while keeping the existing test suite green
throughout.

Acceptance criteria:
- Each of the four functions is decomposed below ~50 lines with no test regressions and no
  observable behavior change.

## KI-008: `_noop()` is duplicated between `import_conversations.py` and `import_repair.py`

Status: filed (#211)
Run: 136
Source: clean-code
Reason not fixed now: needs-decision
Observed in: this branch (commit f54a58e and earlier on this branch)
Affected files:
- src/ccrecall/hooks/import_conversations.py
- src/ccrecall/hooks/import_repair.py

Issue:
`_noop() -> None: pass` is defined identically in both modules as the default value for an
`on_reclaim: Callable[[], None]` parameter.

Why deferred:
Attempted during this run: importing `_noop` from `import_conversations` into `import_repair`
raises `ImportError: cannot import name '_noop' from partially initialized module
'ccrecall.hooks.import_conversations' (most likely due to a circular import)` — confirmed via
`uv run pytest -q`, since `import_conversations.py` already imports `import_repair` at module
load time (`from ccrecall.hooks import import_repair`). De-duplicating requires an architectural
decision about where the shared helper should live (e.g. a small shared utility module both can
import without a cycle), which is out of scope for a mechanical clean-code fix.

Recommended follow-up:
Move `_noop` to a module neither `import_conversations.py` nor `import_repair.py` needs to import
from the other to reach (e.g. a small `hooks/_util.py`, or inline a `lambda: None` default at each
call site instead of a shared named function), then have both modules import/use it from there.

Acceptance criteria:
- `_noop`'s definition exists in exactly one place; both modules use it with no circular import
  and the full test suite still passes.
