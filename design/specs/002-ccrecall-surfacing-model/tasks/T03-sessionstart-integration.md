---
task_id: "T03"
title: "Wire proactive alerts into SessionStart (restructure memory_context.main)"
status: "planned"
depends_on: ["T01", "T02"]
implements: ["FR#6", "FR#12", "FR#13", "FR#16", "AC#1", "AC#2", "AC#3", "AC#5", "AC#7", "AC#9", "AC#10"]
---

## Summary
Wire the proactive surfacing into the only user channel: the SessionStart hook. Restructure `memory_context.main` so it evaluates both proactive alert classes (active writability probe + reading the embedding-status sidecar), and injects a single combined `## ⚠` block ahead of the prior-session context — even when there are no prior sessions and even when the DB connection itself fails. This is the integration that makes T01's mechanism and T02's recording actually reach the user.

## Target Files
- modify: `src/ccrecall/hooks/memory_context.py`
- modify: `tests/test_context_injection.py`
- read: `src/ccrecall/health.py`
- read: `src/ccrecall/hooks/onboarding.py`
- read: `tests/test_onboarding.py`

## Prompt
Restructure `src/ccrecall/hooks/memory_context.py` `main()` per design `## Architecture` (Tier 3, "Injection integration ... requires restructuring, not just a prepend") and `## Edge Cases`.

Today `main()` returns early via `if not sessions: _emit_empty(); return` and wraps the DB path in `except Exception: _emit_empty()` — both short-circuit before any alert could be built. Change the flow so proactive evaluation happens **earlier and independent of the DB path**:

1. Run the **filesystem writability probe** (T01) unconditionally and early — it needs no DB and is cheap.
2. Read the **embedding-status sidecar** (T01) — a plain file read; do NOT load vec/fastembed/onnxruntime (FR#6 / AC#10 / hot-path invariant).
3. Attempt the DB connection and the **DB write-lock probe** (T01) inside their own guard, so a connection/probe failure becomes a "cannot persist data" alert rather than an empty emit. Pass the open connection to the DB probe; if the connection couldn't be opened, the probe is invoked with `conn=None` and reports the fault.
4. Compute the active alert keys, pass them through the T01 snooze ledger (fire vs suppress, update `last_fired`, auto-clear), and build ONE combined block for whatever fires (FR#13).
5. Assemble final output as `directive + proactive + origin + pending + context`, emitting the proactive block **even when `sessions` is empty or the DB is inaccessible** (origin/pending/context simply absent in that case). The proactive block leads (FR#12) — highest-attention position.

Wrap the entire proactive path defensively (FR#16): any exception in probing / sidecar read / snooze / block build must degrade to "no proactive block" and must never break the hook or drop the existing context injection — follow the `_pending_question_block` precedent (broad except, best-effort log, return ""). Hook stdout still emits only the JSON envelope (`hookSpecificOutput.additionalContext`).

Onboarding interplay (design Edge Cases): the writability alert should still surface when onboarding is incomplete (it explains why config can't be saved). Confirm against `onboarding.py` that this doesn't double-inject with the onboarding notice in a way that violates "one message at a time" — if both would fire, prefer surfacing the write-failure (it's the cause). The design does not mandate this precedence, so leave a brief inline comment at that branch explaining the choice, so a future reader doesn't read it as a bug. Check `tests/test_onboarding.py` for assumptions your restructuring could break.

Update `tests/test_context_injection.py`: make ordering assertions tolerate/assert a leading proactive block; add cases for (a) dir unwritable → block injected even with no sessions, (b) DB read-only → block injected, with a concurrent lock holder NOT producing a false alert, (c) embedding-status sidecar present → "embeddings failing" block named, (d) both alerts active → single combined block, (e) forced exception in the proactive path → normal context still injected, (f) an import-level assertion that the hook module path does not import vec/fastembed. Run `uv run pytest tests/test_context_injection.py tests/test_onboarding.py` and confirm green.

## Focus
- Current `main()` structure to rework: `cwd/session_id` guards, `load_config()` onboarding gate, `auto_inject_context` gate, `get_db_path`/`get_db_connection`, `select_sessions`, then `directive + origin + pending + context` assembly (see `memory_context.py:433-535`). The proactive evaluation must straddle these so it survives the early returns.
- `get_db_connection(settings)` can itself raise when the dir/WAL is unwritable — that raise is the signal for the DB-probe `conn=None` fault path. Catch it; don't let it hit the outer `_emit_empty()` before the alert is built.
- The block builder and snooze logic live in `health.py` (T01) — this task only orchestrates; do not re-implement probe/snooze logic here.
- `_pending_question_block` (memory_context.py:66-89) is the exact defensive-wrap template; the proactive block evaluation should be structured the same way.
- AC#3 spans T02 (record) + this task (read & inject) — verify the end-to-end here: status recorded by the embedding process → block injected next session.
- AC#10 is a hard invariant: assert via import inspection that evaluating the embedding alert does not import `fastembed`/`onnxruntime`/`sqlite_vec` on the hook path.

## Verify
- [ ] FR#6: with `embedding-status.json` present, SessionStart injects an "embeddings failing" block naming the reason, without importing vec/fastembed.
- [ ] FR#12: when a proactive alert fires, its block appears ahead of origin/pending/context in the injected output.
- [ ] FR#13: when both alert classes are active, exactly one combined block is injected (not two).
- [ ] FR#16: a forced exception in the proactive path leaves the normal context injection intact and never breaks the hook.
- [ ] AC#1: dir made unwritable → single `## ⚠` "cannot persist data" block injected ahead of any context (even with no prior sessions).
- [ ] AC#2: DB read-only → block injected; a concurrent lock holder alone does not produce a false alert.
- [ ] AC#3: status recorded by the embedding process (T02) → next session injects the "embeddings failing" block.
- [ ] AC#5: end-to-end — after the condition is fixed (embedding-status cleared / probe passes) the next session injects no proactive block and the snooze record is gone.
- [ ] AC#7: both alerts active → one combined block.
- [ ] AC#9: forced exception in probe/sidecar-read path → session start still functions, no injected alert (recall half verified in T04).
- [ ] AC#10: import inspection proves the SessionStart hook path does not load fastembed/onnxruntime/sqlite-vec to evaluate the embedding alert.
