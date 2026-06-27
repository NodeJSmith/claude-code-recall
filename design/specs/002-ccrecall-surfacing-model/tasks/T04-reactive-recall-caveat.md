---
task_id: "T04"
title: "Append reactive coverage/availability caveat to recall results"
status: "done"
depends_on: ["T01"]
implements: ["FR#15", "FR#16", "AC#8", "AC#9"]
---

## Summary
Tier 2 of the model: surface embedding gaps *reactively*, at the point of relevance — inside a recall the user actually runs — instead of nagging proactively. After the recall/search path assembles its results, append a one-line caveat when embeddings are unavailable (results degraded to keyword) or when branch-grain coverage is below the threshold constant. No new persisted state; independent of the proactive tier.

## Target Files
- modify: `src/ccrecall/search_conversations.py`
- modify: `src/ccrecall/cli/commands.py`
- modify: `tests/test_search.py`
- read: `src/ccrecall/health.py`
- read: `src/ccrecall/db.py`

## Prompt
Per design `## Architecture` (Tier 2) and FR#15: in the recall/search path, after results are assembled, compute embedding state at query time and append a single caveat line when warranted.

- The recall path already loads vec for search; reuse `chunk_vec_queryable(conn)` and `db.branch_embedding_coverage(conn)` (returns `(embedded, total)`) from `src/ccrecall/db.py` — do not re-probe capability separately. **Use `chunk_vec_queryable`, NOT `vec_available`**: `vec_available` re-enables and re-loads the extension (side effects) and would behave unpredictably on the already-vec-loaded search connection; `chunk_vec_queryable` is the table-probe the search path already uses (`search_conversations.py:528,629,740`).
- Append the caveat when: `chunk_vec_queryable(conn)` is False (semantic search degraded to keyword), OR `embedded / total < RECALL_CAVEAT_COVERAGE_THRESHOLD` (the `0.95` constant defined in `health.py`, T01). Guard against `total == 0`.
- The search path **already** prints `"search: vector index unavailable, using keyword search"` to stderr at `search_conversations.py:587`. Do not add a second stderr line for the same condition — consolidate: the results-level caveat (and the `--json` field) is the single user-facing signal; keep or remove the existing stderr print so the user sees one consistent message, not two. Decide at the single rendering boundary.
- At/above the threshold on an embeddings-available install, append nothing (AC#8).
- The caveat is one concise line telling the user results may be partial and why (e.g. "embeddings unavailable — keyword-only results" or "N% of history embedded; results may be partial"). Match the surrounding CLI output style in `search_conversations.py` / `cli/commands.py`. Respect the global `--json` mode: in JSON output, surface the caveat as a field rather than appending prose to stdout.
- Wrap the caveat computation defensively (FR#16 spirit / AC#9): a failure computing coverage must not break the recall — degrade to no caveat.

Determine the exact insertion point by reading how the search command renders results in `search_conversations.py` and how `cli/commands.py` invokes it. Add the caveat at the single rendering boundary so both the human and `--json` paths are covered consistently.

Update `tests/test_search.py`: caveat present when vec unavailable; caveat present when coverage below threshold; caveat absent at/above threshold on a healthy install; `total == 0` does not crash; JSON mode carries the caveat as a field, not appended prose. Run `uv run pytest tests/test_search.py` and confirm green.

## Focus
- `branch_embedding_coverage` was added by the item-1 work (PR #43) and already backs `ccrecall stats` / `--status` — reuse it; do not compute coverage a new way (single source of truth).
- The threshold constant lives in `health.py` (T01) — import it; don't hard-code `0.95` at the call site.
- The `--json` global flag is carried by the frozen `CLIContext` (see CLAUDE.md `cli/` notes and `tests/test_cli_context.py`); the caveat must not corrupt JSON output — add a field, never append a prose line to a JSON document.
- This is the only user-visible change to recall output — keep it to a single line / single field; do not restructure existing result rendering.

## Verify
- [ ] FR#15: recall appends a one-line caveat when embeddings are unavailable or coverage `< 0.95`, and appends nothing at/above the threshold on an embeddings-available install (asserted in test_search.py, both human and JSON modes).
- [ ] FR#16: a failure computing the caveat degrades to no caveat and never breaks the recall (recall-path half of the never-raise rule).
- [ ] AC#8: test proves caveat-present (unavailable / partial) vs caveat-absent (healthy, at/above threshold), with `total == 0` handled without crashing.
- [ ] AC#9: a forced failure in the caveat computation leaves recall results intact with no caveat (recall half of the defensive-degradation criterion).
