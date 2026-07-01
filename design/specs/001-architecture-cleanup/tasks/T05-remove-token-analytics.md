---
task_id: "T05"
title: "Remove active token analytics"
status: "planned"
depends_on: ["T01"]
implements: ["FR#5", "FR#6", "AC#4", "AC#5"]
---

## Summary

Remove the unused token analytics product surface. Delete the `ccrecall tokens` command, token skill, token modules/templates, and token-specific tests/fixtures. Keep explicit preservation coverage so existing token tables in user databases are ignored rather than dropped.

## Target Files

- modify: `src/ccrecall/cli/commands.py`
- modify: `src/ccrecall/__init__.py`
- delete: `src/ccrecall/token_parser.py`
- delete: `src/ccrecall/token_schema.py`
- delete: `src/ccrecall/token_analytics.py`
- delete: `src/ccrecall/token_output.py`
- delete: `src/ccrecall/token_insights.py`
- delete: `src/ccrecall/token_dashboard.py`
- delete: `src/ccrecall/templates/dashboard.html`
- delete: `skills/ccr-tokens/`
- modify: `tests/conftest.py`
- delete: `tests/token_helpers.py`
- delete: `tests/test_token_parser.py`
- delete: `tests/test_ingest_token_data.py`
- delete: `tests/test_token_output.py`
- delete: `tests/test_token_insights.py`
- modify: `tests/test_boundary_validation.py`
- modify: `tests/test_db.py`
- modify: `tests/test_cli_context.py`
- read: `design/specs/001-architecture-cleanup/design.md`
- read: `design/specs/001-architecture-cleanup/tasks/context.md`

## Prompt

Implement the `Architecture -> Token Analytics Removal` section.

Remove the active token product surface entirely. Delete `cmd_tokens` and the `token_dashboard_mod` import from `cli/commands.py`. Remove token subsystem descriptions from `src/ccrecall/__init__.py`. Delete `src/ccrecall/token_*.py`, `src/ccrecall/templates/dashboard.html`, `skills/ccr-tokens/`, token fixtures, and token-specific tests.

Keep or add a minimal test that creates representative old token tables/rows in a DB, runs current schema initialization/upgrade, and asserts those tables/rows still exist. Do not import `token_schema.py` for this preservation test because the token source is being removed; create the representative tables directly in the test.

Update `tests/test_boundary_validation.py` to remove token parser boundary tests. Update CLI tests so `ccrecall tokens` is not registered.

Do not drop token tables from any user DB, and do not add compatibility code that reads or writes them.

## Focus

Reverse dependency search found token references in `tests/conftest.py`, `tests/test_boundary_validation.py`, `tests/test_db.py`, `src/ccrecall/__init__.py`, and CLI command registration in addition to token-specific files. `tests/test_db.py` currently imports `token_schema.ensure_schema` and includes token-table preservation assertions; rewrite those tests around manual legacy table creation.

## Verify

- [ ] FR#5: `ccrecall tokens` and `/ccr-tokens` are removed from active product surfaces.
- [ ] FR#6: Existing token analytics tables are ignored and preserved.
- [ ] AC#4: CLI registration no longer includes `tokens`, and `skills/ccr-tokens/` is gone.
- [ ] AC#5: A DB containing old token analytics tables keeps those tables/rows after schema setup/upgrade.
