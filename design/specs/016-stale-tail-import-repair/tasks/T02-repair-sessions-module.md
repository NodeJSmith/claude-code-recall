---
task_id: "T02"
title: "Add hooks/import_repair.py with repair_sessions() execution loop"
status: "planned"
depends_on: ["T01"]
implements: ["FR#2", "FR#5", "FR#8", "AC#5", "AC#8"]
---

## Target Files

- create: `src/ccrecall/hooks/import_repair.py`
- create: `tests/test_import_repair.py`

## Prompt

Read `design/specs/016-stale-tail-import-repair/design.md`'s Approach section and `tasks/context.md`'s constraints. T01 (already merged) added `ingestion_status.find_repairable_sessions(conn, ...) -> list[tuple[str, int, int, list[Path]]]` returning `(session_uuid, session_id, project_id, filepaths)` for `stale_tail`/`ingestion_gap` sessions, and `ingestion_status.reclassify_session(conn, session_uuid, filepaths) -> str` returning the current classification ("ok", "stale_tail", "ingestion_gap", or "pending_tail") for one session.

Create `src/ccrecall/hooks/import_repair.py` (new module — `hooks/import_conversations.py` is already ~430 lines, per `coding-style.md`'s 200-400 typical range, so this execution loop gets its own file rather than growing that one):

```python
def repair_sessions(
    conn: sqlite3.Connection,
    candidates: list[tuple[str, int, int, list[Path]]],
    on_reclaim: Callable[[], None] = _noop,
) -> tuple[int, int, int, int]:
    """Force-reimport every (session_uuid, session_id, project_id, filepaths)
    candidate. Returns (sessions_repaired, messages_recovered, sessions_failed,
    sessions_unrepairable).

    force=True on import_session bypasses the stat/hash skip gates; sync_session's
    UUID-dedup insert (insert_new_messages) is already idempotent, so re-running
    this on an already-fixed session is a safe no-op that recovers 0 messages.

    After a candidate's files are processed (and none raised), the session's
    classification is re-checked via ingestion_status.reclassify_session:
      - now "ok" -> sessions_repaired
      - still "stale_tail"/"ingestion_gap" -> sessions_unrepairable (the on-disk
        transcript genuinely lacks the expected content; reattempting won't help)
    If any of the candidate's files raised, it counts toward sessions_failed
    instead, and reclassification is skipped for it (a poison-file failure is an
    operational problem, not evidence the source content is missing).
    messages_recovered sums msg_count across every file that did succeed,
    regardless of the candidate's overall outcome.
    """
```

Implementation notes:
- For each candidate, sort `filepaths` via `ccrecall.parsing.sort_session_files` (matches `import_project`'s existing multi-file repair-group handling — a session can span multiple JSONL files, e.g. `agent-*` sidechain files).
- For each file in the sorted list, call `import_conversations.import_session(conn, filepath, project_id, force=True)` inside a per-file `SAVEPOINT` (`conn.execute("SAVEPOINT import_file")` / `RELEASE` / `ROLLBACK TO SAVEPOINT` on exception) — copy the containment shape from `import_project` in `src/ccrecall/hooks/import_conversations.py` (lines ~260-289): `sqlite3.OperationalError` re-raises after rollback, any other `Exception` rolls back, logs via `log.exception`, and continues to the next file/candidate rather than aborting the whole repair batch.

  **Important difference from `import_project`'s copy**: `import_project` wraps its SAVEPOINT loop inside an explicit `BEGIN` (guarded by `if not conn.in_transaction: conn.execute("BEGIN")`, `import_conversations.py:218-219`), which makes its whole per-project loop one atomic unit — an `OperationalError` there rolls back every file imported so far in that project. `repair_sessions()` deliberately does **not** add that guard: `find_repairable_sessions()` is pure-SELECT and the call site (T03) runs after `_run()`'s existing per-project loop has already committed, so each file's `SAVEPOINT`/`RELEASE` here commits independently and durably the moment it completes. This is intentional — a repair batch has no cross-candidate invariant to protect, so per-file durability (keep whatever's already fixed if the run is interrupted, safe to resume by re-running `--repair-gaps`) is the correct behavior, not an accident. Do not add a `BEGIN` guard around the whole candidate loop; write the module docstring/comments to describe per-file durability explicitly, so a future reader doesn't "fix" this by copying the guard too.
- Import `import_conversations` (the module) rather than importing `import_session` directly, to avoid a circular import — `import_conversations.py` will import this new module in T03. If that produces a circular import, restructure so `import_repair.py` takes `import_session` as an injected callable parameter instead; use whichever avoids the cycle, but prefer the direct import first since it's simpler.
- Per candidate: sum `msg_count` across its successfully-processed files into `messages_recovered` (always, regardless of outcome). Track whether any file in the candidate raised. After all of the candidate's files are processed:
  - If any file raised: increment `sessions_failed` once for the candidate (regardless of how many of its files failed) and do **not** call `reclassify_session` for it — a poison-file failure means the repair attempt was incomplete/inconclusive for that session, not that its content was verified missing.
  - If no file raised: call `ingestion_status.reclassify_session(conn, session_uuid, filepaths)`. If it returns `"ok"`, increment `sessions_repaired`. For any other result (`"stale_tail"`, `"ingestion_gap"`, or the rarer `"pending_tail"`/`"missing_source"` — the latter two shouldn't normally occur for a candidate that came from `find_repairable_sessions` and was just successfully force-reimported, but `reclassify_session` can theoretically return them per its own docstring), increment `sessions_unrepairable` rather than crashing on an unrecognized category. `"stale_tail"`/`"ingestion_gap"` is the expected unrepairable case (the transcript was fully force-reimported and still doesn't have what's expected — the gap is in the source data itself, not something a retry will fix); the other two are defensive fallbacks for edge-case races, not expected outcomes.
  - Call `on_reclaim()` after each file (not once per candidate) — matches `import_project`'s actual per-file reclaim granularity (`import_conversations.py:296`), which matters for a multi-file repair-group candidate where memory from parsing several large transcripts back-to-back would otherwise accumulate before any reclaim happens.
- Add a module-level `log = logging.getLogger(LOGGER_NAME)` (this file is imported by `import_conversations.py`, itself imported by the process entry point that already calls `setup_logging()` — see `rules/common/logging.md` rule 1, this is library code within the same process, not a new entry point).
- `on_reclaim`'s default (`_noop`) needs its own trivial definition in this new module — `import_conversations.py`'s existing `_noop` (used for the same purpose on `import_project`) isn't exported and shouldn't be imported across modules for a one-line function. Define `def _noop() -> None: pass` locally in `import_repair.py`, mirroring the existing pattern rather than sharing it.

Create `tests/test_import_repair.py`:
- Build a small fixture DB + JSONL transcript representing a `stale_tail` session (import_log stat matches on-disk file, but a trailing message row is missing from `messages`).
- Call `repair_sessions()` with a candidate for that session; assert the missing message row now exists and the returned counts are `(1, >=1, 0, 0)` — repaired, some messages recovered, zero failed, zero unrepairable.
- Run `repair_sessions()` a second time with the same candidate (now already-fixed, reclassifies as `"ok"`) — assert it returns `(1, 0, 0, 0)`: still counted as `sessions_repaired` (its post-repair classification is `"ok"`), but zero `messages_recovered` this time (nothing left to insert) and no duplicate rows.
- An unrepairable case: build a `stale_tail`/`ingestion_gap` candidate whose transcript genuinely cannot supply the missing message (e.g. the expected UUID simply isn't present anywhere in the JSONL — a realistic case, not a contrived one, since this is exactly what the design's motivating bug looks like from the repair side) — assert `repair_sessions()` returns it under `sessions_unrepairable`, not `sessions_repaired`, and does not raise.
- A poison-file case: one candidate's file raises during `import_session` (e.g. corrupt JSONL) — assert `repair_sessions()` does not raise, logs the failure, still processes any remaining candidates (containment matches `import_project`'s behavior), the poison candidate is counted in `sessions_failed`, and `reclassify_session` is not called for it (mock or spy on `ingestion_status.reclassify_session` to confirm, or assert indirectly via a session whose reclassify would raise/differ if called).
- A multi-file candidate where one file succeeds and a later file raises — assert the candidate counts toward both `messages_recovered` (from the successful file) and `sessions_failed` (from the failing one), and is excluded from both `sessions_repaired` and `sessions_unrepairable`.

## Verify

- [ ] FR#2 (execution half): `repair_sessions()` force-reimports every candidate and returns accurate `(sessions_repaired, messages_recovered, sessions_failed, sessions_unrepairable)` counts. (FR#3 — restricting candidates to `stale_tail`/`ingestion_gap` — is T01's responsibility; this task trusts its input rather than re-filtering.)
- [ ] FR#5 / AC#5 (counting half): poison-file test confirms one bad candidate doesn't abort the batch and is reflected in `sessions_failed`.
- [ ] FR#8 / AC#8: unrepairable-case test confirms a candidate whose source transcript genuinely lacks the expected content is reported under `sessions_unrepairable`, distinct from both `sessions_repaired` and `sessions_failed`.
- [ ] Idempotency test confirms re-running repair on an already-fixed session returns `(1, 0, 0, 0)` (still classified `ok`, nothing new to recover) and inserts no duplicates.
- [ ] `uv run pytest tests/test_import_repair.py -q` passes.
