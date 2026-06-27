---
task_id: "T02"
title: "Record/clear embedding-capability failures in the detached embedding process"
status: "planned"
depends_on: ["T01"]
implements: ["FR#4", "FR#5"]
---

## Summary
Make the detached embedding process the authoritative detector of embedding-pipeline failure. At the points where embedding cannot proceed for a structural reason — sqlite-vec unavailable or the fastembed model unavailable — record an embedding-capability failure (with a machine-readable reason) to the `embedding-status.json` sidecar via the T01 helpers. On a successful embedding run, clear the status. This is the *passive* half of the proactive embedding alert; the SessionStart hook (T03) only reads what this task writes, keeping vec/fastembed off the hook hot path.

## Target Files
- modify: `src/ccrecall/hooks/backfill_embeddings.py`
- modify: `src/ccrecall/hooks/sync_current.py`
- modify: `tests/test_backfill_embeddings.py`
- modify: `tests/test_sync_hook.py`
- read: `src/ccrecall/health.py`

## Prompt
Using the helpers added in T01 (`record_embedding_failure(reason)`, `clear_embedding_failure()` in `src/ccrecall/health.py`), instrument the embedding process per design `## Architecture` (Tier 3, embedding-failure detection) and `## Edge Cases`.

In `src/ccrecall/hooks/backfill_embeddings.py`, instrument ONLY the capability-failure points inside the embedding pipeline function `run()` (def at line 260) — currently each only `logger.error` + return `EXIT_ABORT`:
- `model_available(...)` is False (model unavailable / download blocked) — line 294.
- `chunk_vec_queryable(conn)` is False (sqlite-vec unavailable) — line 318.
At each, call `record_embedding_failure(reason=...)` with a distinct reason string (e.g. `"vec_unavailable"`, `"model_unavailable"`) before returning `EXIT_ABORT`. On the **success path** — when a `run()` completes having embedded rows without a capability abort — call `clear_embedding_failure()`.

**Do NOT instrument `run_status()` (def at line 218) — its `chunk_vec_queryable` check at line 233 is the read-only `--status` diagnostic, not the embedding pipeline.** Recording there would fire a false "embeddings failing" alert every time the user runs `ccrecall backfill embeddings --status` on a vec-unavailable machine, violating the design Edge Case "must key off capability failure recorded by the embedding process" — a status probe is not the embedding process.

In `src/ccrecall/hooks/sync_current.py`, find the embedding step on the current-session sync path (it embeds the just-synced exchanges). Apply the same rule: on a structural capability failure, `record_embedding_failure(...)`; on a clean embedding pass, `clear_embedding_failure()`. Do not record for ordinary per-row CONTENT_ERROR (`-1`) outcomes — those are self-healing degradation of a single branch, not a pipeline-capability failure (design Non-goals + Edge Cases: "coverage mid-backfill must not trip the alert").

Keep changes minimal and additive — do NOT alter the existing watermark/heal/eligibility logic, the `-1` sentinel handling, or the no-progress loop-breaker (Behavioral Invariants). Recording must be best-effort: a failure to write the sidecar must not change the embedding process's own exit behavior (wrap defensively).

Update `tests/test_backfill_embeddings.py` and `tests/test_sync_hook.py` to assert: when vec/model is forced unavailable, the run records the expected reason to the sidecar; when a run embeds successfully, it clears the status. Monkeypatch the sidecar path to `tmp_path`. Run `uv run pytest tests/test_backfill_embeddings.py tests/test_sync_hook.py` and confirm green.

## Focus
- Reasons must be machine-readable and stable — T03's block builder maps them to user-facing cause prose. Agree on the exact strings with T01's constants (prefer constants in `health.py` over bare literals).
- The success-path clear is as important as the failure record (FR#5 / AC#5): without it, a transient failure would keep alerting after the user fixed it. Make sure the clear fires on the *normal completion* path, not only on an explicit "all good" branch.
- `sync_current.py` runs detached and already has defensive top-level handling (`except Exception: ... sys.exit(0)`); your recording calls must sit inside that safety and not introduce a new uncaught path.
- Both files already import from `ccrecall.db`; add the `ccrecall.health` import at top (no lazy imports).
- Blast radius: these are the embedding write paths — a regression here perturbs the watermark/heal logic the whole system relies on. Touch only the abort/success seams.

## Verify
- [ ] FR#4: forcing sqlite-vec unavailable (and separately, model unavailable) in a backfill/sync run writes `embedding-status.json` with the corresponding reason (asserted in the two test files).
- [ ] FR#5: a successful embedding run clears `embedding-status.json` (asserted in the two test files), and the existing watermark/heal/eligibility tests still pass.
