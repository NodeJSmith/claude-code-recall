---
task_id: "T01"
title: "Extract shared batch-loop scaffolding for the two backfill run() functions"
status: "done"
depends_on: []
implements: ["FR#1", "FR#2", "FR#3"]
---

## Target Files

- create: `src/ccrecall/hooks/backfill_runner.py`
- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- modify: `src/ccrecall/hooks/backfill_tool_content.py`
- modify (only if a coverage gap surfaces): `tests/test_backfill_embeddings.py`, `tests/test_backfill_tool_content.py`

## Prompt

Read `src/ccrecall/hooks/backfill_embeddings.py` and `src/ccrecall/hooks/backfill_tool_content.py` in full — both `run()` functions currently duplicate the same outer batch-loop shape:

```
while True:
    if limit is not None and total_updated >= limit: break
    rows = <select batch>
    if not rows: break
    current_ids = [r[0] for r in rows]
    if current_ids == last_batch_ids:
        <no-progress action — DIFFERS: embeddings aborts; tool_content excludes ids + continues>
    last_batch_ids = current_ids
    try:
        <process each row — DIFFERS: exception taxonomy, savepoints, progress-message content>
    except Exception:
        <log + commit + return EXIT_ABORT>
    conn.commit()
    <batch trailer — DIFFERS: embeddings also calls reclaim_memory(libc)>
    time.sleep(BACKFILL_BATCH_DELAY_SECONDS)
```

Create `src/ccrecall/hooks/backfill_runner.py` (same layering as the existing `backfill_query.py`/`backfill_status.py`, which both `run()` functions already import from) exposing a generic driver, e.g.:

```python
def run_batch_loop(
    *,
    select_batch: Callable[[], list[tuple]],
    is_limit_reached: Callable[[], bool],
    process_batch: Callable[[list[tuple]], bool],  # returns False to signal batch-level abort (mirrors the outer except Exception -> EXIT_ABORT path)
    on_stuck: Callable[[list[int]], bool],  # current_ids passed in; returns True to abort the whole run, False to continue (already excluded by the caller)
    after_batch: Callable[[list[tuple]], None],  # commit + domain-specific trailer (reclaim_memory, sleep)
) -> bool:  # False => caller should return EXIT_ABORT
```

(Exact parameter names/shape are yours to finalize — the design doc's job was to establish the boundary, not dictate the signature. Keep it readable; avoid over-parameterizing beyond what these two call sites actually need.)

Wire both `run()` functions to call `run_batch_loop`, moving:
- the `while True` / `--limit` short-circuit / stuck-batch **detection** (`current_ids == last_batch_ids`) into the shared driver — shared for real, per design.md FR#2.
- the no-progress **recovery action** into each caller's own `on_stuck` callback: embeddings' callback logs + returns "abort"; tool_content's callback does its existing `exclude_ids.update(...)`, `skipped_stuck += len(...)`, logs, and returns "continue". Do NOT collapse these into one shared action — they are genuinely different (FR#2).
- the per-item loop (SAVEPOINT handling, exception taxonomy, counters, progress-message text) into each caller's own `process_batch` callback, unchanged in content — just relocated. Do NOT try to unify embeddings' exception classes (`ValueError/OverflowError/UnicodeError` → content-error sentinel; bare `Exception` → abort) with tool_content's (`LockExhaustedError/OSError/json.JSONDecodeError/ValueError/TypeError/KeyError` → per-session skip). Do NOT try to unify the rate/ETA computation (embeddings: windowed `deque` over message-count `work_done`; tool_content: flat `total_updated/elapsed` average) — these reflect real cost-model differences per design.md's Approach section.
- the batch trailer into `after_batch`: `conn.commit()` then `reclaim_memory(libc); time.sleep(...)` for embeddings, `time.sleep(...)` only for tool_content.

Leave the pre-loop setup (settings/logger, `--status` short-circuit, `os.nice`, initial counters, initial `SELECT COUNT(*)` for `total_eligible`/`total_work`) and the post-loop completion report (logger.info, `print`, `json_mode` branch) exactly where they are in each `run()` — these aren't part of the duplicated loop shape and don't belong in the shared driver.

Run `uv run pytest tests/test_backfill_embeddings.py tests/test_backfill_tool_content.py -v` after wiring each file, one at a time, so a failure points at exactly one file's extraction.

## Verify

- [ ] FR#1: `run_batch_loop` (or equivalently-named driver) in `backfill_runner.py` is called from both `backfill_embeddings.run()` and `backfill_tool_content.run()`, replacing their duplicated outer while-loop.
- [ ] FR#2: Reading the diff, confirm embeddings' stuck-batch path still returns `EXIT_ABORT` and tool_content's still excludes + continues — i.e. the callback bodies weren't merged into one shared behavior.
- [ ] FR#3: Reading the diff, confirm `reclaim_memory(libc)` still only runs in the embeddings path, not tool_content.
- [ ] AC#1: `uv run pytest tests/test_backfill_embeddings.py tests/test_backfill_tool_content.py` passes with 0 failures.
- [ ] AC#3 (partial — full suite verified in the final gate): `uv run pytest` passes.
- [ ] AC#4 (partial): `uvx prek run --all-files` passes for the files this task touches.
