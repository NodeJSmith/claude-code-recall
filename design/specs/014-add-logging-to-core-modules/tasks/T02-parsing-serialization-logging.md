---
task_id: "T02"
title: "Add logging to parsing/serialization core modules"
status: "planned"
depends_on: []
implements: ["FR#1", "FR#2", "FR#3", "FR#4", "AC#4"]
---

## Target Files

- modify: `src/ccrecall/parsing.py`
- modify: `src/ccrecall/serialization.py`
- modify: `src/ccrecall/dates.py`
- modify: `src/ccrecall/formatting.py`
- modify: `src/ccrecall/transcript_sources.py`

## Prompt

Read `design.md` (Approach section) and `tasks/context.md` (Key Decisions 1-5, especially 3) before starting — decision 3 is specifically about this task's `parsing.py` work.

These 5 modules currently have zero logging. For each file:

1. Read the whole file first.
2. Add `import logging` and `from ccrecall.models import LOGGER_NAME` to the import block, then `log = logging.getLogger(LOGGER_NAME)` at module level (match the pattern in `src/ccrecall/search_vector.py` lines 1-12).
3. `parsing.py` has three generator functions (`parse_jsonl_file`, `parse_lines_with_uuids`, `parse_lines_with_uuids_and_numbers`) that each catch `json.JSONDecodeError` per-line inside a loop over a whole transcript file and silently `continue`. **Do not log inside the loop** — that would spam one log line per malformed line in a file that can have thousands of lines. Instead, count skipped lines within each call and log one summary line (WARNING if count > 0, since malformed JSONL in a transcript file is unexpected) after the loop completes, including the file path and skipped count via `extra=`. This is a generator, so "after the loop" means after the last yield — structure this so the summary still logs even if the caller doesn't fully drain the generator only up to a point they need (a `try/finally` inside the generator, or restructure to collect-then-log if that's simpler and doesn't change the streaming behavior other callers rely on — read the callers first via `grep -rn "parse_jsonl_file\|parse_lines_with_uuids"  src/ccrecall/` to confirm you're not breaking lazy consumption).
4. `dates.py`, `formatting.py`, `serialization.py`, `transcript_sources.py`: read each `except` block found via `grep -n except <file>` and apply the `rules/common/logging.md` decision tree. Not every one needs ERROR — judge whether the caught condition is routine (DEBUG/WARNING) or a genuine failure the caller can't recover from cleanly (ERROR/`exception()`).
5. Do not add a log call inside any `except` block that already re-raises.
6. Use `extra={...}` for structured context (file paths, line numbers, counts) rather than interpolating into the message string.

## Verify

- [ ] FR#1: Every non-reraising `except` in these 5 files logs its outcome (verify via `grep -n except` on each file plus manual read).
- [ ] FR#2: `parsing.py`'s three loop-based parsers log one summary line per call (not per skipped line) — confirm by reading the diff, and by running a small script that feeds a JSONL file with a mix of valid and malformed lines through `parse_jsonl_file` and checking only one log line appears in `~/.ccrecall/ccrecall-<process>.log` (or the equivalent process log for whichever entry point you exercise), not one per bad line.
- [ ] FR#3: External I/O boundaries in these files log on failure.
- [ ] FR#4: `grep -n "getLogger\|basicConfig\|setLevel\|addHandler"` on each of the 5 files shows only `getLogger(LOGGER_NAME)`.
- [ ] AC#4 (partial — `parsing.py`): Manually run `parse_jsonl_file` (or a downstream import path that calls it) against a transcript file containing at least one malformed JSON line, and confirm the summary log line appears in the relevant per-process log file after the run.
- [ ] `uv run pytest` passes with no new failures introduced by this task's changes.
