# Research Brief: Issue 103 - Skip unchanged sessions in `ccrecall status --check-ingestion`

## Findings

- `ccrecall status --check-ingestion` enters through `status.collect_status(..., check_ingestion=True)`, opens the DB read-only with `PRAGMA query_only = ON`, builds an `import_log`-derived source index, then calls `ingestion_status.summarize_ingestion()`.
- `summarize_ingestion()` reparses every tracked existing transcript via `_expected_uuids()` -> `parse_all_with_uuids()` and compares expected active-branch UUIDs with `messages.uuid`. That is the scaling pain: OK sessions are fully parsed again every run.
- Existing import skip precedent is strong but not identical: `hooks/import_conversations.import_session()` first skips by `import_log.file_size` + `file_mtime`, then hashes only on stat mismatch, then delegates to `session_ops.sync_session()` to update `import_log`.
- `import_log` is per file; ingestion status is per session and can combine multiple transcript files (`agent-*.jsonl` plus parent file). Tests already cover multi-file ordering and partial source loss.
- `status.py` currently treats status as read-only and avoids migrating/creating DBs. Any DB-backed "last confirmed OK" marker means `--check-ingestion` either becomes a controlled writer or must degrade when the cache table/columns are absent.
- Schema changes are centralized in `schema.py` and `db.py`; additive changes use `SCHEMA_CORE` plus a self-guarded migration pattern (`_migrate_to_v3`, `_migrate_to_v4`). `tests/test_db.py` pins table/column layout, so schema snapshots must be updated.

## Options and tradeoffs

### Option A - New per-session ingestion check cache table (recommended)

Add a table such as `ingestion_check_cache(session_uuid TEXT PRIMARY KEY, source_fingerprint TEXT NOT NULL, checked_at DATETIME DEFAULT CURRENT_TIMESTAMP)`. `summarize_ingestion()` computes a stable fingerprint from the session's existing source paths and stat metadata, skips full parsing when the stored fingerprint matches, and writes/updates the row only after the session classifies as OK.

Pros:

- Matches the real domain boundary: the expensive check is per session, not per file.
- Handles multi-file sessions cleanly with one fingerprint over the sorted path set.
- Keeps `import_log` focused on import dedup, not audit-cache state.
- Lets failed/problem sessions remain uncached and therefore always rechecked.

Cons:

- Requires a schema addition and migration/test snapshot updates.
- Makes `status --check-ingestion` write cache metadata unless an explicit "no persistence" fallback is chosen.
- Needs careful behavior when the cache table is absent on a read-only connection.

### Option B - Reuse/add columns on `import_log`

Store OK-confirmation stat columns per import-log row and skip when every row in a session has matching OK metadata.

Pros:

- Reuses the table already keyed by `file_path` and already carrying `file_size`/`file_mtime`.
- Fewer new concepts for file freshness.

Cons:

- Awkward for multi-file sessions: OK is a group property, but rows are per file.
- Couples audit-check semantics to import dedup state.
- More likely to produce edge-case ambiguity when one file in a session changes or goes missing.

### Option C - Non-persistent/stat-only shortcut from `import_log`

Trust matching `import_log.file_size`/`file_mtime` as "unchanged enough" and skip without recording that `--check-ingestion` ever confirmed the session.

Pros:

- No schema/write behavior change.
- Easy and aligned with import fast-skip mechanics.

Cons:

- Undercuts the purpose of `--check-ingestion`: historical gaps or prior importer bugs can be hidden forever if import_log stats match.
- Does not satisfy the "previously confirmed OK" requirement.
- Less defensible as an audit mode.

## Recommended approach

Use Option A: a new per-session cache table storing only confirmed-OK freshness. Cache hits should increment `sessions_checked` and `ok_sessions`, but skip `_expected_uuids()` and the `messages.uuid` set query. Cache misses should run the current full logic unchanged.

Only cache OK sessions. Do not cache `pending_tail`, `stale_tail`, `ingestion_gap`, or `missing_source`: those states should remain visible and self-heal naturally after import/sync or file recovery.

## Implementation notes

- Fingerprint should include all existing source paths for a session, sorted deterministically, plus stat metadata. Prefer `st_size` and `st_mtime_ns` if possible; the existing import table uses float `st_mtime`, but new cache code can avoid float equality issues.
- A missing source should bypass cache entirely. Partial source loss is already counted as `missing_source`; do not let an old OK marker mask it.
- Add small helpers in `ingestion_status.py`, e.g. `_source_fingerprint(filepaths)`, `_cached_ok(cursor, session_uuid, fingerprint)`, `_record_ok(cursor, session_uuid, fingerprint)`.
- Decide explicitly how `status.py` opens the DB for `--check-ingestion`. Best fit: keep default status read-only, but for `check_ingestion=True` use a writable connection after the existing outdated-schema guard, or document that the deep check records cache metadata.
- If keeping read-only status is non-negotiable, make persistence opportunistic: read cache only if table exists and suppress writes on `sqlite3.OperationalError`; this is less predictable but preserves the surface contract.
- Schema work likely means `SCHEMA_VERSION = 5`, adding the table to `SCHEMA_CORE`, and adding an additive `_migrate_to_v5()`/always-safe table creation path. Update the schema-equivalence pin in `tests/test_db.py`.

## Tests

- `test_complete_session_cache_hit_skips_parse`: first run records OK; second run with unchanged file counts it OK without calling `parse_all_with_uuids()`.
- `test_cache_invalidates_on_file_append_or_mtime_change`: modify transcript and assert the full parse path runs and pending/stale/gap logic still works.
- `test_problem_sessions_are_not_cached`: create an ingestion gap; repeated runs still parse/reclassify it.
- `test_missing_or_partial_source_bypasses_cache`: existing multi-file partial-loss behavior remains `missing_source` even after a prior OK marker.
- `test_status_check_ingestion_uses_cache_connection_behavior`: pin whether `collect_status(check_ingestion=True)` writes cache metadata or degrades read-only.
- Schema tests: fresh DB contains the new cache table; migration from older DB is reentrant; schema snapshot updated.

## Risks / open questions

- Main product decision: may `ccrecall status --check-ingestion` mutate the DB? The optimization is cleanest if yes, but that conflicts with current `status.py` read-only design.
- Cache fingerprint granularity: `mtime_ns` is safer than float mtime, but cross-platform filesystem precision should be considered.
- Existing DBs without the new schema need a graceful path: require `ccrecall import`/migration first, or let check run uncached until the cache table exists.
- There is no need for new dependencies.

## Suggested next step

Write a small design/implementation plan choosing the DB-write behavior for `--check-ingestion`, then implement the new per-session cache table and regression tests around skip, invalidation, and problem-session non-caching.
