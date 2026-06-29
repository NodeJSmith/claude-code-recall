# Tool Reference

## ccrecall recent

Retrieve recent conversation sessions with all messages.

```bash
ccrecall recent --n 3
```

| Option                    | Effect                                                 |
| ------------------------- | ------------------------------------------------------ |
| `--n N`                   | Number of sessions (1-20, default 3)                   |
| `--sort-order`            | 'desc' (newest first, default) or 'asc'                |
| `--before DATE`           | Sessions before this datetime (ISO)                    |
| `--after DATE`            | Sessions after this datetime (ISO)                     |
| `--session UUID`          | Filter by session UUID (prefix match)                  |
| `--project NAME`          | Filter by project name(s), comma-separated             |
| `--path SUBSTR`           | Filter by cwd substring (e.g. worktree name)           |
| `--verbose`               | Include files_modified and commits                     |
| `--json`                  | Global flag (any position): emit JSON instead of markdown |
| `--include-notifications` | Include task notification messages (hidden by default) |

Use `--verbose` for lenses that need file/commit context (restore-context, review-process, run-retro).

## ccrecall search

Search for sessions using keyword full-text search (FTS5/FTS4/LIKE cascade) fused with chunk-vector similarity via RRF when the sqlite-vec index is available. Returns **ranked session cards** (Track A) — one compact card per session, no transcript text. Drill into a session with `ccrecall tail <handle>`.

```bash
ccrecall search --query "keyword"
```

| Option | Effect |
|--------|--------|
| `--query/-q` | Required (unless `--status`) — substantive keywords |
| `--status` | Print diagnostic info and exit 0 |
| `--keyword-only` | Skip embedding, use keyword search only |
| `--max-results N` | Limit results (1-10, default 5) |
| `--session UUID` | Filter by session UUID (prefix match) |
| `--project NAME` | Filter by project name(s), comma-separated |
| `--path SUBSTR` | Filter by cwd substring (e.g. worktree name) |
| `--verbose` | Expand files_modified, commits, and tool_counts in markdown output (JSON always carries full lists) |
| `--json` | Global flag (any position): emit JSON instead of markdown |
| `--include-notifications` | Include task notification messages (hidden by default) |

### Output — Track A: session card

**Markdown (default):**

```
## 0.87  ccrecall · review-format · 2026-06-25
Topic:  redesign search result format — two-entrypoint split
41 exchanges · 2 files · 1 commits
Handle: ef098861   → ccrecall tail ef098861
```

When `--verbose` is set, three lines are appended after `Handle:`:

```
Files:  src/ccrecall/search_conversations.py, src/ccrecall/formatting.py
Commits: docs: update tool reference for cards and snippets
Tools:  Read: 40, Bash: 22
```

The score prefix is omitted from the heading when only one result is returned or when the LIKE fallback ran. On the unranked path a marker line precedes all cards:

```
(keyword fallback — unranked, ordered by recency)
```

When a branch has no cached summary (`context_summary_json`), the card falls back to the first user message as the topic — the card still renders and the command still exits 0.

**JSON (`ccrecall --json search -q "keyword"`):**

```json
{
  "query": "two-entrypoint split",
  "ranked": true,
  "count": 2,
  "results": [
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
      "exchange_count": 41,
      "files_modified": ["src/ccrecall/search_conversations.py", "src/ccrecall/formatting.py"],
      "commits": ["docs: update tool reference for cards and snippets"],
      "tool_counts": {"Read": 40, "Bash": 22}
    }
  ]
}
```

JSON always carries the full `files_modified`, `commits`, and `tool_counts` lists regardless of `--verbose`.

### `--status` output

```
vec extension: yes
model: jina-v2-small-en-v1.5 (deps available)
chunk coverage: 312/318 chunks at current version
embedded branches: 14/15 (watermark)
```

`chunk coverage` counts exchange-level chunk vectors at the current embedding version. `embedded branches` is the branch watermark — branches where every chunk is at the current version.

## ccrecall search-messages

Search for specific **matched exchanges** by semantic similarity (Track B). Returns one result per matched exchange without rolling up to session. On machines where the vector index is unavailable, exits 0 with an empty unranked result. There is no keyword fallback for `search-messages` in this release.

```bash
ccrecall search-messages --query "keyword"
```

| Option | Effect |
|--------|--------|
| `--query/-q` | Required — search query |
| `--max-results N` | Limit results (1-10, default 5) |
| `--session UUID` | Filter by session UUID (prefix match) |
| `--project NAME` | Filter by project name(s), comma-separated |
| `--path SUBSTR` | Filter by cwd substring (e.g. worktree name) |
| `--json` | Global flag (any position): emit JSON instead of markdown |
| `--include-notifications` | Include task notification messages (hidden by default) |

No `--verbose` flag: snippet text is pre-bounded per message with no collapsible metadata lists.

### Output — Track B: matched exchange snippet

**Markdown (default):**

```
0.91  ccrecall/review-format · ef098861 · exchange 19 · 13:02
  User: does B need its own message-level index?
  Asst: the existing messages_fts already covers message-level keyword search…
  → ccrecall tail ef098861
```

The score prefix is omitted when only one result is returned. On the vector path `matched_role` is `null` and `match_terms` is `[]` — the whole exchange is the match unit, so no term highlighting appears.

**JSON (`ccrecall --json search-messages -q "keyword"`):**

```json
{
  "query": "message-level index",
  "ranked": true,
  "count": 1,
  "results": [
    {
      "score": null,
      "score_raw": 0.91,
      "session_uuid": "ef098861-8904-4f1d-a368-4f806ba059d7",
      "handle": "ef098861",
      "project": "ccrecall",
      "git_branch": "review-format",
      "exchange_index": 19,
      "matched_role": null,
      "timestamp": "2026-06-25T13:02:11Z",
      "user": "does B need its own message-level index?",
      "assistant": "the existing messages_fts already covers message-level keyword search…",
      "match_terms": []
    }
  ]
}
```

`score` is `null` here because there is only one result (see score semantics below). With multiple results, `score` is a normalized float like `0.87`. `matched_role` and `match_terms` are `null`/`[]` on the vector path. A future keyword-path Track B rung populates them via FTS `snippet()`.

## Score and envelope fields

Both tracks share the same JSON envelope and score semantics.

**Envelope** — top-level fields present in every JSON response:

| Field | Type | Description |
|-------|------|-------------|
| `query` | string | The search query as submitted |
| `ranked` | bool | `true` when FTS or vector ranking produced a signal; `false` on the LIKE-only fallback |
| `count` | int | Number of results in this response |
| `results` | array | Card (A) or snippet (B) objects |

**`score`** — min-max normalized to `[0.0, 1.0]` within the bounded result set, two decimal places. `null` in three cases:

- only one result (degenerate normalization window),
- all results share the same raw score, or
- the LIKE fallback ran (`ranked: false`).

A `null` score is not a failure — it means no relative calibration is available for this result set.

**`score_raw`** — the ranker-native value, higher-is-better in both tracks. `null` on the unranked path.
- Track A: fused RRF value (small positive float; larger = better).
- Track B (vector path): `1.0 - distance` (cosine-style similarity derived from L2 distance on normalized vectors).

**Markdown/JSON parity** — every field visible in markdown is also present in JSON. JSON adds fields that are too verbose for the terminal view: `score_raw`, `session_uuid`, `started_at`, `files_modified`, `commits`, `tool_counts` for cards; `score_raw`, `session_uuid`, `matched_role`, `match_terms` for snippets.
