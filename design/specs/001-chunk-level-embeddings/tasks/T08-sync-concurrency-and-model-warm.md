---
task_id: "T08"
title: "Guard sync-current concurrency and warm the model cache on setup"
status: "planned"
depends_on: []
implements: ["FR#16"]
---

## Summary

Harden the detached embedding path against the two machine-level hazards the design calls out: add a
**lock-file concurrency guard** to `sync-current` so two rapid Stops can't spawn parallel CPU-bound
inference processes (the orphan-swarm the reaper units fight), and **warm the fastembed model cache**
during setup/onboarding so the detached `sync-current` never triggers an invisible multi-minute
~120 MB download on first install. Independent of the chunk substrate (touches only hooks), so it can
land any time.

## Target Files

- modify: `src/ccrecall/hooks/sync_current.py`
- modify: `src/ccrecall/hooks/memory_sync.py`
- modify: `src/ccrecall/hooks/memory_setup.py`
- modify: `src/ccrecall/hooks/onboarding.py`
- modify: `tests/test_sync_hook.py`
- read: `src/ccrecall/db.py`
- read: `src/ccrecall/embeddings.py`
- read: `design/specs/001-chunk-level-embeddings/design.md`
- read: `design/specs/001-chunk-level-embeddings/tasks/context.md`

## Prompt

Implement per design.md `## Key Constraints` ("`sync-current` needs a concurrency guard", challenge
C2) and `## Dependencies and Assumptions` (the first-install model-download caveat, challenge M22).

1. **`sync-current` concurrency guard (FR#16).** `memory_sync.py:39` spawns a detached
   `sync-current` on **every** Stop with no guard (the backfill has `PID_KEY`; sync-current has
   nothing). Add a lock-file guard at `sync_current.run()` startup: if a prior `sync-current` is
   running, **exit 0 immediately and skip this sync** (recovered on the next Stop). **Skip, not
   queue** — guards must never themselves accumulate processes. Reuse the existing PID-file helpers
   (`pid_file_path`/`remove_pid_file` in `db.py`) and the atomic `O_CREAT | O_EXCL` + liveness-check
   pattern from `memory_setup._spawn_background` (`:50-93`): create the lock atomically; if it exists
   and the owning PID is alive, skip; if stale (dead PID), reap and proceed. Define a `PID_KEY`
   (e.g. `"ccrecall-sync-current"`) in `sync_current.py` and clean it up on exit (best-effort,
   including the error paths — `run()` already prints `{"continue": true}` and must keep doing so).
   - Keep the hook stdout contract intact: `sync-current`'s wrapper still prints exactly
     `{"continue": true}` / the existing skip output. The guard's skip path prints
     `{"continue": true}` and returns.

2. **Warm the model cache on setup/onboarding (M22).** The detached `sync-current` calls
   `embed_text` → `get_model()`, which on a cold cache downloads ~120 MB synchronously — an invisible
   multi-minute hang (logging off by default). Warm the cache when the user completes setup so the
   detached path never downloads:
   - Trigger a model warm (`model_available()` / `get_model()`) **off the hot path** — do NOT run the
     download synchronously inside the SessionStart hook (`memory_setup.main`) or the onboarding
     hook's stdout path. Spawn it as a detached background step (reuse `_spawn_background`) at the
     point setup completes / onboarding is enabled, or via a dedicated tiny entry the
     onboarding/`write-config` completion can invoke. Pick the placement that keeps SessionStart
     non-blocking and runs at most once (PID-guarded like the other background spawns).
   - **Emit a logged warning** (regardless of `logging_enabled` — use a direct best-effort log, since
     the detached context has logging off by default) if a model download is ever triggered from a
     **detached** context, so the invisible-hang case becomes observable. Add this where `get_model`
     is reached from the detached `sync-current`/backfill path (e.g. detect cold-cache-on-detached
     and log before/around the construct).
   - This is pre-existing behavior the chunk change inherits, not creates — keep the change minimal
     and do not couple it to the chunk schema.

3. **Tests (`tests/test_sync_hook.py`)** — add:
   - **Concurrency guard (AC#13):** a second `sync-current` invoked while one holds the lock exits
     without embedding (with a fake/held lock file); the first completes normally; a stale lock (dead
     PID) is reaped and the new run proceeds. Assert the stdout stays `{"continue": true}`.
   - Model-warm: setup/onboarding completion triggers the warm spawn at most once (PID-guarded) and
     does not block SessionStart; the detached-download warning path logs when forced.

## Focus

- `memory_sync.py` is the Stop hook that spawns the detached `sync-current` (`:38-42`); it must stay
  non-blocking and keep printing `{"continue": true}` (`:53`). The guard lives in `sync_current.run`
  (the spawned process), NOT in `memory_sync` — `memory_sync` should keep spawning unconditionally;
  the spawned process self-skips if another is running (simplest, matches "skip not queue").
- `_spawn_background` (`memory_setup.py:50-93`) is the reference atomic-PID-guard + detached-spawn
  pattern; `pid_file_path`/`remove_pid_file` are in `db.py:77-85`; the backfill's `PID_KEY` pattern
  is `backfill_embeddings.py:57-62`.
- `get_model` (`embeddings.py:46-67`) is the singleton constructor that downloads on first call;
  `model_available` (`:70-84`) warms it and never raises. The "detached" warning should fire when
  the construct happens in a non-interactive spawned process — thread a flag or detect via the entry
  point, keeping it best-effort.
- Hooks must remain direct console-script entry points (no heavy new top-level imports that slow the
  ~440 ms hook import — a behavioral invariant). The model warm is spawned as a separate process, so
  it does not add import weight to the hooks themselves.
- This task has no dependency on the chunk schema; order it independently.

## Verify

- [ ] FR#16: A second `sync-current` invocation while one is running skips (exits 0 without
      embedding) rather than running a parallel embed; the lock is reaped if stale (AC#13).
- [ ] AC#13: A second `sync-current` started while one is running exits without embedding; the first
      completes normally — verified in `tests/test_sync_hook.py` with a held/fake lock file.
