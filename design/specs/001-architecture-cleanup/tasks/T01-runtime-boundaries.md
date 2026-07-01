---
task_id: "T01"
title: "Split runtime boundaries out of db.py"
status: "planned"
depends_on: []
implements: ["FR#1", "FR#2", "FR#10", "AC#1", "AC#9"]
---

## Summary

Create the narrow runtime modules that make later semantic optionality and jobs work. Move path constants, settings/config, database connection setup, runtime file helpers, and logging out of `db.py` without changing behavior. Keep hooks and non-semantic commands working while avoiding new semantic imports on base paths.

## Target Files

- create: `src/ccrecall/paths.py`
- create: `src/ccrecall/settings.py`
- create: `src/ccrecall/database.py`
- create: `src/ccrecall/runtime_files.py`
- create: `src/ccrecall/logging_config.py`
- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/health.py`
- modify: `src/ccrecall/recent_chats.py`
- modify: `src/ccrecall/session_tail.py`
- modify: `src/ccrecall/legacy.py`
- modify: `src/ccrecall/hooks/onboarding.py`
- modify: `src/ccrecall/hooks/write_config.py`
- modify: `src/ccrecall/hooks/clear_handoff.py`
- modify: `src/ccrecall/hooks/memory_sync.py`
- modify: `src/ccrecall/hooks/memory_context.py`
- modify: `src/ccrecall/hooks/memory_setup.py`
- modify: `src/ccrecall/hooks/import_conversations.py`
- modify: `src/ccrecall/hooks/backfill_summaries.py`
- modify: `src/ccrecall/hooks/sync_current.py`
- modify: `src/ccrecall/hooks/warm_model.py`
- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `tests/test_db.py`
- modify: `tests/test_write_config.py`
- modify: `tests/test_onboarding.py`
- modify: `tests/test_legacy_migration.py`
- modify: `tests/test_context_injection.py`
- read: `design/specs/001-architecture-cleanup/design.md`
- read: `design/specs/001-architecture-cleanup/tasks/context.md`

## Prompt

Implement the `Architecture -> Runtime Boundary Extraction` section of `design/specs/001-architecture-cleanup/design.md`.

Move responsibilities out of `src/ccrecall/db.py` into these modules:

- `paths.py`: stable runtime path constants and `ensure_parent_dir`.
- `settings.py`: `DEFAULT_SETTINGS`, `CURRENT_ONBOARDING_VERSION`, `load_config`, `load_settings`, `get_db_path`, `resolve_db_settings`.
- `database.py`: `apply_base_pragmas`, `get_db_connection`, schema application, and non-semantic DB helpers that do not require sqlite-vec.
- `runtime_files.py`: `atomic_write_json`, `pid_file_path`, `remove_pid_file`, and PID/temp constants such as `SYNC_TEMP_PREFIX` that are still in transition.
- `logging_config.py`: `setup_logging`, `log_hook_exception`, log constants.

Update callers to import from the new modules. Keep `db.py` only as a small compatibility facade if that reduces churn, but it must not import `sqlite_vec`, `ccrecall.embeddings`, or other semantic-native modules at top level after this task. Do not implement the semantic vector boundary here; leave vector helpers in place only if they remain isolated from base imports for T02 to move.

Preserve direct hook entry points and JSON stdout. Update tests that monkeypatch `ccrecall.db.CONFIG_PATH`, import `CURRENT_ONBOARDING_VERSION`, or inspect `health.py` imports so they point at the new modules or a compatibility alias intentionally retained for this pass.

## Focus

Reverse dependencies show almost every subsystem imports `ccrecall.db`, including hooks, `recent_chats.py`, `session_tail.py`, `legacy.py`, `search_conversations.py`, and many tests. Keep this task mostly mechanical and behavior-preserving. `tests/test_context_injection.py` contains comments/assertions around semantic-native imports; update the comments so they no longer assume `db.py` transitively imports fastembed. Avoid circular imports: `models.py` currently holds `LOGGER_NAME` and `BUSY_TIMEOUT_MS` to avoid cycles.

## Verify

- [ ] FR#1: Hook modules, recent, tail, and keyword search import successfully after runtime module extraction.
- [ ] FR#2: Importing the base database/settings/path modules does not import `sqlite_vec`, `fastembed`, or `numpy`.
- [ ] FR#10: Existing hook tests still verify JSON-only stdout behavior after import rewrites.
- [ ] AC#1: A missing-semantic simulation can import `ccrecall.cli`, hook modules, and DB connection code without native dependency failures.
- [ ] AC#9: SessionStart and Stop hook tests still assert only valid JSON hook envelopes are printed.
