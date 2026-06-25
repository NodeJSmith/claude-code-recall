> **Provenance:** absorbed from draft PR #33 (`worktree-review-format`) on 2026-06-25 as the
> authoritative output-format contract for issue #31's "full cohesive landing". PR #33 is
> being cancelled and its branch deleted; this doc is the surviving copy. The substrate +
> integration design that *implements* this contract lives in `design.md` alongside it.

# Design: Search-Result Format Contract (Session Cards + Message Snippets)

**Date:** 2026-06-25
**Status:** draft
**Scope-mode:** hold
**Research:** design/research/2026-06-25-search-result-format/research.md

## Problem

`ccrecall`'s recall surface returns a **raw transcript dump**. `search` (and `recent`)
rank at the session/branch level, then `_hydrate_branches()` fetches *every* message of
each matched session and `format_markdown_session()` prints the whole conversation — no
relevance score, no snippet, no bound. Three results can be ~31K tokens; the default of
five is worse. The primary consumer is an AI agent that must synthesize a short answer,
so this is not only a cost problem: oversized, undifferentiated context degrades answer
**quality** (distractors, "lost in the middle"), which the prior-art brief documents as
the "context stuffing" anti-pattern.

Two genuinely different jobs are conflated under one dump:

- **A — session discovery** ("find me *sessions* like X" — the *which* question)
- **B — message retrieval** ("find me the specific *messages* about X" — the *details* question)

Mature retrieval systems (Elasticsearch, ripgrep, Claude Code's own Grep/Glob, VS Code,
LSP) split these and, universally, **(1) attach a relevance score to every result** and
**(2) bound the output size**. `ccrecall` violates both today.

This design defines the **output data-shape contract** for both result types — what one
session-summary result and one message-summary result contain, and how each renders in
markdown and JSON. It is a *format contract*, not an implementation plan: the build order
for the two tracks is governed by GitHub #31 (message/chunk-granularity embeddings) and
#32 (richer session summaries), not by this doc.

## Goals

- Define the **session-summary result shape** (track A): a compact, scored, bounded card
  whose size is flat regardless of how long the underlying session was.
- Define the **message-summary result shape** (track B): a scored matched-exchange result
  with a session+position locator and a bounded excerpt.
- Pin the **shared invariants** every result of either type must satisfy: a relevance
  score (or an explicit "unranked" signal), a hard output bound, and markdown-default /
  JSON-superset parity.
- Keep `ccrecall tail` as the **single full-fetch path**; full transcript content is the
  deliberate, opt-in escape hatch, never inlined into a result list.
- Express both shapes in terms of data the repo **already stores** (`context_summary_json`,
  `messages_fts`, `branch_messages`) so the contract is implementable without new indexes
  for the keyword path.

## Non-Goals

- **Implementing either track.** This doc defines shapes only; the build lands under
  #31/#32 and their follow-ups.
- **A message-level vector index (`message_vec`).** Track B's *semantic ranking* needs it,
  but that is exactly GitHub #31. B's shape is defined independently of it (the shape is
  identical whether B is keyword-ranked or semantically ranked).
- **Position-anchored `tail`.** B's locator carries an exchange index, but teaching `tail`
  to open *at* a position (vs. its current end-of-session view) is follow-up work for B's
  implementation. The shape carries enough to support it later.
- **Unifying `recent` onto the card renderer.** A natural future simplification (`recent` =
  discovery sorted by time instead of relevance), explicitly deferred under hold scope.
- **Changing ranking algorithms.** RRF fusion, BM25, and the FTS/LIKE cascade are unchanged;
  this doc only adds *exposing* the score they already compute.

## User Scenarios

### Recall agent: AI consumer synthesizing an answer
- **Goal:** answer a user's "what did we decide about X" from past sessions.
- **Context:** invoked via the `/ccr-recall` skill; works inside a bounded context window.

#### A — locate the right session(s)
1. **Runs session discovery for a topic.**
   - Sees: a ranked list of compact cards — score, topic, disposition, counts, handle.
   - Decides: which one or two sessions are worth opening, from score + topic + disposition.
   - Then: issues `ccrecall tail <handle>` for the chosen session(s) — the full-fetch step.

2. **Triages by score.**
   - Sees: a relevance score on every card (or an explicit "unranked" marker when only the
     keyword-LIKE fallback ran).
   - Decides: whether result #3 is close enough to #1 to bother opening, or to stop.

#### B — pull the specific exchanges
1. **Runs message retrieval for a phrase.**
   - Sees: a ranked list of matched exchanges — score, locator (session handle + exchange
     index + time), the user/assistant pair with the matched terms highlighted.
   - Decides: whether the snippet already answers the question, or whether to fetch more.
   - Then: optionally `ccrecall tail <handle>` (or, future, `--context N` to widen the window).

### Human operator: terminal user
- **Goal:** find a past session without scrolling pages of transcript.
- **Context:** runs `ccrecall search`/message-retrieval directly in a terminal.

#### Scan and drill
1. **Reads the result list.**
   - Sees: markdown cards/snippets that fit on screen, newest-relevant first.
   - Decides: which handle to `tail`.

## Functional Requirements

- **FR#1** A session-summary result renders, by default, as a compact card containing: a
  relevance score, project, git branch, date, topic, disposition, exchange count, modified-
  file count, commit count, and a session handle.
- **FR#2** A session-summary result includes no exchange text and no full message list in its
  default (markdown) form.
- **FR#3** A session-summary result's rendered size is independent of the underlying
  session's length (a 500-exchange session and a 3-exchange session produce same-sized cards).
- **FR#4** A message-summary result renders, by default, as the matched exchange: the
  user/assistant turn pair containing the match, with the matched query terms highlighted.
- **FR#5** A message-summary result includes a locator sufficient to fetch more: session
  handle, exchange index within the session, and the exchange timestamp.
- **FR#6** A message-summary result's excerpt text is bounded per message (long turns are
  truncated), so a single result cannot reintroduce a transcript dump.
- **FR#7** Every result of either type carries a relevance `score` field when a ranking
  signal exists (FTS/BM25 or vector).
- **FR#8** When no ranking signal exists (LIKE fallback only), the envelope sets `ranked: false`
  and each result's `score` is null (no fabricated score), and results are ordered by recency.
- **FR#9** Both result types render in markdown by default and in JSON when the global
  `--json` flag is set; the JSON form is a strict superset of the markdown fields.
- **FR#10** The JSON form of a session-summary result carries the full metadata lists
  (files_modified, commits, tool_counts) that the markdown form reduces to counts.
- **FR#11** A session-summary card degrades gracefully when its branch has no
  `context_summary_json` (older/uncached branches): it still renders a card (topic from the
  first user message, counts from the branch row) and never errors.
- **FR#12** Neither result type inlines full transcript content; `ccrecall tail` remains the
  only path that returns a full session.
- **FR#13** A multi-branch session yields exactly one session-summary card (dedup to the
  highest-ranked branch), preserving today's per-session dedup behavior.

## Edge Cases

- **No matches:** A returns "No sessions found for query: …"; B returns "No messages found
  for query: …". Empty `results: []` with `count: 0` in JSON.
- **Unranked (LIKE-only) path:** `score` is null and `ranked: false` at the top level; a
  one-line "(keyword fallback — unranked, ordered by recency)" marker appears in markdown.
- **Uncached branch (no `context_summary_json`):** card falls back to first-user-message
  topic and branch-row counts; disposition omitted; must not crash (FR#11).
- **B match with no paired turn** (first user message, or an assistant turn with no preceding
  user): render the single available message; the exchange pairing already tolerates this.
- **Over-long topic / exchange text:** truncated via existing limits (`_TOPIC_MAX_CHARS`,
  `truncate_mid`); a card/snippet never grows unbounded.
- **Highlight straddling a truncation boundary:** best-effort highlight; correctness of the
  bound takes precedence over preserving every match marker.
- **Notification / subagent-result messages:** excluded from B by default, opt-in via
  `--include-notifications`. (The flag already exists on `search`/`recent` — `_NOTIFS` in
  `cli/commands.py` — and threads to `fetch_branch_messages`; B's CLI surface reuses the same
  flag, so no new flag, only wiring it through B's path.)
- **Score ties / identical fused scores:** stable secondary ordering by recency.

## Acceptance Criteria

- **AC#1** Given a session of any length, its rendered session card (markdown) contains a
  score, topic, disposition, counts, and handle, and contains no exchange/message body text.
  (FR#1, FR#2)
- **AC#2** Two sessions of very different lengths produce session cards within a small
  constant size of each other. (FR#3)
- **AC#3** A message-summary result shows the matched user/assistant exchange with the query
  terms highlighted and a locator of (handle, exchange index, timestamp). (FR#4, FR#5)
- **AC#4** A message-summary result's excerpt is bounded: a match inside a very long turn does
  not emit the full turn. (FR#6)
- **AC#5** With a ranking signal available, every result carries a numeric `score`; with only
  the LIKE fallback, every result carries `ranked: false` and a null score. (FR#7, FR#8)
- **AC#6** For both types, the `--json` output contains every field shown in markdown plus the
  superset fields, and parses as valid JSON. (FR#9, FR#10)
- **AC#7** A search whose top result is a branch with no `context_summary_json` still returns a
  well-formed card and exits 0. (FR#11)
- **AC#8** No result list, for either type, contains a full session transcript; only `tail`
  does. (FR#12)
- **AC#9** A session with two active branches matching the query appears once. (FR#13)

## Key Constraints

- **No full-transcript inlining in any result list.** The dump is the specific thing being
  removed; a "rich" option that re-inlines transcripts defeats the design.
- **No fabricated scores.** When the ranker produces no signal (LIKE fallback), surface
  "unranked" — do not invent a number. A misleading score is worse than an honest absence.
- **No new index for the keyword path.** Track B's defined shape must be producible from the
  existing `messages_fts` + `branch_messages` + `snippet()`; requiring `message_vec` to render
  a B result would couple this contract to #31, which is forbidden here.
- **Markdown is the agent-facing default.** Do not flip the default to JSON; the skill and
  hooks consume markdown. JSON is opt-in and additive.
- **Preserve the FTS5 → FTS4 → LIKE degradation contract.** The shape must be renderable at
  every rung of that cascade (with the unranked signal on the bottom rung).

## Dependencies and Assumptions

- **`context_summary_json`** is present on synced branches and carries topic, disposition,
  first/last exchanges, and metadata (the SessionStart-injection summary). A is a re-render of
  this data; the fallback (FR#11) covers branches that predate it.
- **`messages_fts`** (FTS5/FTS4 over message `content`) and **`branch_messages`** already
  exist; B's keyword shape and KWIC come from them via SQLite `snippet()`.
- **`build_exchange_pairs()`** (summarizer) defines the exchange unit and indexing that B's
  locator references — the same unit A's "N exchanges" count uses.
- No external services, auth, or network. Read-only over the local `~/.ccrecall/conversations.db`.
- No data sensitivity beyond what already lives in the DB; no new data is persisted by a format
  change.

## Architecture

This is a **rendering/output contract**, layered over the existing rank → select pipeline.
Nothing about ranking changes; the change is *what a selected result turns into*.

### Shared envelope (both tracks)

Top-level JSON envelope (markdown mirrors the same fields):

```json
{
  "query": "two-entrypoint split",
  "ranked": true,
  "count": 2,
  "results": [ /* card objects (A) or snippet objects (B) */ ]
}
```

- `ranked: false` ⇒ LIKE fallback; every result's `score` is null; markdown prints the
  unranked marker line.
- `count` is the bounded result count (A: `--max-results` 1–10; B: top-k, same bound family).

### Track A — session-summary card

Markdown (default):

```
## {score:.2f}  {project} · {git_branch} · {ended_date}
Topic:  {topic}
Status: {disposition} · {exchange_count} exchanges · {n_files} files · {n_commits} commits
Handle: {handle}   → ccrecall tail {handle}
```

JSON result object (superset):

```json
{
  "score": 0.87,
  "score_raw": 0.0309,
  "session_uuid": "ef098861-8904-4f1d-a368-4f806ba059d7",
  "handle": "ef098861",
  "project": "ccrecall",
  "git_branch": "review-format",
  "started_at": "2026-06-25T07:30:00Z",
  "ended_at": "2026-06-25T13:09:42Z",
  "topic": "redesign search result format — two-entrypoint split",
  "disposition": "IN_PROGRESS",
  "exchange_count": 41,
  "files_modified": ["src/ccrecall/search_conversations.py", "…"],
  "commits": ["chore(main): release 0.11.1"],
  "tool_counts": {"Read": 40, "Bash": 22}
}
```

**Field provenance** (the card is assembled from two sources — the renderer must know which):

| From the rank/select join (`branches`+`sessions`+`projects`, as in `_hydrate_branches`) | From `context_summary_json` |
|---|---|
| `session_uuid`, `handle` (uuid[:8]), `project`, `git_branch`, `started_at`, `ended_at`, `score`/`score_raw` | `topic`, `disposition`, and (under `metadata`) `exchange_count`, `files_modified`, `commits`, `tool_counts` |

Note `context_summary_json` stores `started_at`/`ended_at`/`git_branch` under its `metadata`
key too; prefer the join columns as the source of truth and treat the summary's copies as
fallback. The markdown reduces the three metadata lists to counts; JSON carries them in full
(FR#10). A **new compact card renderer** sits beside the existing `render_context_summary()`
(which stays — it is the SessionStart-injection renderer, reached via `compute_context_summary`
and the `memory_context` fallback); A does **not** call `fetch_branch_messages` at all.

### Track B — message-summary (matched exchange)

Markdown (default):

```
{score:.2f}  {project}/{git_branch} · {handle} · exchange {idx} · {time}
  User: {user_text — query terms highlighted, per-message bounded}
  Asst: {assistant_text — query terms highlighted, per-message bounded}
  → ccrecall tail {handle}
```

JSON result object:

```json
{
  "score": 0.91,
  "score_raw": 1.84,
  "session_uuid": "ef098861-…",
  "handle": "ef098861",
  "project": "ccrecall",
  "git_branch": "review-format",
  "exchange_index": 19,
  "matched_role": "assistant",
  "timestamp": "2026-06-25T13:02:11Z",
  "user": "does B need its own message-level index?",
  "assistant": "the existing messages_fts already covers message-level keyword search…",
  "match_terms": ["messages_fts", "snippet"]
}
```

The match is found at the message grain (via `messages_fts`), then widened to its enclosing
exchange pair for display.

**`exchange_index` is resolved against the session's active-leaf branch** — the same branch A
surfaces — not against the raw `messages` table. This matters because a `messages_fts` hit is at
the message grain and one message can belong to several branches of a session; the index is
defined as the position from `build_exchange_pairs()` run over *that active-leaf branch's*
messages, so it is single-valued per session. The **unambiguous anchor** is the pair
(`handle`, `timestamp`) — `timestamp` alone pins the message; `exchange_index` is a
human/agent-friendly convenience that a future position-anchored `tail`/`--context N` can use.
The exact FTS-rowid → active-leaf-exchange mapping is a track-B *implementation* detail (deferred
with B), but the shape is fixed: locator = (`handle`, `timestamp`, `exchange_index`).

### Score representation

Two fields per result: the **presented `score`** (uniform across both tracks) and a **`score_raw`**
(the ranker-native value). To keep `score_raw` interpretable, it follows one cross-track
convention: **higher = better, always.** The two rankers' native scales are *not* otherwise
comparable, so `score_raw` is for within-track/within-query inspection only — never compare an A
`score_raw` to a B one.

- **Presented `score`:** min-max normalized to `[0,1]` within the result set (two decimals), for
  both tracks. Chosen for triage usefulness (relative gaps are visible) over raw scores (not
  comparable across queries). **Single-result edge case (#31 amendment):** when the result set
  has exactly one result, min-max is degenerate (`0/0`); rather than emit a misleading `1.00`,
  `score` is `null` (the markdown renders without a score line) while `score_raw` is still
  emitted. A lone `1.00` would read as a perfect match and give a triaging agent no calibration.
- **A `score_raw`:** the fused RRF value — a small positive float (e.g. `0.0309`); larger = better
  already. Today `rrf()` returns ranked ids and *discards* this; the contract needs a
  **score-returning fusion** (a sibling returning `(id, score)` pairs) so the card can carry it.
- **B `score_raw` — keyword path:** SQLite `bm25()` (FTS5) returns a value where *more negative* =
  better, so the contract stores its **negation** (e.g. native `-1.84` → `score_raw: 1.84`) to
  satisfy the higher-is-better convention. FTS4 lacks BM25 → fall back to `matchinfo`/rank ordinal;
  if even that is unavailable, `ranked: false`.
- **B `score_raw` — vector path (#31 amendment):** the chunk-KNN distance is L2 on normalized
  vectors (lower = better), stored as `score_raw = 1.0 - distance` (higher = better). On the
  vector path the chunk *is* the matched unit, so there are no discrete term hits: **`matched_role`
  is `null` and `match_terms` is `[]`** (the markdown excerpt renders without highlighting). Only
  the keyword path populates `matched_role`/`match_terms`. The field *names* are identical across
  ranking sources — consumers must accept `matched_role: null` and `match_terms: []` as valid.
- **LIKE fallback (either track):** no ranker ⇒ `score: null`, `score_raw: null`, `ranked: false`,
  recency order.

See Alternatives for the raw-vs-normalized trade-off.

### Where this layers in

`search_conversations.run()` keeps rank → dedup → select; the terminal step changes from
"`_hydrate_branches` → `format_markdown_session` (dump)" to "render card from
`context_summary_json` + score". Track B is a parallel select-and-render path over
`messages_fts`. Both feed the shared envelope.

## Replacement Targets

- **`formatting.py::format_markdown_session()` (full-conversation dump) — replaced for the
  search/discovery path** by the compact card renderer. The function may remain only if another
  consumer needs it; the search path stops calling it. Implementers should not keep the dump
  alive behind a flag on A.
- **`search_conversations.py::_hydrate_branches()` message fetch — removed from track A.** A
  renders from `context_summary_json`; it must not call `fetch_branch_messages`. The function
  either loses its message-loading or is replaced by a card-hydrator that selects summary
  columns.
- **`fusion.py::rrf()` score discard — superseded** by a score-returning fusion for A's `score`.
  `rrf()` itself may stay if other callers want ids-only, but A uses the score-bearing variant.
- **`formatting.py::format_json_sessions()` (sessions-with-full-messages JSON) — replaced** by
  the card/snippet JSON envelopes above. The old JSON inlines every message; the new JSON
  carries card/snippet objects.

No existing *ranking* code is replaced — only the hydrate-and-dump tail and the score discard.

## Convention Examples

### Boundary-narrow exception handling (degrade vs. propagate)

**Source:** `src/ccrecall/search_conversations.py`

```python
def _get_vec_branch_ids(cursor, query_vec, top_k, ...):
    try:
        serialized = sqlite_vec.serialize_float32(query_vec)
        rows = cursor.execute(
            "SELECT branch_id, distance FROM branch_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (serialized, top_k),
        ).fetchall()
    except sqlite3.Error:
        return []  # DB-level failure → degrade; a real (non-DB) bug still propagates
```

New rendering/scoring code follows the same rule: catch `sqlite3.Error` to degrade a query
path, never a bare `except`. (Tested by `TestExceptionNarrowing`.)

### FTS5 → FTS4 → LIKE capability cascade

**Source:** `src/ccrecall/schema.py` + `src/ccrecall/search_conversations.py`

```python
def detect_fts_support(conn) -> str | None:
    # (real impl wraps the PRAGMA in try/except sqlite3.Error → None; elided here)
    opts = {row[0] for row in conn.execute("PRAGMA compile_options").fetchall()}
    if "ENABLE_FTS5" in opts:
        return "fts5"
    if "ENABLE_FTS4" in opts or "ENABLE_FTS3" in opts:
        return "fts4"
    return None
```

The B-score path branches on this same `fts_level`: BM25 on fts5, `matchinfo`/ordinal on fts4,
`ranked: false` on the LIKE rung.

### In-memory DB fixture for search tests

**Source:** `tests/test_search.py`

```python
@pytest.fixture
def search_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.commit()
    # … seed projects / sessions / branches / messages / branch_messages …
    yield conn
    conn.close()
```

New card/snippet rendering tests seed the same way (real SQLite, real FTS triggers) rather than
mocking the DB — mock only at true external boundaries.

### `whenever` at the formatting boundary

**Source:** `src/ccrecall/formatting.py`

```python
def format_time(ts_str: str | None, fmt: str = "%H:%M") -> str:
    if not ts_str:
        return "??:??"
    try:
        local = Instant.parse_iso(ts_str).to_system_tz()
        return local.to_stdlib().strftime(fmt)  # cross to stdlib only for strftime
    except ValueError:
        return ts_str[:16] if ts_str else "??:??"
```

Card/snippet timestamps reuse these helpers; no stdlib `datetime` in new code.

## Alternatives Considered

- **Keep the full-summary render for A (today's `render_context_summary`).** Rejected as the
  default: N results × first/last-exchange summary re-bloats — it is most of what we're escaping.
  (Available behind `--verbose`/JSON if a later need appears; not the default.)
- **B as a single matched message (KWIC line), not an exchange pair.** Lighter per hit and the
  initial recommendation, but the chosen shape pairs the match with its user/assistant turn for
  enough surrounding context to judge relevance without a `tail`. Accepted the ~2× token cost for
  the higher signal; the per-message bound (FR#6) keeps it from drifting back to a dump.
- **Raw scores instead of min-max normalization.** Simpler and absolute, but raw RRF/BM25 values
  aren't comparable across queries and read as noise to a triaging agent. Compromise: normalized
  in markdown, `score_raw` preserved in JSON.
- **Defining B's shape only after `message_vec` (#31) lands.** Rejected: the result *shape* is
  identical whether B is keyword- or vector-ranked, so deferring the shape needlessly blocks the
  contract. Only B's *ranking quality* depends on #31.
- **Do nothing (keep the dump).** Rejected: documented quality and cost harm; violates the two
  universal invariants every surveyed system enforces.

## Test Strategy

### Existing Tests to Adapt
- **`tests/test_search.py`** — `search_sessions()` currently returns session dicts carrying full
  `messages`; tests assert on `r["messages"]` (e.g. `test_messages_loaded`). When A stops
  hydrating messages, these assertions move to card fields (topic/disposition/score/handle). The
  dedup, filter, and degradation tests stay (ranking is unchanged).
- **`tests/test_formatting.py`** — covers `format_markdown_session` / `format_json_sessions`;
  adapt to the new card/snippet renderers (or split: keep session-injection rendering tests,
  add card tests).
- **`tests/test_fusion.py`** — add coverage for the score-returning fusion variant alongside the
  existing ids-only `rrf()` tests.

### New Test Coverage
- Card renderer: flat size across session lengths (AC#2); no body text (AC#1); uncached-branch
  fallback (AC#7); per-session dedup (AC#9). **Unit.**
- Snippet renderer: matched-exchange shape + highlight + locator (AC#3); per-message bound on a
  long turn (AC#4); no-paired-turn edge case. **Unit.**
- Score contract: numeric score when ranked; `ranked:false` + null score on LIKE (AC#5).
  **Unit**, across fts5/fts4/LIKE rungs.
- JSON superset parity for both types (AC#6). **Unit.**
- "No result list contains a full transcript" guard (AC#8). **Integration**, over a seeded DB.

### Tests to Remove
- Any assertion that the search/discovery path returns full per-session message bodies (as
  opposed to moving it) — once A is message-free, those expectations are wrong, not just relocated.

## Documentation Updates

- **`skills/ccr-recall/references/tool-reference.md`** — replace the "Output" example (full
  `### Conversation` dump) with the card and snippet shapes; document the `score`/`ranked` fields
  and the markdown/JSON parity.
- **`skills/ccr-recall/SKILL.md`** — the Tools/Workflow sections describe `search` as returning
  conversations; update to "ranked session cards" (A) and, when B ships, "matched exchanges" (B);
  note `tail` as the drill-in.
- **`CLAUDE.md`** — the architecture note mentions `formatting.py`/search; refresh once the dump
  is replaced (the "one parse boundary" framing is unaffected).
- **GitHub #31 / #32** — cross-link this contract as the agreed result shape the implementations
  must emit (comment, not a doc file).
- **CHANGELOG** — handled by release-please from Conventional Commits at implementation time; no
  manual entry now.

## Impact

### Changed Files
*(This doc is the artifact; the list below is the shape the implementing work will touch, seeded
for `mine-plan`. No code changes land from the design doc itself.)*

- `src/ccrecall/formatting.py` — modify: add compact card + snippet renderers; retire the
  full-conversation dump from the search path; replace `format_json_sessions` envelope.
- `src/ccrecall/search_conversations.py` — modify: render A from `context_summary_json`; drop
  message hydration from track A; thread the score through.
- `src/ccrecall/fusion.py` — modify: add a score-returning fusion variant (keep `rrf()` ids-only).
- `src/ccrecall/summarizer.py` — read: `build_exchange_pairs`, `context_summary_json` shape, and
  truncation limits are referenced by the renderers (no change required by the shape itself).
- `src/ccrecall/db.py` — read: `fetch_branch_messages` / `branch_messages` consulted by track B's
  exchange assembly (B implementation).
- `skills/ccr-recall/references/tool-reference.md` — modify: documented output shapes.
- `skills/ccr-recall/SKILL.md` — modify: tool descriptions.
- `tests/test_search.py`, `tests/test_formatting.py`, `tests/test_fusion.py` — modify: adapt to
  card/snippet shapes and the score contract; add new coverage.

### Behavioral Invariants
- **Ranking is unchanged.** FTS5/FTS4/LIKE cascade, RRF fusion, per-session dedup, stale-embedding
  exclusion, and all `--project`/`--session`/`--path` filters keep their current behavior. Only the
  rendered output and the exposed score are new.
- **`tail` is untouched** by this contract (its position-anchoring is separate, deferred work).
- **Markdown stays the default;** `--json` stays the only output-format switch (global flag).
- **The conversations DB schema is unchanged** — this is a read/render contract, no migration.

### Blast Radius
- The `/ccr-recall` skill and its reference doc (primary consumer) — output shape changes; the
  skill's synthesis instructions still apply.
- The SessionStart context-injection path is **not** affected: it uses `render_context_summary`
  directly, which this contract leaves in place.
- Any external/manual caller parsing `search --json` sees a new (documented) envelope — acceptable
  pre-1.0; called out in Documentation Updates.

## Open Questions

*(none — resolve any that arise before plan approval)*
