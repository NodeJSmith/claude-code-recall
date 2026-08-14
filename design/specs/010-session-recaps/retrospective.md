# Retrospective: Session Recaps (attempt 1, abandoned)

**Status:** abandoned, not merged
**Date:** 2026-08-14
**Artifact:** PR #118, branch `114-115` (43 commits, ~100 review rounds)
**Companion:** `design.md` in this directory is the design this was built to. It is still substantially correct — read it first.

## What happened

The feature was built, reviewed to exhaustion, and stopped short of merge. Nothing shipped: `main` was at `SCHEMA_VERSION = 7` with no `session_recap` tables, so the v8/v9 recap schema never reached a released version and no user database carries it. The cost of stopping was sunk effort only.

It was not stopped because the design was wrong or because the remaining bugs were individually hard. It was stopped because the **rate of defect discovery stopped converging**, and the reason turned out to be structural rather than incidental.

## Why it was stopped

The evidence, in the order it accumulated:

- The session before the decision closed 19 defects. **Five of them were introduced by fixes made in that same session.** None of the five was caught by the test suite; all came from reviewers.
- A final review round produced 57 unresolved threads. Triaging them against the code found four real defects. One of them meant **the drainer could not open its own database on any installation that had ever run embeddings** — that is, the default configuration. The live log carried **69 instances** of that traceback.
- The full test suite (1,317 tests, all green) never noticed. Every test builds its database through a fixture that loads sqlite-vec, so no test ever saw the shape a real install has.
- Fixing that round produced two further P1-class findings from reviewers, both against the recovery path. Both were pre-existing rather than regressions, but the second one was the **third instance of the same defect** found in a single afternoon.

That third instance is the one that ended it. The same fact was being destroyed by three different writers, and each fix taught one more writer not to destroy it. That is not a shrinking list of bugs. It is one fault with an unknown number of surface expressions.

## The thesis: what actually went wrong

**One mutable column carried two independent facts, and the invariant tying them together was maintained by convention across a dozen writers.**

`session_recap_attempts.cleanup_state` answered both:

- *Did this attempt ever spawn a provider process?* — monotonic. Once true, true forever.
- *What phase is packet and process cleanup in?* — mutable, overwritten repeatedly.

Every writer updating the second silently destroyed the first. Recovery then read the **absence** of a spawn marker as positive proof that no process was running, deleted the packet, declared the attempt clean, and allowed the job to be requeued beside a process that may still have been alive.

The three instances found were:

1. `_recover_expired_claims` correctly refused to prove cleanup for an unidentified attempt, then overwrote the field recording why.
2. `acknowledge_cleanup` flattened the same marker on the direct post-spawn failure path.
3. `_recover_cleanup_failed_attempts` read the resulting absence as proof the process group was gone.

Every other recurring bug in this subsystem has the same shape — two code paths that must agree about one fact, where nothing forces agreement:

- journal replay honoured the `EXCLUDED_PROJECT` sentinel; the claimed-job path discarded it, so a project excluded after import still had its contents sent to the provider
- `write_packet` failure stopped the drain when quarantine was the refusing condition, but not when the disk was full
- the spawn-intent marker was written, and its return value ignored
- `STATUS_CLEANUP_FAILED` returned success to the drain loop
- `upsert_job` and `recap_state_changed_input` each hand-write a "resettable" predicate, and **they already disagree** about when to bump `claim_token`
- `status.py` hardcodes a retry-reason list independent of `recap_state.CONTENT_DEPENDENT_BLOCK_REASONS`
- `reason='platform_unsupported'` is written by three call sites under two different fencing disciplines, two of them unfenced

## What to keep

The requirements were the expensive part of this work, and they are correct. Attempt 2 should start from them, not re-derive them.

- **The fencing model.** `claim_token` + `retry_lineage` + `active_attempt_id`, with every mutating statement carrying the fence in its `WHERE` clause so a stale writer silently no-ops. This is sound and was not the source of any defect.
- **Cleanup proof as a correctness property.** Requiring positive proof that a provider process group is gone before starting another, and stopping the drain when that proof cannot be obtained. Right call, and the reason the defects above were dangerous rather than cosmetic.
- **Packet and quarantine protocol.** Content-free metadata, owner-bound packet paths keyed by a nonce, quarantine rows as the record of packets awaiting cleanup.
- **Durable intent at SessionEnd.** The journal marker plus replay, so intent survives a hook that cannot wait for sync or the provider.
- **DB-only recap authority.** Building packets from normalized imported SQLite rather than a second transcript parse. `evaluation.md` in this directory carries the evidence for that choice; it still stands.
- **Off by default.** `llm_summaries_enabled` defaulting to false is why abandoning this cost nothing.
- **The clear-first/set-last watermark protocol** for embedding state, which came out of this line of work and is independently correct.

## What attempt 2 must do differently

**Put the facts in the schema on day one.** All of the following are cheap at the start and expensive to retrofit, which is precisely why they never got done here:

1. **A separate, write-once `spawned_at` column.** Not a phase value that a later writer can overwrite. The rule becomes: *only positive proof clears an attempt; absence of evidence never does.* The owner-identity triple (`owner_pid`, `process_group_id`, `process_started_at`) already has this property — it is written once by `record_attempt_launch` and never nulled — which is why identity-present cases were never buggy. Extend that property to cover the window between the spawn and the launch record.
2. **`CHECK` constraints on every state-like column.** `session_recap_attempts.state` had one and produced no vocabulary bugs. `cleanup_state` and `reason` did not and produced several. Same table, same session, different outcomes.
3. **One choke-point function per invariant, not a convention.** Where two code paths must agree, they must call the same function. Where a value must be spelled the same in two places, it must be one constant. This sounds obvious and was violated at least six times.
4. **A test that runs the real thing.** No test in the suite exercised SessionEnd through drain through provider through materialize. `record_attempt_launch` had no real-database coverage at all. A single end-to-end test against a database built the way a real install builds it would have caught the defect that ended this attempt.
5. **Operational logging from the start.** The only reason the fatal defect was found is that a detached process happened to write tracebacks to a log file. See issue #123.

## Traps

Things that looked fine and were not:

- **Test fixtures that never resemble a real install.** The sqlite-vec fixture masked a total feature failure across 1,317 passing tests.
- **Tests that pin a value instead of a property.** At least one assertion in this branch passed *while the code was wrong*, because it pinned the exact string the buggy implementation produced. Converting it to a property assertion turned it into a real regression test. Grep for equality assertions on state strings and ask whether the property or the literal is what matters.
- **Reviewer volume as a substitute for structure.** Roughly 100 review rounds found real bugs, repeatedly, and never made the code safe. Review caught instances; only structure kills classes.
- **A long-lived branch.** 43 commits and 90 review threads created real pressure to merge that had nothing to do with whether the code was ready. `gh-pr-threads` itself broke on this PR under the volume (Claudefiles #510).

## Where the material is

- `design.md` — the intended design. Still the starting point.
- `evaluation.md` — the model and data-source evaluation behind the DB-only decision.
- `eligibility-audit.md` — the meaningful-session eligibility work.
- PR #118 and branch `114-115` — the full implementation and its review history, preserved unmerged.
- Issues #120, #124, #125, #126 — catalogued follow-ups from the final review round, most of which describe traps rather than tasks now.
- Issue #121 — migration tests never exercise vec0 objects. This is the gap that hid the fatal defect.
- Issue #123 — the subsystem had effectively no operational logging.
