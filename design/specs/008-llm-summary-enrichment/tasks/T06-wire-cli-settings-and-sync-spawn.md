---
task_id: "T06"
title: "Wire CLI settings and sync spawn"
status: "planned"
depends_on: ["T05"]
implements: ["FR#5", "FR#6", "FR#11", "FR#18", "AC#1", "AC#6", "AC#7", "AC#11"]
---

## Summary

Expose the canonical manual backfill command, user settings, direct internal worker script, and opt-in post-sync spawn.
Keep the direct entry point lightweight and hook-visible output unchanged.

## Target Files

- modify: `src/ccrecall/config.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `src/ccrecall/hooks/sync_current.py`
- modify: `pyproject.toml`
- create: `tests/test_llm_summary_cli.py`
- modify: `tests/test_sync_hook.py`
- read: `src/ccrecall/hooks/memory_sync.py`
- read: `src/ccrecall/cli/context.py`

## Prompt

Add the six LLM settings using `DEFAULT_SETTINGS`: opt-in boolean, configurable model defaulting to `sonnet`, effort, timeout, `$1.00` budget threshold, and minimum exchange count. Register canonical `ccrecall backfill llm-summaries` flags (`--days`, `--limit`, `--session`, `--force`, `--check-capability`) with existing cyclopts validation style. Make `--check-capability` mutually exclusive with selectors and delegate all paths to T05's worker/capability boundary.

Add the internal `ccrecall-llm-summaries` console script in `pyproject.toml`. Its import graph must reach only lightweight config/DB/LLM modules, never the full cyclopts command graph or embedding dependencies. In `sync_current.run()`, after `sync_session()` commits/closes and only when new messages arrived and opt-in is true, detached-spawn the internal entry with one session/limit. Reuse the cross-platform detached subprocess pattern; do not run Claude in `sync_current` and do not change any hook stdout envelope.

## Focus

`commands.py` eagerly imports embedding modules, which is acceptable for the canonical user CLI but not the direct spawned worker. `sync_current.py` currently calls `get_connection(..., load_vec=True)` and prints its hook payload after DB close; the new spawn belongs after that close and must be best-effort. Preserve its PID guard and all existing early returns. Help/documentation language must accurately state that `$1.00` is a configurable upstream stop threshold, not a guaranteed maximum charge.

## Verify

- [ ] FR#5: CLI tests cover the canonical manual command, filters, force, capability-only mode, and delegation to the worker.
- [ ] FR#6: Sync-hook tests prove exactly one detached worker spawn only for enabled settings plus new messages, with no extra stdout or inline Claude call.
- [ ] FR#6: Sync-hook tests prove the enrichment spawn occurs only after the DB context has committed and closed, and a spawn failure remains best-effort without changing the JSON hook envelope.
- [ ] FR#11: Help/config tests explicitly disclose selected local transcript content, branch/session metadata, and source-path provenance sent through Claude Code auth only after opt-in.
- [ ] FR#18: Settings and argv tests preserve configured model/budget values and the `$1.00` defaults.
- [ ] AC#1: Existing sync-hook tests retain deterministic sync behavior and hook output when LLM enrichment is disabled.
- [ ] AC#6: Hook contract/import tests retain JSON-only stdout for `memory_sync.py`, `memory_context.py`, and `memory_setup.py`, and prove `sync_current.py` only spawns the detached worker.
- [ ] AC#6: Clean-process import tests prove `memory_sync.py`, `memory_context.py`, and `memory_setup.py` do not import `llm_summarizer.py`; `sync_current.py` never invokes Claude directly.
- [ ] AC#7: Direct-console subprocess tests prove no vec tables, embedding model, or heavy embedding modules are loaded.
- [ ] AC#7: Direct-console tests prove startup and worker execution never call a `load_vec=True` connection path.
- [ ] AC#7: Direct-console subprocess tests prove `ccrecall-llm-summaries` does not import the cyclopts CLI command graph.
- [ ] AC#11: CLI help/config tests disclose the budget-threshold behavior.
