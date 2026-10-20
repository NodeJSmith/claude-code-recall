# Known Issues

Real issues found while building this feature and intentionally left unfixed.

## KI-001: A project that keeps failing is fully retried and logged on every import

Status: open
Recorded: 2026-10-20 (221)
Source: ship-challenge
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/hooks/import_conversations.py

Issue:
`import_project_contained` rolls back a failed project as a whole, including its `import_log` rows, so the project is retried on every import. Each failure adds a `log.exception` traceback to `ccrecall-import.log`, which is 1 MB with 2 backups. If the failure fires late in the file loop, outside the per-file SAVEPOINT (for example in `has_pending_tool_content`, `sort_session_files`, or a failed commit), the files that already imported are rolled back and re-imported on every run.

Why deferred:
This is rare. Most project-level failures fire in `upsert_project`, before any file imports. Committing per file would break the "transaction spans the project" invariant that D3's rollback depends on.

Recommended follow-up:
Handle it with #223's failure surfacing. Options are bounded retry, a failure sidecar read by `health.py`, or quarantining a project after N consecutive failures.

Acceptance criteria:
- A project that fails on every run neither re-imports its earlier files on every run nor adds an unbounded traceback per run.

## KI-002: The `ON CONFLICT(key) DO NOTHING` race branch is untested, and the import path aborts instead of reaching it

Status: open
Recorded: 2026-10-20 (221)
Source: ship-challenge
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/project_ops.py
- src/ccrecall/hooks/import_conversations.py

Issue:
Only the sync path can reach the "losing racer's insert is dropped, the key's next write heals it" branch in `upsert_project`. On the import path, the deferred `BEGIN` takes a WAL read snapshot at the SELECT. If another connection commits before the INSERT, the INSERT raises `SQLITE_BUSY` (an `OperationalError`), and D3 propagates it, aborting that import run. No test exercises either outcome. The abort predates this change: the old `ON CONFLICT(path)` insert ran under the same transaction shape.

Why deferred:
No production behavior is wrong. The heal claim holds on the one path that reaches the branch, and an aborted import retries on its next run.

Recommended follow-up:
If import runs are seen aborting on this race, open the import's transaction with `BEGIN IMMEDIATE`, or retry the project once on `SQLITE_BUSY`. Add a test that interleaves a competing insert.

Acceptance criteria:
- A test drives the DO NOTHING branch with a row a racer inserted first.
- A sync that races an import's project insert no longer aborts the import run.
