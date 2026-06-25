# Context: Chunk-Level Conversation Embeddings (Issue #31)

## Problem & Motivation

`ccrecall` semantic search embeds exactly **one vector per branch**, built from the
markdown `context_summary` (first 2 + last 6 exchanges). For any session longer than 8
exchanges, everything between exchange 2 and exchange N−6 is never embedded — a user
searching for a topic discussed in the middle of a long working session gets no vector hit.
The recall gap is structural and scales with session length, and long working sessions are
exactly where recall matters most. This feature replaces the single summary vector with
**per-exchange chunk vectors** so every exchange is independently searchable, and lands two
distinct retrieval entrypoints: **A — session discovery** (scored session cards) and
**B — message retrieval** (matched-exchange snippets). The output shapes are governed by an
absorbed contract (`output-format-contract.md`, ex-PR #33); this work builds the retrieval
substrate that feeds it.

## Visual Artifacts

None. This is a backend/CLI change; the only "rendered" artifacts are markdown/JSON output
shapes, which are specified verbatim in `output-format-contract.md` (Track A card markdown +
JSON at its Architecture → "Track A — session-summary card", Track B snippet at "Track B —
message-summary").

## Key Decisions

1. **Chunk == one exchange** (user turn + following assistant turns, as produced by
   `build_exchange_pairs`). The contract's future "`message_vec`" is realized here as
   `chunk_vec`; "chunk", "exchange", and "matched exchange" are the same unit.
2. **Chunk-grain staleness, not branch-grain.** `chunks.embedding_version` / `embedding_model`
   drive query-time exclusion (FR#9), so a partially re-embedded branch still serves its
   already-current chunks instead of being excluded wholesale. The `branches.embedding_version`
   columns are retained and repurposed as a per-branch *watermark* ("every current exchange has
   a current-version vector"), maintained with a **clear-first / set-last** protocol.
3. **Two failure nets for the watermark.** The watermark alone is insufficient (challenge C1):
   the backfill also carries an explicit **chunk-grain heal clause** (`chunks` row with no
   `chunk_vec`) to catch suppressed write-path content errors and post-drop orphans.
4. **Write path is incremental and bounded.** Embed only new/content-changed exchanges
   (`content_hash` diff), capped at `MAX_WRITE_PATH_EMBEDS_PER_SYNC` per sync; version-stale
   chunks are left to the background backfill (challenge H6). Steady state ≈ 1 inference/sync
   **even immediately after an `EMBEDDING_VERSION` bump**. This is a requirement, not an
   optimization — the Stop hook spawns a detached process on the user's machine.
5. **Token-aware head+tail cap** (challenge M12): cap to a char budget, then verify
   `len(tokenize) <= MODEL_TOKEN_LIMIT` and tighten until it fits, so dense content (minified
   JSON / base64) under the char budget but over the token limit can't trip `CONTENT_ERROR`.
   Display `user_text`/`assistant_text` use the same head+tail logic so the shown excerpt aligns
   with the embedded region.
6. **Max rollup for A.** Rank sessions by their best-matching chunk (keep first chunk per
   branch in KNN order), composed with FTS via the unchanged RRF; a new `rrf_scored` sibling
   exposes the fused score. Score normalization (min-max, single-result→null) happens at
   render time over the bounded result set.
7. **B is a separate command, `ccrecall search-messages`** (challenge C3), not a `--chunks`
   flag — it returns a different result type (snippets vs cards) and a type-flipping flag
   collides with `--keyword-only`.
8. **One embedding code path.** All vectors (chunk write, query, backfill) go through
   `embeddings.py` (`embed_text`/`embed_texts`). No second embedding path may exist.
9. **Order invariant on write.** Vector upsert FIRST, version/bookkeeping columns LAST — a
   swallowed embed failure leaves the chunk eligible for backfill, never marked-done-without-
   vector. (Generalizes today's `write_branch_embedding`.)
10. **Additive-first, teardown-last migration.** `chunks`/`chunk_vec` are added alongside the
    existing `branch_vec` (which is dropped only once every caller has migrated to the chunk
    path), keeping every task's checkpoint green.

## Constraints & Anti-Patterns

- **vec0 rejects `INSERT OR REPLACE`** (confirmed against `sqlite-vec 0.1.9`): chunk upsert is
  DELETE-then-INSERT, like `upsert_branch_vec`.
- **No full-transcript inlining in any result list** — A renders cards, B renders bounded
  excerpts, `ccrecall tail` is the only full-fetch path. Do NOT keep the dump alive behind a flag.
- **Conversations DB is a public contract** — schema changes are additive
  (`CREATE ... IF NOT EXISTS`) plus a version-gated re-embed; **never** drop or rewrite
  `messages`/`branches`/`branch_messages`. Only derived `branch_vec`/`chunk_vec`/`chunks` are
  droppable (lossless — regenerable).
- **Stop hook stays non-blocking** — `memory_sync.py` spawns a detached `sync-current` and
  returns `{"continue": true}` immediately. Do NOT move embedding onto the hook thread.
- **Hooks remain direct console-script entry points** — do not add top-level imports that slow
  the ~440ms hook import; do not route hooks through the cyclopts app.
- **Markdown is the agent-facing default**; the global `--json` flag is the only output-format
  switch. Do not add per-command `--json`.
- **No fabricated scores** — LIKE-only fallback emits `ranked: false` with null scores.
- **Catch `sqlite3.Error` to degrade a query path, never a bare `except`** (see Convention
  Examples).
- **Out of scope (do NOT implement):** re-designing the output format; richer session summaries
  (#32); position-anchored `tail` / `--context N`; keyword-path Track B (FTS `snippet()`
  rendering — deferred to #34); changing the embedding model or dimension (stays
  `jina-v2-small`, 512-dim — only granularity changes).

## Design Doc References

- `## Architecture → (1) Schema` — `chunks` + `chunk_vec` DDL, cascade triggers, the
  unconditional `branch_vec` drop (NOT via dimension self-heal), watermark reset on `chunk_vec`
  drop, watermark clear-first/set-last protocol, the chunk-grain heal clause.
- `## Architecture → (2) Write path` — `embed_branch_chunks` steps 1–8, the diff, the cap,
  `MAX_WRITE_PATH_EMBEDS_PER_SYNC`, raises-not-suppresses contract + the two callers' nets.
- `## Architecture → (3) Backfill` — `CHUNK_EMBEDDABLE_BRANCH_FILTER`, eligibility =
  watermark-stale OR heal clause, per-branch message fetch (+`m.uuid`), content-error vs
  batch-abort distinction, inference-count status/ETA.
- `## Architecture → (4) Search + output` — chunk-KNN + best-chunk rollup (A), direct chunk
  retrieval (B), `rrf_scored`, render-time score normalization, ranked-signal wrapper, graceful-
  degrade topic probe, `tool_counts` PRAGMA guard, overfetch `CHUNK_COLLAPSE_FACTOR` + under-fill
  diagnostic, `search-messages` command, Track B vec0-unavailable behavior.
- `## Replacement Targets` — what is replaced vs retained (esp. `format_markdown_session` /
  `format_json_sessions` are RETAINED for `recent_chats.py`).
- `## Migration` — `EMBEDDING_VERSION` 2→3, reversibility, no history loss.
- `## Impact → Changed Files / Behavioral Invariants / Blast Radius`.
- `output-format-contract.md` — authoritative for card/snippet/envelope field shapes,
  markdown/JSON parity, score representation (`score` normalized, `score_raw` higher-is-better),
  field provenance table.

## Convention Examples

### Boundary-narrow exception handling (degrade vs. propagate)

**Source:** `src/ccrecall/search_conversations.py:150-157`

```python
try:
    serialized = sqlite_vec.serialize_float32(query_vec)
    rows = cursor.execute(
        "SELECT branch_id, distance FROM branch_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (serialized, top_k),
    ).fetchall()
except sqlite3.Error:
    return []  # DB-level failure → degrade to keyword; a real (non-DB) bug still propagates
```

The new chunk-KNN path follows the same rule: catch `sqlite3.Error` to degrade, never a bare
`except`.

### Order-invariant embedding write (vector first, bookkeeping last)

**Source:** `src/ccrecall/db.py:137-154`

```python
def upsert_branch_vec(cursor, branch_id, embedding):
    """Replace a branch's vector row (DELETE+INSERT — vec0 rejects INSERT OR REPLACE)."""
    cursor.execute("DELETE FROM branch_vec WHERE branch_id = ?", (branch_id,))
    cursor.execute("INSERT INTO branch_vec(branch_id, embedding) VALUES (?, ?)",
                   (branch_id, sqlite_vec.serialize_float32(embedding)))

def write_branch_embedding(cursor, branch_id, embedding, summary_version):
    upsert_branch_vec(cursor, branch_id, embedding)          # vector FIRST
    cursor.execute("UPDATE branches SET embedding_version = ?, ... WHERE id = ?", ...)  # bookkeeping LAST
```

The chunk write helpers (`upsert_chunk_vec`, `write_chunk_embedding`) mirror this exactly at the
chunk grain.

### vec0 cascade-on-delete trigger

**Source:** `src/ccrecall/db.py:200-204`

```python
conn.execute(
    "CREATE TRIGGER IF NOT EXISTS branches_vec_ad"
    " AFTER DELETE ON branches"
    " BEGIN DELETE FROM branch_vec WHERE branch_id = OLD.id; END"
)
```

The chunk design adds a second level (`branches`→`chunks`→`chunk_vec`); the spike confirmed the
two-trigger chain fires end-to-end on a single `DELETE FROM branches`.

### In-memory DB fixture for search tests

**Source:** `tests/test_search.py` (and `tests/conftest.py` `memory_db`)

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

New chunk/search tests seed the same way (real SQLite + real vec0 where available), mocking
only true external boundaries. Tests seed `embedding_version` from the `EMBEDDING_VERSION`
constant (not a hardcoded literal) so the version bump is transparent.

### `whenever` at the formatting boundary

**Source:** `src/ccrecall/formatting.py:12-28`

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
