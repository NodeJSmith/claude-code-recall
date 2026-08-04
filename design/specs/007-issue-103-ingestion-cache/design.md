# Design: Issue 103 Ingestion Check Cache

**Date:** 2026-08-04
**Status:** archived
**Mode:** sketch

## Problem

`ccrecall status --check-ingestion` reparses every tracked transcript on every run, even when a session was already confirmed OK and all source transcript files are unchanged. The check gets slower as history grows because `ingestion_status.summarize_ingestion()` always calls `_expected_uuids()` and `parse_all_with_uuids()` for every existing source group.

## Goals

- Make repeated `ccrecall status --check-ingestion` runs skip sessions that were previously confirmed OK and whose transcript source set is unchanged.
- Preserve audit correctness by fully rechecking first-time, modified, missing-source, and problem sessions.
- Keep the cache state local to the conversations DB and aligned with existing schema/migration patterns.

## Non-Goals

- Do not change the ingestion classification semantics for `pending_tail`, `stale_tail`, `ingestion_gap`, or `missing_source`.
- Do not use import-log stat equality alone as proof that a session is OK.
- Do not introduce new dependencies or embedding/vector behavior.

## Functional Requirements

- **FR#1** The ingestion check must persist a per-session confirmed-OK freshness marker after a full check finds no missing expected UUIDs.
- **FR#2** The ingestion check must skip transcript parsing for a session when its stored confirmed-OK marker matches the current source fingerprint.
- **FR#3** The source fingerprint must cover the complete existing source file set for a session, including deterministic path order and file stat metadata.
- **FR#4** The ingestion check must not use or write an OK cache marker for sessions with missing source files or missing expected UUIDs.
- **FR#5** `ccrecall status --check-ingestion` must use a connection mode that can read and update the cache while preserving the existing non-creating behavior for missing databases and the outdated-schema early return.

## Acceptance Criteria

- **AC#1** `uv run pytest tests/test_ingestion_status.py` passes with new tests proving first-run OK recording and second-run cache-hit parsing skip behavior.
- **AC#2** `uv run pytest tests/test_ingestion_status.py` passes with tests proving file append/stat changes invalidate the cache and problem sessions are reparsed rather than cached.
- **AC#3** `uv run pytest tests/test_status.py` passes with a test pinning that `collect_status(check_ingestion=True)` can persist ingestion-cache metadata without creating a missing DB or bypassing the outdated-schema guard.
- **AC#4** `uv run pytest tests/test_db.py` passes with schema-version, fresh-schema, and snapshot expectations updated for the new cache table.
- **AC#5** `uv run pytest tests/test_ingestion_status.py tests/test_status.py tests/test_db.py` passes locally.

## Approach

Add a dedicated per-session cache table rather than extending `import_log`. `import_log` is keyed by `file_path`, while `summarize_ingestion()` works at a session boundary and can combine parent and `agent-*.jsonl` files. A session-level row avoids group-property ambiguity and keeps import dedup state separate from audit-check state.

The schema change should follow the existing additive migration pattern in `src/ccrecall/schema.py` and `src/ccrecall/db.py`: add an `ingestion_check_cache` table to `SCHEMA_CORE`, bump `SCHEMA_VERSION` from `4` to `5`, and add a small `_migrate_to_v5()` that creates the table idempotently. Wire it from `_apply_migrations()` alongside the existing self-guarded additive migrations. Update `tests/test_db.py` table and column snapshots.

Use a table shape like:

```sql
CREATE TABLE IF NOT EXISTS ingestion_check_cache (
  session_uuid TEXT PRIMARY KEY,
  source_fingerprint TEXT NOT NULL,
  checked_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

In `src/ccrecall/ingestion_status.py`, add helpers for source fingerprinting, cache lookup, and recording. The fingerprint should be deterministic across runs and include every existing source path sorted by string path plus each file's `st_size` and `st_mtime_ns`. Missing sources must bypass the cache entirely so an old OK marker never hides partial source loss. Only call `_record_ok()` after the current full logic reaches the existing `ok_sessions` path.

For `src/ccrecall/status.py`, keep `_readonly_connection()` for default status and embedding reads, but let the `check_ingestion=True` path open the DB through `get_connection(settings, load_vec=False)` after the current outdated-schema guard. This preserves the current no-create behavior by checking `db_path.exists()` before opening, and it preserves the outdated-schema early return by probing with `_readonly_connection()` first. The check can then deliberately write cache metadata as part of the expensive audit mode.

## Reference Artifacts

- `design/research/2026-08-04-issue-103-ingestion-cache/research.md` - saved prior research brief used as discovery input.

## Changed Files

- modify: `src/ccrecall/schema.py` - add the ingestion check cache table to the baseline schema.
- modify: `src/ccrecall/db.py` - bump schema version and add the idempotent v5 table migration.
- modify: `src/ccrecall/status.py` - use a writable, migration-aware connection only for `check_ingestion=True` after preserving missing/outdated DB behavior.
- modify: `src/ccrecall/ingestion_status.py` - add fingerprint, cache hit, and cache record behavior around the existing per-session classification loop.
- modify: `tests/test_ingestion_status.py` - add cache hit, invalidation, and no-cache-for-problem-session coverage.
- modify: `tests/test_status.py` - pin `collect_status(check_ingestion=True)` cache-writing connection behavior.
- modify: `tests/test_db.py` - update schema table/column snapshot and migration coverage for v5.
