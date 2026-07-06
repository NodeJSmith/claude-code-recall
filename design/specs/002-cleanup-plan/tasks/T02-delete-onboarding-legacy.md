---
task_id: "T02"
title: "Delete onboarding and legacy migration systems"
status: "done"
depends_on: ["T01"]
implements: ["FR#2", "FR#3", "FR#19"]
---

## Summary

Delete the onboarding system (interactive config prompts) and the legacy migration system (`~/.claude-memory` → `~/.ccrecall`). Replace onboarding with the existing "create config with defaults if missing" behavior that `load_config()`/`load_settings()` already provide. Remove the `ccrecall-onboarding` console script from `pyproject.toml` and `hooks/hooks.json`. Clean blast radius into `cli/commands.py`, `db.py`, `memory_context.py`, `memory_setup.py`, `test_db.py`, and `test_sync_hook.py`.

## Target Files

- delete: `src/ccrecall/hooks/onboarding.py`
- delete: `src/ccrecall/hooks/write_config.py`
- delete: `src/ccrecall/legacy.py`
- delete: `tests/test_onboarding.py`
- delete: `tests/test_write_config.py`
- delete: `tests/test_legacy_migration.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/hooks/memory_context.py`
- modify: `src/ccrecall/hooks/memory_setup.py`
- modify: `tests/test_db.py`
- modify: `tests/test_sync_hook.py`
- modify: `pyproject.toml`
- modify: `hooks/hooks.json`
- read: `design/specs/002-cleanup-plan/design.md` (§ Architecture → Deletion pass → Onboarding, Legacy)

## Prompt

Delete the onboarding and legacy migration systems and clean all references from shared files.

### Onboarding deletion

**Files to delete**: `src/ccrecall/hooks/onboarding.py`, `src/ccrecall/hooks/write_config.py`, `tests/test_onboarding.py`, `tests/test_write_config.py`.

**Shared file cleanup:**

1. `src/ccrecall/cli/commands.py`: Remove the `hooks.write_config` import (line 28) and the `cmd_write_config` command definition (lines 289-297).

2. `src/ccrecall/db.py`: Remove the `CURRENT_ONBOARDING_VERSION = 1` constant (line 64).

3. `src/ccrecall/hooks/memory_context.py`: Remove the onboarding gate at lines 593-603. This section silently blocks context injection for users who never completed onboarding. Remove the comment, `load_config()` call, the `if not config.get("onboarding_completed")` check and its body (including the `conn.close()` and `_emit_with_proactive` calls inside it). **Keep line 591** (`proactive_block = ...`) which is read by other gates downstream.

4. `tests/test_db.py`: Remove `CURRENT_ONBOARDING_VERSION` from the `from ccrecall.db import (...)` block (line 14). Remove the `TestCurrentOnboardingVersion` class (including the `class` line and all its methods).

5. `pyproject.toml`: Remove the `ccrecall-onboarding` line from `[project.scripts]` (line 74).

6. `hooks/hooks.json`: Remove the onboarding hook entry (the block at lines 11-15 referencing `ccrecall-onboarding`).

### Legacy migration deletion

**Files to delete**: `src/ccrecall/legacy.py`, `tests/test_legacy_migration.py`.

**Shared file cleanup:**

1. `src/ccrecall/cli/commands.py`: Remove the `legacy as legacy_mod` import (line 15) and the `cmd_migrate` command definition (lines 92-100).

2. `src/ccrecall/hooks/memory_setup.py`: Remove imports at lines 26-27 (`from ccrecall.legacy import PID_KEY as MIGRATE_PID_KEY` and `find_legacy_db`). Remove the `MIGRATION_NOTICE` string constant (line 36). Within the `main()` function at lines 161-182, carefully remove ONLY the legacy-specific code: `legacy_db = find_legacy_db()`, the `if legacy_db is not None:` branch (which spawns `ccrecall migrate` and sets `MIGRATION_NOTICE`), and the `legacy_db is None and` guard on the backfill trigger. **Preserve**: the `if db_absent:` initial-import trigger and the `elif _needs_reimport(settings):` re-import trigger — these are essential first-run logic.

3. `tests/test_sync_hook.py`: Remove the 3 legacy monkeypatches at lines 784, 806, and 825 (these monkeypatch `memory_setup.find_legacy_db`).

After all deletions, run `uv run pytest` to verify no test failures, then run `uvx prek run --all-files`.

## Focus

- The `memory_setup.py` cleanup is the highest-risk edit. Lines 161-182 interleave legacy migration code with essential first-run logic (`db_absent` check, `_needs_reimport` check). Read the full function before editing to understand the control flow. The `if legacy_db is not None:` branch is nested inside a broader conditional — after removing it, the remaining `if db_absent:` / `elif _needs_reimport(settings):` flow must still be syntactically correct.
- The `memory_context.py` onboarding gate (lines 593-603) must be removed completely — if left in, fresh installs silently get no context injection. But line 591 (`proactive_block = ...`) must stay because later gates read it.
- The `hooks.json` onboarding entry is the second of three hooks in the `SessionStart` array. After removal, the array should have two entries (setup and context). Verify the JSON stays valid.
- In `cli/commands.py`, both `cmd_write_config` and `cmd_migrate` are being removed. T01 also removes `cmd_tokens` from this file. If T01 hasn't run yet, only remove the onboarding and legacy commands/imports.

## Verify

- [ ] FR#2: `hooks/onboarding.py` and `hooks/write_config.py` are deleted; `grep -r 'onboarding\|write_config' src/ccrecall/` returns no import references to deleted modules
- [ ] FR#3: `legacy.py` is deleted; `grep -r 'from ccrecall.legacy\|from ccrecall import legacy' src/` returns nothing
- [ ] FR#19: The onboarding gate in `memory_context.py` is removed (no `onboarding_completed` check remains in the function)
- [ ] FR#2: `ccrecall-onboarding` is absent from `pyproject.toml` and `hooks/hooks.json`
- [ ] FR#2: `uv run pytest` passes with zero failures
- [ ] FR#3: `uvx prek run --all-files` passes
