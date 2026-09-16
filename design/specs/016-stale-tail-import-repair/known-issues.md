# Known Issues

Durable issues discovered during orchestration that were intentionally not fixed in this run.

## KI-001: No dedicated test for repair_sessions()'s project_id-is-None defensive branch

Status: open
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
