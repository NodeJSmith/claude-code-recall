---
task_id: "T10"
title: "Decompose search_conversations.py into 5 modules"
status: "planned"
depends_on: ["T08"]
implements: ["FR#11", "AC#7", "AC#8"]
---

## Summary

Split the 879-line `search_conversations.py` into 5 focused modules: `search_query.py` (FTS query building), `search_vector.py` (KNN/vector execution + snippet hydration), `search_hydrate.py` (result dedup + card hydration), `search_conversations.py` (orchestrators), and `search_cli.py` (CLI entry points). No behavior changes — purely structural.

## Target Files

- create: `src/ccrecall/search_query.py`
- create: `src/ccrecall/search_vector.py`
- create: `src/ccrecall/search_hydrate.py`
- create: `src/ccrecall/search_cli.py`
- modify: `src/ccrecall/search_conversations.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `tests/test_search.py`
- modify: `tests/test_sync_hook.py`
- read: `design/specs/002-cleanup-plan/design.md` (§ Architecture → Search decomposition)

## Prompt

### Module assignment

Read `src/ccrecall/search_conversations.py` fully. Move functions per this table:

| Target module | Functions to move | Est. lines |
|---|---|---|
| `search_query.py` | `scope_filter_clause`, `_get_fts_branch_ids`, query-building constants | ~134 |
| `search_vector.py` | `_execute_chunk_knn`, `_get_vec_chunk_ids`, `_hydrate_snippets` | ~170 |
| `search_hydrate.py` | `_dedup_by_session`, `_hydrate_cards` | ~165 |
| `search_conversations.py` | `search_sessions`, `search_messages`, `_compute_caveat` | ~175 |
| `search_cli.py` | `run`, `run_messages`, `print_status`, format wrappers, `MAX_SEARCH_RESULTS` | ~215 |

### Import structure

- `search_query.py` imports from `ccrecall.db`/`ccrecall.config`
- `search_vector.py` imports from `ccrecall.db`
- `search_hydrate.py` imports from `ccrecall.formatting`
- `search_conversations.py` imports from `search_query`, `search_vector`, `search_hydrate`, `ccrecall.db`, `ccrecall.fusion`
- `search_cli.py` imports from `search_conversations`, `ccrecall.db`/`ccrecall.config`, `ccrecall.health`, `ccrecall.formatting`

### CLI commands update

`src/ccrecall/cli/commands.py` does `from ccrecall import search_conversations as search_mod` and references `search_mod.MAX_SEARCH_RESULTS` in `Annotated[...]` parameter defaults (evaluated at import time) and `search_mod.run(...)` / `search_mod.run_messages(...)` in command bodies. After the split, `run`, `run_messages`, and `MAX_SEARCH_RESULTS` live in `search_cli.py`. Update the import to `from ccrecall import search_cli as search_mod` (or import specific symbols).

### Test updates

1. `tests/test_search.py`: Update imports to point to correct new modules.
2. `tests/test_sync_hook.py`: Update `run` import from `search_conversations` to `search_cli`.

### Constraints

- Boundary types stay simple: branch IDs, score tuples, result dicts. No new classes.
- No behavior changes. Search results must be identical.
- Keep `is_active = 1` filters in all SQL queries.
- Each new module under 400 lines (AC#7).

Run `uv run pytest` and `uvx prek run --all-files`.

## Focus

- Read the entire 879-line file before making any moves. Some functions have subtle dependencies on module-level state.
- `_compute_caveat` uses `health.py`'s embedding status — it stays in the orchestrator module.
- `MAX_SEARCH_RESULTS` is referenced by CLI functions. Check all references before placing it.
- The `cli/commands.py` update is critical — without it, `ccrecall search` breaks on import.
- `formatting.py` (381 lines) and `fusion.py` (40 lines) are unchanged.

## Verify

- [ ] FR#11: `search_conversations.py` contains only `search_sessions`, `search_messages`, `_compute_caveat` (~175 lines)
- [ ] FR#11: `search_query.py`, `search_vector.py`, `search_hydrate.py`, `search_cli.py` exist with correct functions
- [ ] AC#7: No decomposed module exceeds 400 lines
- [ ] AC#8: `uv run pytest tests/test_search.py` passes — search results equivalent
- [ ] FR#11: `is_active = 1` filters present in all search SQL queries
- [ ] FR#11: `uv run pytest` passes with zero failures
- [ ] FR#11: `uvx prek run --all-files` passes
