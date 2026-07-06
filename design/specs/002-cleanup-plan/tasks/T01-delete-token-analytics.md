---
task_id: "T01"
title: "Delete token analytics subsystem"
status: "done"
depends_on: []
implements: ["FR#1"]
---

## Summary

Delete the token analytics subsystem — 6 source files, 5 test files, and the `skills/ccr-tokens/` skill directory. Clean the blast radius into 4 shared files: `cli/commands.py`, `conftest.py`, `test_db.py`, and `test_boundary_validation.py`. Update the `__init__.py` docstring to remove token submodule references.

This is the largest deletion (~4,651 lines), but the most isolated — token code has a narrow interface with the rest of the codebase.

## Target Files

- delete: `src/ccrecall/token_schema.py`
- delete: `src/ccrecall/token_parser.py`
- delete: `src/ccrecall/token_analytics.py`
- delete: `src/ccrecall/token_output.py`
- delete: `src/ccrecall/token_insights.py`
- delete: `src/ccrecall/token_dashboard.py`
- delete: `tests/test_ingest_token_data.py`
- delete: `tests/test_token_output.py`
- delete: `tests/test_token_insights.py`
- delete: `tests/test_token_parser.py`
- delete: `tests/token_helpers.py`
- delete: `skills/ccr-tokens/` (entire directory)
- modify: `src/ccrecall/cli/commands.py`
- modify: `src/ccrecall/__init__.py`
- modify: `tests/conftest.py`
- modify: `tests/test_db.py`
- modify: `tests/test_boundary_validation.py`
- read: `design/specs/002-cleanup-plan/design.md` (§ Architecture → Deletion pass → Token analytics)

## Prompt

Delete the token analytics subsystem and clean all references from shared files.

**Source files to delete** (6): `src/ccrecall/token_schema.py`, `src/ccrecall/token_parser.py`, `src/ccrecall/token_analytics.py`, `src/ccrecall/token_output.py`, `src/ccrecall/token_insights.py`, `src/ccrecall/token_dashboard.py`.

**Test files to delete** (5): `tests/test_ingest_token_data.py`, `tests/test_token_output.py`, `tests/test_token_insights.py`, `tests/test_token_parser.py`, `tests/token_helpers.py`.

**Skill directory to delete**: `skills/ccr-tokens/` (entire directory — the skill depends on the removed token subsystem).

**Shared file cleanup:**

1. `src/ccrecall/cli/commands.py`: Remove the `token_dashboard` import (line 19) and the `cmd_tokens` command definition (line 283 area). Do not remove other imports or commands.

2. `tests/conftest.py`: Remove only the token-specific imports at lines 8, 13-15 (`token_helpers`, `token_analytics`, `token_parser`, `token_schema`). Do NOT delete lines 10-12 which import `_ensure_vec_schema`, `health`, and `SCHEMA` — those are used by non-token fixtures. Remove the `token_db` fixture (lines 72-79) and `populated_token_db` fixture (lines 82-107), including their `@pytest.fixture` decorators.

3. `tests/test_db.py`: Remove the `token_schema` import (line 26: `from ccrecall.token_schema import ensure_schema`). Remove `TestNoTokenSnapshotsOnConversationDb` class (lines 731-749, 2 tests). In `TestExistingV6DbOpen`, remove ONLY the token_snapshots-specific lines (CREATE TABLE token_snapshots, INSERT, and the final token_snapshots assertion) — preserve the core test assertions (PRAGMA user_version, get_db_connection reopen, projects/sessions/branches/messages row-count checks).

4. `tests/test_boundary_validation.py`: Remove `token_parser` import (line 15), the `_jnl` helper function (lines 75-81), and `TestTokenValidation` class (lines 84-120, 3 tests).

5. `src/ccrecall/__init__.py`: Remove the token submodule lines from the docstring (lines 12-17 covering `token_schema`, `token_parser`, `token_analytics`, `token_output`, `token_insights`, `token_dashboard`).

After all deletions, run `uv run pytest` to verify no test failures, then run `uvx prek run --all-files` to verify lint/format/type-check passes.

## Focus

- The `conftest.py` cleanup is the most error-prone part — the token imports are interleaved with non-token imports. Lines 10-12 (`_ensure_vec_schema`, `health`, `SCHEMA`) must be preserved.
- In `test_db.py`, the `TestExistingV6DbOpen` class tests that `get_db_connection` preserves existing data. The token_snapshots lines within that test verify a table that will no longer exist — remove those lines but keep the rest of the test intact. The test method `test_existing_v6_db_rows_intact_after_get_db_connection` must continue to pass after removing the token assertions.
- `skills/ccr-tokens/SKILL.md` is the only file in the skill directory — delete the whole directory.

## Verify

- [ ] FR#1: All 6 token source files are deleted and no module imports them (`grep -r 'token_schema\|token_parser\|token_analytics\|token_output\|token_insights\|token_dashboard' src/` returns nothing)
- [ ] FR#1: All 5 token test files and `skills/ccr-tokens/` directory are deleted
- [ ] FR#1: `tests/conftest.py` retains `_ensure_vec_schema`, `health`, `SCHEMA` imports and `memory_db`, `jsonl_fixture` fixtures
- [ ] FR#1: `uv run pytest` passes with zero failures
- [ ] FR#1: `uvx prek run --all-files` passes
