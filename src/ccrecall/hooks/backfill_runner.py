"""Shared outer batch-loop driver for the backfill `run()` functions.

`backfill_embeddings.run()` and `backfill_tool_content.run()` each drive an
outer batch loop with the same shape: select a batch, stop if the run's
`--limit` is reached or the batch is empty, detect a re-selected ("stuck")
batch, dispatch to per-item processing, then run a batch trailer. This module
owns exactly that shape via `run_batch_loop`, alongside the existing
`backfill_query.py` (constants/selection) and `backfill_status.py` (status
reporting) that both `run()` functions already import from.

Everything domain-specific stays with the caller's callbacks: per-item
processing (SAVEPOINT handling, exception taxonomy, progress-message
content), no-progress *recovery* (embeddings aborts the whole run;
tool_content excludes the stuck ids and continues — only the *detection* is
shared here), and the batch trailer's commit plus any extras
(`reclaim_memory` for embeddings, none for tool_content).

`limit_reached` and `lower_scheduling_priority` are two more bits of
identical start-of-run scaffolding both callers needed; they live here too
rather than being redefined per caller.
"""

import contextlib
import os
from collections.abc import Callable


def run_batch_loop(
    *,
    select_batch: Callable[[], list[tuple]],
    is_limit_reached: Callable[[], bool],
    process_batch: Callable[[list[tuple]], bool],
    on_stuck: Callable[[list[int]], bool],
    after_batch: Callable[[list[tuple]], None],
) -> bool:
    """Drive the shared outer batch loop; callbacks own everything domain-specific.

    - `select_batch()` returns the next batch (already sliced to any
      caller-side `--limit` remainder). An empty return ends the loop.
    - `is_limit_reached()` is checked before every `select_batch()` call.
    - `process_batch(rows)` runs the per-item work for one batch. It owns its
      own exception handling and must commit before returning False (mirrors
      the callers' `except Exception: ...; conn.commit(); return
      EXIT_ABORT` path) — this driver does not commit on that path.
    - `on_stuck(current_ids)` runs when the same batch is re-selected
      (`current_ids == last_batch_ids`). Return True to abort the whole run,
      False to continue — the caller is responsible for changing its own
      `select_batch()` output (e.g. excluding the stuck ids) so the next
      call doesn't re-select the same batch forever.
    - `after_batch(rows)` runs only after a batch's `process_batch` returns
      True. It owns the success-path commit plus any domain-specific
      trailer (memory reclaim, a fixed sleep, ...).

    Returns False if the run should abort (caller returns EXIT_ABORT), True
    on normal loop completion (limit reached or no more rows).
    """
    last_batch_ids: list[int] | None = None
    while True:
        if is_limit_reached():
            break
        rows = select_batch()
        if not rows:
            break
        current_ids = [row[0] for row in rows]
        if current_ids == last_batch_ids:
            if on_stuck(current_ids):
                return False
            continue
        last_batch_ids = current_ids
        if not process_batch(rows):
            return False
        after_batch(rows)
    return True


def limit_reached(limit: int | None, current: int) -> bool:
    """Shared `--limit` check: True once `current` has reached `limit` (no limit = never)."""
    return limit is not None and current >= limit


def lower_scheduling_priority(nice_level: int) -> None:
    """Best-effort nice(2) call so a background backfill yields to interactive work.

    os.nice is POSIX-only and may be denied; either way the run proceeds.
    """
    with contextlib.suppress(AttributeError, OSError):
        os.nice(nice_level)
