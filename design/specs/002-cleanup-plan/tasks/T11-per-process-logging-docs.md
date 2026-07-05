---
task_id: "T11"
title: "Add per-process logging and update documentation"
status: "planned"
depends_on: ["T09", "T10"]
implements: ["FR#12", "FR#13", "AC#1", "AC#2", "AC#9"]
---

## Summary

Modify `setup_logging` (in `config.py`) to accept a `process_name` parameter so each process type writes to its own rotating log file. Wire up logging in all hook entry points, background processes, and the CLI. Add systematic logging to code paths that currently have none. Update CLAUDE.md with all architecture changes from the cleanup.

## Target Files

- modify: `src/ccrecall/config.py`
- modify: `src/ccrecall/hooks/memory_setup.py`
- modify: `src/ccrecall/hooks/memory_sync.py`
- modify: `src/ccrecall/hooks/memory_context.py`
- modify: `src/ccrecall/hooks/sync_current.py`
- modify: `src/ccrecall/hooks/clear_handoff.py`
- modify: `src/ccrecall/hooks/import_conversations.py`
- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- modify: `src/ccrecall/hooks/backfill_summaries.py`
- modify: `src/ccrecall/hooks/warm_model.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `src/ccrecall/search_conversations.py`
- modify: `CLAUDE.md`
- read: `design/specs/002-cleanup-plan/design.md` (§ Architecture → Per-process logging, § Documentation Updates)

## Prompt

### Modify setup_logging (FR#13)

In `src/ccrecall/config.py`, modify `setup_logging` to accept `process_name`:

```python
def setup_logging(settings: dict, process_name: str = "ccrecall") -> logging.Logger:
    log_dir = RUNTIME_DIR
    log_path = log_dir / f"ccrecall-{process_name}.log"
    # ... RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
```

Update `log_hook_exception` to pass process name through:
```python
def log_hook_exception(context: str) -> None:
    setup_logging(load_settings(), process_name=context).exception("%s hook failed", context)
```

### Wire up process names (FR#12, FR#13)

| Process name | Call site |
|---|---|
| `setup` | `hooks/memory_setup.py` — rename `"memory-setup"` to `"setup"` |
| `sync` | `hooks/memory_sync.py` — rename `"memory-sync"` to `"sync"`; `hooks/sync_current.py` — add `process_name="sync"` |
| `context` | `hooks/memory_context.py` — add `process_name="context"`; add `log_hook_exception("context")` (currently missing) |
| `clear-handoff` | `hooks/clear_handoff.py` — keep as-is |
| `import` | `hooks/import_conversations.py` — add `process_name="import"` |
| `backfill-embed` | `hooks/backfill_embeddings.py` — add `process_name="backfill-embed"` |
| `backfill-summary` | `hooks/backfill_summaries.py` — add `process_name="backfill-summary"` |
| `warm-model` | `hooks/warm_model.py` — add `setup_logging` call (currently no logging) |
| `cli` | `cli/commands.py` — add `setup_logging` call in CLI entry point |

### Add systematic logging (FR#12)

Add INFO/DEBUG logging to:
1. **Search** (`search_conversations.py`): Log query, result count, whether vector search used.
2. **Context injection** (`memory_context.py`): Log sessions injected and context token count.
3. **Warm model** (`warm_model.py`): Log warm-up start/completion.

### Update CLAUDE.md

1. **Architecture section**: Mention `config.py` split, session-keyed branches, per-process logging.
2. **Fix the "Two invariants" heading and add a fourth**: The heading says "two" but documents three. Add a fourth invariant for branch-count check (one active branch per session, enforced by `UNIQUE(session_id)` and validated by `ccrecall stats`). Update heading to "Four invariants to preserve".
3. **Remove token references** from module descriptions.
4. **Update Names table**: Remove `ccrecall-onboarding`. Verify `ccrecall-warm-model` is listed.
5. **Update Commands section**: Remove `ccrecall tokens`, `ccrecall migrate`, `ccrecall write-config`.

### Final verification (AC#1, AC#2)

This is the last task. Run full verification:
```bash
uv run pytest         # AC#1
uvx prek run --all-files  # AC#2
```

## Focus

- The `setup_logging` signature change must be backwards-compatible — `process_name` has a default value so existing callers still work.
- `memory_context.py` currently has no `log_hook_exception` — it uses `sys.exit(0)` with no error logging. Add error logging.
- `warm_model.py` has no logging at all. Add `setup_logging` call and basic INFO logging.
- The CLAUDE.md update should describe the post-cleanup architecture, not the old one.

## Verify

- [ ] FR#12: Search queries, context injection, and warm-model operations produce INFO or DEBUG log entries
- [ ] FR#13: Each process type writes to its own log file (`~/.ccrecall/ccrecall-<process>.log`)
- [ ] AC#9: Running hooks produces log files at the expected paths
- [ ] AC#1: `uv run pytest` passes with zero failures
- [ ] AC#2: `uvx prek run --all-files` passes
- [ ] FR#13: CLAUDE.md mentions config.py split, session-keyed branches, per-process logging, and has "Four invariants to preserve" heading with the branch-count invariant as the fourth entry
