---
task_id: "T01"
title: "Add logging to DB/status-facing core modules (highest dark-operation risk)"
status: "done"
depends_on: []
implements: ["FR#1", "FR#3", "FR#4", "AC#4"]
---

## Target Files

- modify: `src/ccrecall/schema.py`
- modify: `src/ccrecall/search_cli.py`
- modify: `src/ccrecall/status.py`
- modify: `src/ccrecall/ingestion_status.py`

## Prompt

Read `design.md` (Approach section) and `tasks/context.md` (Key Decisions 1-5) before starting — they define the logging convention and the judgment rules for this whole feature.

These four modules currently have zero logging (`grep -n getLogger` on each returns nothing) and are the highest dark-operation risk in the codebase: they swallow `sqlite3.Error`, `OSError`, and `FileNotFoundError` today with no trace.

For each file:

1. Read the whole file first.
2. Add `import logging` and `from ccrecall.models import LOGGER_NAME` to the import block, then `log = logging.getLogger(LOGGER_NAME)` at module level (match the exact pattern in `src/ccrecall/search_vector.py` lines 1-12 — that module is already fixed, use it as your reference).
3. For every `except` block that does not re-raise: add a log call. Use `log.exception(...)` when inside the `except` (auto-attaches the traceback), `log.error(...)` when logging a failure that isn't from a caught exception (e.g. a status code check). Apply the decision tree in `rules/common/logging.md` ("Choosing a Level") to pick DEBUG/INFO/WARNING/ERROR — not everything is ERROR. A condition that's genuinely expected in normal operation (e.g. a file that's been deleted between listing and stat) is WARNING or DEBUG, not ERROR.
4. Known spots to check (confirm these are still accurate against current `HEAD` — line numbers may have shifted):
   - `schema.py`'s `detect_fts_support` — swallows `sqlite3.Error`, returns `None`. This is worth an `exception()` log: silently having no FTS support changes downstream search behavior and would otherwise be very hard to diagnose.
   - `search_cli.py` — multiple `sqlite3.Error`/`OSError` catches. Read each one's context to judge severity.
   - `ingestion_status.py`'s `_source_fingerprint` — catches `FileNotFoundError` per-file inside a loop and returns `None` for the whole fingerprint. This is a cache-invalidation signal, not necessarily an error — judge whether WARNING (unexpected) or DEBUG (routine race with concurrent file changes) fits better given the function's docstring and callers.
   - `status.py` — check both flagged spots for their actual exception context; log at the boundary, not deeper in helper functions that don't own the try/except.
5. Add module-level or call-site logging at other qualifying boundaries you find while reading (external I/O, state transitions) even if not listed above — the four spots above are a starting point from a prior audit, not the full list.
6. Do not add a log call inside any `except` block that already re-raises (propagating errors are not dark operations).
7. Use `extra={...}` for structured context (IDs, counts, paths) per `rules/common/logging.md` "Structured Context" — don't interpolate everything into the message string.

## Verify

- [ ] FR#1: Every non-reraising `except` in these 4 files (confirmed via `grep -n except src/ccrecall/schema.py src/ccrecall/search_cli.py src/ccrecall/status.py src/ccrecall/ingestion_status.py` and manual read) logs its outcome.
- [ ] FR#3: External I/O boundaries in these files (DB reads, file stats) log on failure.
- [ ] FR#4: `grep -n "getLogger\|basicConfig\|setLevel\|addHandler"` on each of the 4 files shows only `getLogger(LOGGER_NAME)` — no handler/level configuration added.
- [ ] AC#4 (partial — `ingestion_status.py`): Manually trigger `_source_fingerprint` with a missing file (e.g. via a small script or an existing test fixture that deletes a referenced transcript file mid-run) and confirm a log line appears in `~/.ccrecall/ccrecall-<process>.log` for whichever process path exercises this function — not via a log-capture test.
- [ ] `uv run pytest` passes with no new failures introduced by this task's changes.
