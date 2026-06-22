# Design: sync_session Split (Part 2 of Issue #20)

**Date:** 2026-06-21
**Status:** archived
**Scope-mode:** hold

> **Scope note (2026-06-21):** This spec originally covered both `migrations.py` and `session_ops.py`. After investigation, the `migrations.py` versioned-DML code was found to be effectively dead for the maintainer's workflow (it only transforms a populated DB opened in-place at `user_version < 6`; this never happens here). Rather than split that code for readability, the decision was to **squash** it to a v6 baseline in a separate follow-up spec (subtract-first). This spec is therefore narrowed to the **`session_ops.py` `sync_session` split only** — the genuinely hot path (runs on every sync and import). The migrations squash is tracked separately.

## Problem

`src/ccrecall/session_ops.py` is essentially one function: `sync_session` (~339 lines, lines 49–387). It runs on **every** sync and every import — the hottest write path in the codebase. Its own docstring enumerates seven distinct responsibilities crammed into one flat body threading a shared `cursor`: import_log dedup, session upsert, message insertion with UUID dedup, branch detection, per-branch metadata + branch_messages diff, aggregated-content assembly, and context-summary + embed-on-write. At ~339 lines it is the second-largest function issue #20 flagged (part 1, PR #21, took the largest — `build_output`). Because it mutates the SQLite file on disk, a careless edit corrupts user data silently rather than producing a wrong chart.

## Goals

- Every function in `session_ops.py` lands under the 50-line guideline, except the top-level orchestrator `sync_session` (pure sequencing + control flow over named helpers) and `sync_branch` if it remains a per-branch orchestrator over named helpers.
- `sync_session` reads as orchestration over named helpers, one per responsibility its docstring already names.
- **No observable behavior change.** For identical inputs and starting DB state, `sync_session`'s return value and every row it writes (to `sessions`, `messages`, `branches`, `branch_messages`, `import_log`) are identical to current behavior.

## Non-Goals

- `migrations.py` — out of scope here; being squashed to a v6 baseline in a separate follow-up spec (see scope note).
- The read/render-path splits deferred to later #20 work: `summarizer.py` (`render_context_summary`), `hooks/memory_context.py` (`main`, `select_sessions`), `hooks/backfill_embeddings.py` (`run`).
- The insights/findings/recommendations triple-representation (a behavior change, not a refactor).
- The read-path helper relocations/renames (`sanitize_fts_term` → `db.py`, `_CONFIG_KEYS` rename).
- No change to dedup logic, embed-on-write ordering, commit ownership, or any row value. Structure only.
- No change to `sync_session`'s public signature — it is imported by `hooks/sync_current.py` and `hooks/import_conversations.py`.

## User Scenarios

### Maintainer (Jessica): sole developer on ccrecall
- **Goal:** adjust how a session imports (e.g., change branch-summary behavior) without reading a 340-line function end-to-end.
- **Context:** debugging a wrong row after a sync, or changing one step of the per-branch write path.

#### Adjust per-branch sync behavior
1. **Locate the per-branch helper.**
   - Sees: a `sync_session` orchestrator that loops branches and calls one focused per-branch helper, which delegates summary/embed to named functions.
   - Decides: edit the one helper that owns that step.
   - Then: dedup, session upsert, and import_log logic are untouched.

## Functional Requirements

- **FR#1** `sync_session(conn, filepath, project_dir, ...)` returns the same integer (new-message count, `0`, or `-1`) and writes the same rows to `sessions`, `messages`, `branches`, `branch_messages`, and `import_log` as the pre-refactor function for identical inputs and starting state.
- **FR#2** The embed-on-write ordering invariant is preserved: vec0 upsert before version-column write, only for active leaves with a successful summary and a queryable vec table.
- **FR#3** Every function in `session_ops.py` is ≤50 lines, except the top-level orchestrator `sync_session` (pure sequencing + control flow) and `sync_branch` if it remains a per-branch orchestrator over named helpers.
- **FR#4** `sync_session` remains importable from `ccrecall.session_ops` with its unchanged signature (including the `_project_id` adapter parameter).

## Edge Cases

- **import_log NULL-hash staleness.** A stored NULL `file_hash` with a provided `file_hash` is treated as stale and re-processed (row updated); an exact non-NULL hash match returns `-1`. The dedup helper must preserve this asymmetry.
- **Empty / branchless / messageless session.** `sync_session` early-returns `0` when parsing yields no entries, no branches, or no messages. These early returns must be preserved at the orchestrator level.
- **Embed/summary failures during sync.** `sync_session` classifies summary write failures three ways (content errors `(ValueError, TypeError, KeyError)` → skip; `sqlite3.Error` → log+skip; embed failure → broad `contextlib.suppress(Exception)`) so one branch's failure never aborts the import. Every `except` boundary and its logging must move intact into whatever helper owns that step.
- **sqlite-vec not loaded.** `vec_writable = branch_vec_queryable(conn)` is probed once before the branch loop; embed-on-write is skipped entirely when false. The probe must stay hoisted (once per call, not per branch).
- **Single-active-branch enforcement.** After upserting an active branch, all other branches in the session are set `is_active = 0`. This must run with the same condition and timing.
- **Type-narrowing assert.** `assert branch_db_id is not None  # noqa: S101` (session_ops.py:279) sits after the INSERT-vs-UPDATE if/else and covers both paths; it must be preserved (at the end of the helper that produces `branch_db_id`, before its return).

## Acceptance Criteria

- **AC#1** A DB-state golden pin for `sync_session` covers any write path the existing tests (`tests/test_session_ops.py`, `tests/test_import_pipeline.py`, `tests/test_sync_hook.py`) leave unasserted — specifically the exact `import_log` row on the NULL-hash-stale update path and the exact `-1` return on an exact non-NULL hash match. Green on current code, kept green after the split. (FR#1)
- **AC#2** A manual or `ruff`-assisted scan confirms no function in `session_ops.py` exceeds the line guideline except the documented orchestrator(s). (FR#3)
- **AC#3** The full test suite passes with zero failures after the refactor, with the existing `sync_session` suites passing **unchanged**. (FR#1, FR#2)
- **AC#4** Importing `sync_session` from `ccrecall.session_ops` succeeds with its unchanged signature. (FR#4)

## Key Constraints

- **Behavior-pin-before-move.** No structural change ships before a characterization test pinning `sync_session`'s DB effect is green on the *current* code. The existing public-level suites are the primary pin; AC#1 adds a DB-state pin for the paths they leave unasserted. This is a `refactoring-discipline.md` requirement.
- **No smuggled behavior changes.** If the split surfaces a latent bug, do not fix it here — note it, file it separately, preserve current behavior including warts. The pins encode current behavior.
- **`sync_session` never commits.** Callers (`sync_current.py`, `import_conversations.py`) own the transaction boundary. Helpers must not introduce or remove a `commit()`, and commit ownership must not move into `sync_session`.
- **Preserve every `except` boundary wholesale.** The three summary/embed failure classifications are load-bearing — each `try/except` moves intact into the helper owning the guarded op; never widened, narrowed, or dropped.
- **Hoisted probes stay hoisted.** `vec_writable` is probed once before the branch loop; keep it once-per-call, passed down as an arg.
- **Cursor/connection threading, not methods.** `sync_session` threads an explicit `sqlite3.Cursor`/`Connection` today; the decomposition keeps that — module-level helpers taking `cursor`/`conn` (and per-branch data) as explicit args. No `SyncSession` class with methods (personal style: functions over methods, no `_`-private-method ceremony).
- **Naming:** newly extracted helpers are public (no `_` prefix), matching the personal convention.

## Dependencies and Assumptions

- No external systems. In-process SQLite plus JSONL file reads.
- `session_ops.py` imports from `ccrecall.db` (`branch_vec_queryable`, `write_branch_embedding`) — it sits *above* `db.py`, so its helpers stay in `session_ops.py`.
- Assumes the existing fixtures (`memory_db`, `make_vec_conn`, the JSONL `fixtures/*.jsonl`) plus the existing `sync_session` suites are sufficient to characterize the function. AC#1 fills the one or two unasserted write paths.

## Architecture

Decompose `sync_session` into module-level helpers, one per responsibility its docstring names, all taking explicit `cursor`/`conn` + data:

- `import_log_skip_check(cursor, filepath, write_import_log, file_hash) -> tuple[row, bool]` — the dedup probe; returns the existing log row and whether to short-circuit to `-1`. Preserves the NULL-hash-stale asymmetry.
- `upsert_session(cursor, session_uuid, project_id, meta) -> int` — the `sessions` INSERT…ON CONFLICT + id fetch.
- `insert_new_messages(cursor, session_id, messages, valid_branch_uuids, existing_uuids) -> int` — the message-insert loop with UUID dedup, notification flagging, tool-result skip, empty-text skip; returns `new_count`.
- `sync_branch(conn, cursor, branch, messages, uuid_to_msg_id, existing_branches, session_id, vec_writable)` — the per-branch loop body (lines 210–366, ~157L). Further delegates:
  - `upsert_branch(...) -> int` — the INSERT-vs-UPDATE + the single-active-branch enforcement → `branch_db_id`. Keeps the `assert branch_db_id is not None` after the if/else, before return.
  - `diff_branch_messages(cursor, branch_db_id, branch_uuids, uuid_to_msg_id)` — the add/remove link diff.
  - `write_branch_summary(cursor, branch_db_id) -> str | None` — `compute_context_summary` + the 3-way exception classification. Returns `summary_md` or None.
  - `embed_branch(cursor, branch_db_id, summary_md, is_active, vec_writable)` — the guarded embed-on-write with the ordering invariant intact.
- `write_import_log(cursor, filepath, session_id, file_hash, log_row)` — the final UPDATE-or-INSERT.
- `sync_session` becomes orchestration: skip check (early `-1`), parse + branch/message early-returns (`0`), project upsert (keep the `_project_id` pre-resolution branch), `upsert_session`, build `valid_branch_uuids` + `existing_uuids`, `insert_new_messages`, build `uuid_to_msg_id`, fetch `existing_branches`, probe `vec_writable` once, loop `sync_branch` over branches, `write_import_log`, return `new_count`.

The cross-helper shared reads (`valid_branch_uuids`, `existing_uuids`, `uuid_to_msg_id`, `vec_writable`, `existing_branches`) stay computed in the orchestrator and pass down as args.

## Replacement Targets

No existing code is being replaced — this is a pure decomposition of one function into helpers within the same module.

## Convention Examples

### Load-bearing exception classification (move wholesale, never reshape)
**Source:** `src/ccrecall/session_ops.py` (summary-write failure handling)
```python
try:
    summary_md, summary_json = compute_context_summary(cursor, branch_db_id)
    cursor.execute("UPDATE branches SET context_summary = ? ... WHERE id = ?", (...))
except (ValueError, TypeError, KeyError):
    summary_md = None            # content error — skip this branch's summary
except sqlite3.Error:
    logging.getLogger(LOGGER_NAME).exception("sync: summary write failed for branch %s", branch_db_id)
    summary_md = None            # infra error — log + skip
```

### Module-level helper threading an explicit cursor (the target shape)
**Source:** `src/ccrecall/token_parser.py` (the part-1 split produced exactly this shape — module functions over an explicit state, no `_`-private methods)
```python
def handle_assistant_line(state, entry):
    ...
```

## Alternatives Considered

- **A `SyncSession` class holding `conn`/`cursor` as state with handler methods.** Rejected — personal style is functions over methods and no `_`-private-method ceremony; the cursor threads cleanly as an explicit arg, and `sync_session` has exactly two call paths, so a class adds state-to-hold (`reader-load.md`) with no payoff.
- **Leave `sync_session` as-is.** Rejected — it is the second-largest function #20 flagged and the hottest write path; every future change to import behavior pays the 340-line reading cost.
- **Bundle with the migrations split (original plan).** Rejected — the migrations code is being squashed (dead for this workflow), not split; bundling a deletion redesign with this hot-path split would couple two unrelated changes (`decomposition-discipline.md`).

## Test Strategy

### Existing Tests to Adapt
- `tests/test_session_ops.py`, `tests/test_import_pipeline.py`, `tests/test_sync_hook.py` — must pass **unchanged**; they pin `sync_session` at the public level. If any needs editing, that signals a behavior change and is a red flag, not a routine adaptation.
- `tests/conftest.py` (`memory_db`, `make_vec_conn`) and the JSONL `fixtures/*.jsonl` — reused as-is.

### New Test Coverage
- **DB-state golden pin for `sync_session`** for the NULL-hash-stale `import_log` update path and the exact-hash `-1` skip (FR#1, AC#1). Committed first, green on current code.

### Tests to Remove
No tests to remove — nothing is deleted from the public surface.

## Documentation Updates
- `CHANGELOG` — add a part-2 entry referencing issue #20 (the `sync_session` split), mirroring the part-1 (PR #21) entry style.
- No README, CLI-help, or rules-file references to `sync_session` exist — confirmed during reconnaissance. No other doc updates required.

## Impact

### Changed Files
- `src/ccrecall/session_ops.py` — modify: split `sync_session` into per-responsibility helpers threading `cursor`/`conn`.
- `tests/test_session_ops.py` — modify: add a DB-state golden pin for the unasserted write paths.

### Behavioral Invariants
- For identical starting DB state and inputs, `sync_session`'s return value and every row it writes are identical to current behavior.
- `sync_session` never commits (callers own the transaction); commit ownership must not move into it.
- The embed-on-write ordering invariant (vec0 upsert before version-column write) holds.
- `sync_session`'s signature (including `_project_id`) is unchanged.

### Blast Radius
- `sync_session` is called by `hooks/sync_current.py:119` and `hooks/import_conversations.py:71`. Behavior-preserving, so consumers are unaffected.
- All within `src/ccrecall/`; no cross-package or external consumers.

## Open Questions

None.
