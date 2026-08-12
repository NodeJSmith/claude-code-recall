---
task_id: "T02"
title: "Add atomic recap schema and branch order"
status: "done"
depends_on: []
implements: ["FR#20", "FR#21", "AC#13", "AC#14"]
---

## Summary

Create the complete claim-capable recap schema in one atomic version migration and persist deterministic root-to-leaf branch-message order. Add schema capability checks and a migration-free bounded hook connection. Update all direct branch-message fixtures and ordering assumptions.

## Target Files

- modify: `src/ccrecall/llm_summary_db.py`
- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/parsing.py`
- modify: `src/ccrecall/branch_ops.py`
- read: `src/ccrecall/schema.py`
- modify: `tests/test_db.py`
- modify: `tests/test_parsing.py`
- modify: `tests/test_import_pipeline.py`
- modify: `tests/test_session_ops.py`
- modify: `tests/test_integration.py`
- modify: `tests/test_summarizer.py`
- modify: `tests/test_backfill_embeddings.py`
- modify: `tests/test_backfill_tool_content.py`
- modify: `tests/test_recent_chats.py`
- modify: `tests/test_sync_hook.py`
- modify: `tests/test_status.py`
- modify: `tests/test_context_alerts.py`

## Prompt

Implement the design's "Schema and retention" and "Migration" foundations. Add `branch_messages.position`, current/materialized recap input hash/version columns, jobs, attempts, runtime, provider health, runs, run candidates, quarantine/process identity fields, checks, foreign keys, and named indexes in one new `BEGIN IMMEDIATE` version migration. Keep every recap object out of `SCHEMA_CORE` and the pre-transaction additive block; advance `user_version` only after postconditions pass. Add reusable schema capability introspection and a no-migrate short-timeout connection for SessionEnd. Change branch discovery to preserve an ordered root-to-leaf UUID list and make `diff_branch_messages()` insert/update retained positions. Backfill old positions deterministically by timestamp and message row ID.

## Focus

`find_all_branches()` currently exposes only a set, and `diff_branch_messages()` converts desired links to a set. Preserve membership semantics while adding ordered path data. `fetch_branch_messages()` remains timestamp-ordered for existing consumers unless recap code explicitly requests `bm.position`. Many tests directly insert two-column `branch_messages` rows or compare their shape; update every Target File fixture without changing unrelated behavior. `llm_summary_db._open_connection()` currently executes and commits `SCHEMA_CORE` before migrations, so recap DDL must be exclusively versioned.

## Verify

- [ ] FR#20: Schema capability helpers distinguish complete, missing/outdated, and partial recap schemas without migration.
- [ ] FR#21: Interruption/concurrency tests expose either old schema or the complete recap schema, never a usable partial claim schema.
- [ ] AC#13: Read-only capability tests perform no writes and avoid recap SQL when objects are unavailable.
- [ ] AC#14: Fresh and upgraded databases have matching recap objects, constraints, indexes, and deterministic positions.
