---
task_id: "T07"
title: "Add search-messages command for Entrypoint B matched exchanges"
status: "planned"
depends_on: ["T05", "T06"]
implements: ["FR#4", "FR#13", "FR#17", "FR#11", "AC#3", "AC#14"]
---

## Summary

Add Entrypoint B as a separate CLI command, `ccrecall search-messages QUERY`, returning matched
**exchanges** (not rolled up to session) ranked by chunk distance, rendered as the contract's
snippet shape. Because each `chunks` row already carries both bounded turns and the locator, B needs
no message fetch and no FTS-rowid→exchange mapping. On a machine where vec0 won't load, B returns a
well-formed empty `ranked:false` envelope (it has no keyword fallback in this landing — deferred to
issue #34).

## Target Files

- modify: `src/ccrecall/cli/commands.py`
- modify: `src/ccrecall/search_conversations.py`
- modify: `tests/test_search.py`
- read: `src/ccrecall/formatting.py`
- read: `src/ccrecall/db.py`
- read: `design/specs/001-chunk-level-embeddings/output-format-contract.md`
- read: `design/specs/001-chunk-level-embeddings/design.md`
- read: `design/specs/001-chunk-level-embeddings/tasks/context.md`

## Prompt

Implement Entrypoint B per design.md `## Architecture → (4) Search + output` (Entrypoint B section)
and `output-format-contract.md` "Track B — message-summary". B is a **separate command**, not a
flag on `search` (challenge C3).

1. **Search path (`search_conversations.py`)** — add a B entry function (e.g. `search_messages`)
   that runs the **same** chunk-KNN query as A (reuse the chunk-KNN helper from T06), filters to
   current version + the user `--project`/`--session`/`--path` filters, but **does NOT roll up to
   session** — return the top chunk rows directly. Hydrate each into the contract's snippet shape
   straight from the `chunks` row: `exchange_index`, `timestamp`, `first_message_uuid` (locator),
   and the `user`/`assistant` fields from the row's bounded `user_text`/`assistant_text`. No
   `fetch_branch_messages`, no FTS mapping (the chunk *is* the exchange).
   - `score_raw = 1.0 - distance` (vectors are L2-normalized; lower distance = better → higher
     score_raw = better, satisfying the contract's higher-is-better invariant). Presented `score` is
     min-max normalized at render time over the result set (T05 envelope) — single-result → `null`.
   - Semantic-path field values: `match_terms = []` and `matched_role = null` (the whole exchange is
     the match unit; no discrete term hits). The field *names* match the keyword path; only values
     differ.
   - **vec0 unavailable (FR#17):** when the chunk vector index is unavailable (`chunk_vec_queryable`
     false, or `sqlite3.Error` on the KNN), return a well-formed **empty `ranked:false` envelope** —
     exit 0, never an error. There is no keyword fallback for B in this landing (deferred, #34).
   - Render via the T05 **snippet renderer** + envelope; markdown default, JSON under the global
     `--json`.

2. **CLI (`cli/commands.py`)** — add a `search-messages` command that mirrors `search`'s option
   group: `--max-results` (same `MAX_SEARCH_RESULTS` bound family), `--project`, `--session`,
   `--path`, `--include-notifications` (reuse `_NOTIFS`), `--db`, and the global `--json` via
   `ctx.output_format` — no per-command `--json`. The positional/`-q` query is required (B has no
   `--status` mode). Reuse the shared `Annotated` flag types `_NOTIFS` and `_DB` and the
   `CLIContextParam`. **Do NOT add `--verbose` to `search-messages`:** `--verbose` is a Track A card
   affordance (it expands the card's `files_modified`/`commits`/`tool_counts` lists); a B snippet has
   no analogous collapsible metadata — its excerpt is already bounded — so `--verbose` would be a
   no-op surface. Omit it. Call the B run function in `search_conversations`.

3. **Tests (`tests/test_search.py`)** — add B coverage:
   - **Matched-exchange shape (AC#3):** B returns matched exchanges with a `(handle,
     exchange_index, timestamp)` locator and bounded `user`/`assistant` excerpts, ordered by chunk
     distance; not rolled up (two matches in one session both appear).
   - **Bounded excerpt:** a match inside a very long turn does not emit the full turn (the bounded
     `user_text`/`assistant_text` are used).
   - **vec0 unavailable (AC#14):** with the chunk vector index unavailable, `search-messages` exits
     0 with an empty `ranked:false` envelope.
   - **Contract parity (AC#10, snippet half):** snippet JSON/markdown match
     `output-format-contract.md` field-for-field, including `matched_role:null`/`match_terms:[]`.

## Focus

- The chunk-KNN helper and `chunk_vec_queryable` guard land in T06 — reuse them; do not write a
  second KNN path (one embedding/query path is a key constraint).
- The snippet renderer + envelope + score normalization land in T05 — reuse them; B only computes
  `score_raw = 1.0 - distance` and assembles snippet dicts.
- `cli/commands.py` patterns: `cmd_search` (`:193-228`) is the closest template; `_SEARCH_MODE`
  group is search-specific (B has no status mode, so don't reuse that group). `_NOTIFS`/
  `_DB`/`CLIContextParam`/`ctx.output_format` are at `:58-65` and used throughout — follow them.
  (`_VERBOSE` exists there too but is **not** used by `search-messages` — see the Prompt prohibition.)
- `--include-notifications` already threads to `fetch_branch_messages`; for B it is mostly moot
  (chunks store the exchange text directly), but accept the flag for surface symmetry.
- Document the exact command name `search-messages` (the skill + tool-reference doc updates are
  T09's job; just make the command name match what T09 will document).
- Catch `sqlite3.Error` to degrade to the empty `ranked:false` envelope; never a bare except.

## Verify

- [ ] FR#4: `ccrecall search-messages` returns matched exchanges directly (not rolled up to
      session), semantically ranked by chunk distance.
- [ ] FR#13: Each B result carries a `(handle, exchange_index, timestamp)` locator and a
      per-message-bounded excerpt (separate `user`/`assistant` fields).
- [ ] FR#17: With the vector index unavailable, B returns a well-formed empty `ranked:false`
      envelope (never an error) — AC#14.
- [ ] FR#11: B's snippet + envelope JSON/markdown conform to `output-format-contract.md` (no extra
      output fields), via the T05 renderers (AC#10 snippet half).
- [ ] AC#3: B returns matched exchanges with a `(handle, exchange_index, timestamp)` locator and a
      bounded excerpt, ordered by chunk distance.
- [ ] AC#14: With the vector index unavailable, `search-messages` exits 0 with an empty
      `ranked:false` envelope.
- [ ] AC#10: B snippet JSON/markdown match `output-format-contract.md` field-for-field (including
      `matched_role:null`/`match_terms:[]`).
