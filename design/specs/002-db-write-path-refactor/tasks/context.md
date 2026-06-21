# Context: sync_session Split (Part 2 of Issue #20)

## Problem & Motivation
`src/ccrecall/session_ops.py` is essentially one function: `sync_session` (~339L, lines 49–387), the hottest write path in ccrecall — it runs on every sync and every import. Its docstring names seven responsibilities crammed into one flat body threading a shared `cursor`: import_log dedup, session upsert, message insertion with UUID dedup, branch detection, per-branch metadata + branch_messages diff, aggregated-content assembly, and context-summary + embed-on-write. Because it mutates the SQLite file on disk, a careless edit corrupts user data silently. This spec splits it into per-responsibility helpers, behavior-preserving. (The originally-bundled `migrations.py` work was found to be dead code for this workflow and is being squashed separately — see design.md scope note.)

## Visual Artifacts
None.

## Key Decisions
1. **Behavior-preserving only.** For identical inputs and starting DB state, `sync_session`'s return value and every row it writes (`sessions`, `messages`, `branches`, `branch_messages`, `import_log`) are byte-identical to current behavior. No observable change.
2. **Pin before move.** The existing public-level suites (`test_session_ops.py`, `test_import_pipeline.py`, `test_sync_hook.py`) are the primary pin; T01 adds a DB-state pin for the write paths they leave unasserted, green on current code, before T02 splits.
3. **Functions over methods.** `sync_session` threads an explicit `sqlite3.Cursor`/`Connection` today; the split keeps that — module-level helpers taking `cursor`/`conn` + data as explicit args. No `SyncSession` class.
4. **Decoupled from migrations.** This is `session_ops.py` only. `migrations.py` is untouched (being squashed to a v6 baseline in a separate follow-up spec).

## Constraints & Anti-Patterns
- **`sync_session` never commits** — `sync_current.py` and `import_conversations.py` own the transaction boundary. Moving a commit into any helper, or into `sync_session`, is a behavior change.
- **Hoist the vec probe once.** `vec_writable = branch_vec_queryable(conn)` is computed before the branch loop to avoid paying `embed_text` inference per inactive leaf; keep it once-per-call, passed into `sync_branch`/`embed_branch`.
- **Embed-on-write order is load-bearing.** vec0 upsert FIRST, version columns LAST, only for active leaves with a successful summary and a queryable vec table — so version columns stay at 0 if the upsert is swallowed (branch stays eligible for backfill).
- **The three `except` boundaries** in the summary write are intentionally distinct (content skip vs infra log+skip vs embed broad-suppress). Move each into the helper owning its guarded op; do not merge or widen them.
- **Preserve early-return guards and the type-narrowing assert** (`assert branch_db_id is not None  # noqa: S101`, session_ops.py:279, which sits **after** the if/else and covers both paths).
- **No smuggled behavior changes.** If a split surfaces a latent bug, note + file separately; preserve current behavior including warts.
- **Naming:** newly extracted helpers are public (no `_` prefix), matching the personal convention.
- **Public import surface unchanged:** `sync_session` (signature incl. `_project_id`) — imported by `hooks/sync_current.py:119` and `hooks/import_conversations.py:71`.
- **Out of scope (do NOT touch):** `migrations.py` (separate squash spec); read/render-path splits (`summarizer.py`, `hooks/memory_context.py`, `hooks/backfill_embeddings.py`); the insights triple-representation.

## Design Doc References
- `## Architecture` — the concrete per-responsibility helper decomposition for `sync_session`.
- `## Edge Cases` — the guard/except/probe/assert behaviors that must survive the split.
- `## Key Constraints` — pin-before-move, no-commit, except/probe preservation, functions-over-methods.
- `## Impact → Behavioral Invariants` — what must stay byte-identical.

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

### Module-level helper threading explicit state (the target shape, from part 1)
**Source:** `src/ccrecall/token_parser.py` — the part-1 split produced module functions over an explicit state object, no `_`-private methods. `sync_session`'s helpers follow the same shape.
