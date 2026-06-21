---
task_id: "T02"
title: "Split sync_session into per-responsibility helpers"
status: "planned"
depends_on: ["T01"]
implements: ["FR#1", "FR#2", "FR#3", "FR#4", "AC#2", "AC#3", "AC#4"]
---

## Summary
Decompose `sync_session` (~339L, lines 49–387 — the whole module is essentially this one function) into a thin orchestrator over module-level helpers, one per responsibility its own docstring names: import_log dedup, session upsert, message insertion with UUID dedup, per-branch sync (itself delegating branch upsert, branch_messages diff, summary write, embed-on-write), and import_log write. All helpers thread an explicit `cursor`/`conn`. Behavior-preserving — same return value and same rows written; the T01 pin and the existing `sync_session` suites stay green.

## Target Files
- modify: `src/ccrecall/session_ops.py`
- read: `design/specs/002-db-write-path-refactor/design.md`
- read: `design/specs/002-db-write-path-refactor/tasks/context.md`
- read: `tests/test_session_ops.py`
- read: `tests/test_import_pipeline.py`
- read: `tests/test_sync_hook.py`

## Prompt
Read `tasks/context.md` and `design/specs/002-db-write-path-refactor/design.md` (`## Architecture`, `## Edge Cases`, `## Key Constraints`).

Refactor `src/ccrecall/session_ops.py`, extracting these module-level helpers (all public, no `_` prefix; all taking explicit `cursor`/`conn` + data):
- `import_log_skip_check(cursor, filepath, write_import_log, file_hash) -> tuple[row, bool]` — the dedup probe; returns the existing log row and whether to short-circuit to `-1` (exact non-NULL hash match). Preserve the NULL-hash-stale asymmetry.
- `upsert_session(cursor, session_uuid, project_id, meta) -> int` — the `sessions` INSERT…ON CONFLICT + id fetch.
- `insert_new_messages(cursor, session_id, messages, valid_branch_uuids, existing_uuids) -> int` — the message-insert loop with UUID dedup, notification flagging, tool-result skip, empty-text skip; returns `new_count` (keep its mutation of `existing_uuids`, or return the updated set — keep semantics identical).
- `sync_branch(conn, cursor, branch, messages, uuid_to_msg_id, existing_branches, session_id, vec_writable)` — the per-branch loop body (lines 210–366). It further delegates:
  - `upsert_branch(cursor, branch, branch_meta, exchange_count, files_json, commits_json, tool_counts_json, session_id, existing_branches) -> int` — the INSERT-vs-UPDATE + the single-active-branch enforcement → `branch_db_id`. Keep the `assert branch_db_id is not None  # noqa: S101` line (currently session_ops.py:279, which sits **after** the INSERT-vs-UPDATE if/else and covers **both** paths) at the end of `upsert_branch`'s body, after the if/else, immediately before `return branch_db_id`. Do not move it onto the INSERT-only path; do not drop it.
  - `diff_branch_messages(cursor, branch_db_id, branch_uuids, uuid_to_msg_id)` — the add/remove link diff.
  - `write_branch_summary(cursor, branch_db_id) -> str | None` — `compute_context_summary` + the **3-way exception classification** (content `(ValueError, TypeError, KeyError)` → skip; `sqlite3.Error` → log+skip) moved wholesale, never reshaped. Returns `summary_md` or None.
  - `embed_branch(cursor, branch_db_id, summary_md, is_active, vec_writable)` — the guarded embed-on-write with `contextlib.suppress(Exception)`; **preserve the ordering invariant** (vec0 upsert FIRST via `write_branch_embedding`, version columns LAST, only for active leaves with a successful summary and `vec_writable`).
- `write_import_log(cursor, filepath, session_id, file_hash, log_row)` — the final UPDATE-or-INSERT.
- `sync_session` becomes orchestration: skip check (early `-1`), parse + branch/message early-returns (`0`), project upsert (keep the `_project_id` pre-resolution branch), `upsert_session`, build `valid_branch_uuids` + `existing_uuids`, `insert_new_messages`, build `uuid_to_msg_id`, fetch `existing_branches`, **probe `vec_writable = branch_vec_queryable(conn)` ONCE** before the loop, loop `sync_branch` over branches, `write_import_log`, `return new_count`.

`sync_session` must NOT introduce a `commit()` — callers own the transaction. Keep the cross-helper shared reads (`valid_branch_uuids`, `existing_uuids`, `uuid_to_msg_id`, `vec_writable`, `existing_branches`) computed in the orchestrator and passed down as args.

Run `uv run pytest -q` — full suite green, including `tests/test_session_ops.py` (incl. the T01 pin), `tests/test_import_pipeline.py`, `tests/test_sync_hook.py`, all unchanged.

## Focus
- **`sync_session` never commits** — `sync_current.py:119` and `import_conversations.py:71` own the transaction boundary. Moving a commit into any helper is a behavior change.
- **Hoist the vec probe once.** `vec_writable = branch_vec_queryable(conn)` (session_ops.py:208) is deliberately computed before the branch loop to avoid paying `embed_text` inference per inactive leaf; keep it once-per-call, passed into `sync_branch`/`embed_branch`.
- **Embed-on-write order is load-bearing** (design `## Edge Cases`): version columns must stay at 0 if the vec upsert is swallowed, so the branch stays eligible for backfill — vec0 upsert FIRST, version columns LAST.
- **The three `except` boundaries** in the summary write (session_ops.py:338–350) are intentionally distinct (content skip vs infra log+skip; plus the separate embed broad-suppress at 363). Move each into the helper owning its guarded op; do not merge or widen them.
- `sync_session` is imported by `hooks/sync_current.py:119` and `hooks/import_conversations.py:71` — signature unchanged (incl. the `_project_id` adapter param).
- Behavior-preserving structure only. No smuggled changes.

## Verify
- [ ] FR#1: `sync_session` returns the same integer (new-count / `0` / `-1`) and writes the same rows to `sessions`/`messages`/`branches`/`branch_messages`/`import_log` as before (T01 pin + existing suites green).
- [ ] FR#2: embed-on-write ordering preserved — vec0 upsert before version-column write, only for active leaves with a successful summary and queryable vec table.
- [ ] FR#3: every function in `session_ops.py` is ≤50 lines except `sync_session` (pure orchestrator) and `sync_branch` if it remains a per-branch orchestrator over named helpers.
- [ ] FR#4: `sync_session` remains importable from `ccrecall.session_ops` with unchanged signature (incl. `_project_id`).
- [ ] AC#2: a scan confirms no function in `session_ops.py` exceeds the guideline except the documented orchestrator(s).
- [ ] AC#3: `uv run pytest -q` passes with zero failures; existing `sync_session` suites pass unchanged.
- [ ] AC#4: importing `sync_session` from `ccrecall.session_ops` succeeds with unchanged signature.
