---
name: ccr-resume
description: "Use when picking up a fresh session after /clear, a stop, or an unanswered AskUserQuestion — reconstructs the prior session's intent from its transcript tail and surfaces any unresolved decision. User-invoked only (never auto-fired); for a hand-written end-of-day handoff use /mine-good-morning instead."
user-invocable: true
disable-model-invocation: true
---

# Resume

Pick up work in a fresh session after the previous one ended — via `/clear` (to avoid resending a large uncached context), after being stopped, or after a question the prior session left open (a rejected `AskUserQuestion` *or* one asked in prose). `/clear` starts a **new session with a new transcript file**; the prior session's transcript stays on disk. This skill reads its **tail** to recover what the disk can't tell you: the user's last instruction and any decision that was never resolved.

## Why this exists — the failure mode it prevents

On pickup, the instinct is to read on-disk artifacts (git state, task files, design docs) and infer "what's left," then act. That misses the one thing artifacts never record: **the pending decision**. A real case — an orchestration finished, asked a ship-or-not question, the user rejected the tool call, and the next session shipped a PR from the artifacts without ever seeing that the question was still open. Disk state said "done"; the transcript said "waiting on the user." **Read the tail before you touch anything.**

## How this differs from neighbors

- `mine-good-morning` reads a hand-written end-of-day handoff. This needs no handoff — it reconstructs from the transcript automatically.
- `mine-status` reports branch/tasks/last-commit. This recovers *intent and pending decisions*, not just current state.

## Arguments

$ARGUMENTS — optional. A natural-language directive for how to follow up on the prior session — e.g. "keep going with the refactor but skip the test rewrite", "just summarize where we left off", "did the migration finish?". This is *intent*, not a session selector: it does not get passed to `ccrecall tail`; it shapes how you proceed in Phase 3. Omit it to get a plain orientation and a "how do you want to proceed?" prompt.

To target a session other than the auto-picked prior one (rare), run `ccrecall tail --list`, then `ccrecall tail <id-substring>` directly.

---

## Phase 1: Recover the transcript tail (do this first, always)

Run the lever — it auto-picks the prior session, locates its JSONL, and prints the tail, the last typed instruction, the last assistant message, and any **unanswered** `AskUserQuestion`:

```bash
ccrecall tail
```

Do **not** pass $ARGUMENTS here — it's a follow-up directive, not a session id (see Arguments).

- Auto-detect picks the second-newest session (the newest is *this* one, live). If the wrong session comes up or you need to choose, run `ccrecall tail --list` and re-run with the right id substring.
- If `ccrecall tail` finds nothing (no project dir, only the current session, or a moved cwd), fall back to `/ccr-recall` to retrieve the tail — never substitute disk artifacts for the transcript.
- A clear/startup may already have surfaced an "Unresolved Decision From Prior Session" block at the top of context — if so, this confirms and expands it; reconcile and proceed to Phase 2.

Read the output fully before doing anything else.

## Phase 2: Reconcile intent against disk state

Now — and only now — check the on-disk reality and line it up with what the transcript says was intended:

- `git status` / `git -C <root> log --oneline -5` — what actually landed vs. what the tail says was in flight.
- Task/spec files, background-job notifications in the tail (a job may have finished after the session ended).

Name any mismatch between "what the transcript wanted" and "what the disk shows." Do not paper over it.

## Phase 3: Surface — never auto-resolve

The prior session may have ended on an **open decision** it was waiting on the user for. It takes two forms — treat them identically:

1. **Structured** — `ccrecall tail` printed a PENDING QUESTION block (an `AskUserQuestion` the user never answered). The lever detects this for you.
2. **Prose** — no PENDING QUESTION block, but the LAST ASSISTANT MESSAGE excerpt ends by asking the user something or offering a choice ("Want me to also update the tests?", "Should I do X or Y?"). The harness records this as ordinary text, so the lever *cannot* flag it — you have to read the excerpt and judge whether it left a question hanging.

**If either form is present, surface it — do not resolve it:**
- Structured: re-present with `AskUserQuestion`, reusing the exact option labels and descriptions from the tail block (the picker appends "Other" automatically).
- Prose: pose the question the assistant left open — as `AskUserQuestion` if it maps to clear choices, otherwise as a plain question.

Then **stop** — do not pick an option, and do not act on the work the decision gates.

**If there is no open decision:** give a 3–5 line orientation — where things stand, the last instruction, what's reconciled vs. mismatched.

**Folding in the $ARGUMENTS directive (if given):** it is the user's answer to "how do we proceed" — but it does *not* override an open decision the prior session was waiting on *from them*. So:
- Open decision present, and the directive plainly answers it → confirm that reading with the user, then proceed. Otherwise surface the decision first; the directive resolves what comes after.
- No open decision → act on the directive directly instead of asking how to proceed.
- No directive → ask how the user wants to proceed before taking any action.

## The one rule

An unanswered, rejected, or interrupted question — structured (`AskUserQuestion`) *or* prose left hanging in the last assistant message — is an open decision, not an invitation to choose. Surface it; let the user decide. A $ARGUMENTS directive answers "how do we proceed," never a decision the prior session was still waiting on.
