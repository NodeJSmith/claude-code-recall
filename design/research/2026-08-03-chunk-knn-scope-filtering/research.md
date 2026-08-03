# Research Brief: Fix Chunk-KNN Scope Filtering Before Top-K Limiting

---
proposal: "Fix ccrecall vector search so project/session/path/date filters cannot exclude valid in-scope chunks before they are considered."
date: 2026-08-03
status: Draft
flexibility: Exploring
motivation: "Best recall. Prioritize search correctness even if the implementation is more involved."
constraints: "Scope is the search subsystem; inspect related search contracts and CLI behavior, not a full rewrite. No hard constraints."
non-goals: "No full search rewrite; no application-code changes in this research pass."
depth: normal
---

## Context

### What prompted this

Issue 100 identifies a recall bug in the vector path: `execute_chunk_knn()` asks sqlite-vec for the globally nearest `top_k` rows first, then applies scope/date predicates in a second SQL query. If the first `top_k` global chunks are mostly outside the user's requested scope, in-scope chunks beyond that global window are never seen.

### Current state

The search subsystem has two user-facing entry points:

- `ccrecall search` / `search_sessions()` is Track A: session cards. It fuses keyword branch hits and vector branch hits with RRF when embeddings are available, then deduplicates to one branch per session and hydrates cards.
- `ccrecall search-messages` / `search_messages()` is Track B: matched exchange snippets. It is vector-only today and intentionally does not roll up chunks to sessions.

Current vector flow:

1. `search_sessions()` computes `chunk_top_k = max(max_results * 4 * 8, 20)` and calls `get_vec_chunk_ids()`.
2. `search_messages()` computes `top_k = max(max_results * 4, 20)` and calls `execute_chunk_knn()` directly.
3. `execute_chunk_knn()` runs:
   `SELECT chunk_id, distance FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance`.
4. Only after that global KNN result is materialized, it joins `chunks`, `branches`, `sessions`, and `projects`, filters current `embedding_version` / `embedding_model`, `b.is_active = 1`, and appends `scope_filter_clause()` predicates for project/session/path/before/after.
5. The result preserves original KNN distance order and drops filtered-out chunks.

This means all filters in `scope_filter_clause()` are post-KNN filters for the vector path. Keyword search does not have this bug: `get_fts_branch_ids()` appends the same scope predicate before `ORDER BY ... LIMIT ?` on both FTS and LIKE rungs.

### Search contracts and CLI/API semantics

- Scope predicates are shared through `search_query.scope_filter_clause()` and assume aliases `s` = sessions, `p` = projects, `b` = branches.
- Project filter: `p.name IN (...)`, parsed from comma-separated CLI `--project` by `parse_project_filter()`.
- Session filter: prefix match, `s.uuid LIKE '<escaped>%'`.
- Path filter: substring match, `s.cwd LIKE '%<escaped>%'`.
- Date filters: branch-level `b.started_at < before` and `b.started_at > after`, not per-exchange. CLI help for both `search` and `search-messages` says this explicitly.
- `run()` and `run_messages()` validate dates before DB access and normalize offset instants to UTC via `validate_or_exit()`.
- `search_sessions()` returns `(cards, ranked)`. Fusion path is ranked; fts5 keyword fallback is ranked; fts4/LIKE are unranked by current contract.
- `search_messages()` returns `(snippets, ranked)`. It returns `ranked=False` only before KNN can run; if sqlite-vec is available and KNN runs but returns no rows, it returns `([], True)`.
- Vector result ordering is distance order for Track B. Track A rolls up best chunk per branch, then RRF-fuses vector branch order with keyword branch order.
- `MAX_SEARCH_RESULTS = 10`; CLI validates and `search_cli` clamps direct callers.
- JSON output uses `{query, ranked, count, results}`. Track A additionally includes `caveat`; Track B does not.

## Feasibility Analysis

### What would need to change

| Area | Files affected | Effort | Risk |
|---|---:|---|---|
| KNN retrieval strategy | `src/ccrecall/search_vector.py` | Medium | High impact because both Track A and B share `execute_chunk_knn()` |
| Scope predicate handling | `src/ccrecall/search_query.py`, possibly new helper in `search_vector.py` | Medium | Current helper is SQL-alias specific and not directly usable inside `chunk_vec` without joins/metadata |
| Track A/B top-k behavior | `src/ccrecall/search_conversations.py` | Low/Medium | Existing overfetch constants already encode recall/performance tradeoffs; may need new constants or parameters |
| CLI/API semantics | `src/ccrecall/search_cli.py`, `src/ccrecall/cli/commands.py` | Low | Likely no flag changes; semantics should be preserved |
| Tests | `tests/test_search.py` | Medium | Needs targeted vec fixtures that prove filtered in-scope hits beyond global `top_k` are recovered |
| Schema/migration, if predicate pushdown chosen | `src/ccrecall/db.py`, `src/ccrecall/schema.py`, migrations/backfill write paths | Large | Changing `chunk_vec` shape is derived-data-safe but touches setup, writes, and backfill behavior |

### What already supports this

- `execute_chunk_knn()` is a single shared choke point for chunk-KNN. Fixing it benefits both `search` fusion and `search-messages`.
- `scope_filter_clause()` already centralizes the intended filter semantics for project/session/path/date.
- Tests already cover keyword filters, date validation, stale chunk exclusion, Track A rollup, Track B no-rollup, JSON/markdown output contracts, and vec-unavailable degradation.
- `chunk_vec` contains only derived vectors. Dropping/recreating it for a schema change is not user-data loss if backfill can repopulate, though it is operationally disruptive.
- The code already embraces overfetch as a recall strategy (`OVERFETCH_MULTIPLIER`, `CHUNK_COLLAPSE_FACTOR`) and logs under-fill for Track A collapse.

### What works against this

- The current `chunk_vec` virtual table has only `chunk_id` and `embedding`; sqlite-vec cannot apply project/session/path/date predicates during KNN unless those fields are present as vec0 metadata or partition columns.
- Existing scope semantics include `LIKE` for session prefix and path substring. sqlite-vec metadata filters support equality and inequality operators, not `LIKE`, so full predicate pushdown cannot preserve current session/path semantics without schema or semantic changes.
- Date filters are stored on `branches.started_at`, while KNN rows are chunks. Pushdown would require duplicating branch-level metadata into `chunk_vec` and keeping it synchronized.
- Track A has two lossy stages after KNN: best-chunk-per-branch rollup and per-session dedup. Fixing scope filtering does not eliminate those underfill modes.

## Options Evaluated

### Option A: Adaptive filtered KNN overfetch/retry in `execute_chunk_knn()`

**How it works**: Keep the current `chunk_vec` schema. Teach `execute_chunk_knn()` to request a larger KNN window when scope filters are active or when post-filtering underfills. For each attempt, run the same sqlite-vec KNN query with `k = current_k`, apply the existing join + `scope_filter_clause()` filter, preserve distance order, and stop when enough filtered chunks are found or a ceiling is reached.

For Track B, “enough” should be at least `max_results`, not merely the current overfetch top-k. That implies either passing a `target_results` parameter into `execute_chunk_knn()` or having `search_messages()` call it with a requested filtered target. For Track A, enough is harder because chunks collapse to branches and sessions after `get_vec_chunk_ids()` and RRF. A practical first version can ask for the existing `top_k` filtered chunks and keep `CHUNK_COLLAPSE_FACTOR`; a stronger version can let `get_vec_chunk_ids()` continue retrying until it sees enough distinct branches or reaches the ceiling.

The retry ceilings should be explicit and conservative. For example: start at existing `top_k`; when filters are active, grow by 4x or 5x up to a cap such as `max(top_k, 1000)` or all vector rows for very small databases. Optionally count scoped candidate chunks first and cap to that count. This preserves API behavior and avoids schema migrations.

**Pros**:
- Fixes the immediate correctness hole for all current filters, including `LIKE` path/session filters that sqlite-vec metadata cannot push down.
- Centralized change: mostly `search_vector.py`, with small call-site changes if `target_results`/`target_branches` is added.
- No schema migration, no re-embedding, no new dependency, no change to CLI flags or output contracts.
- Easy to regression-test using current test infrastructure (`make_vec_conn()`, deterministic fake vectors, small `top_k`).

**Cons**:
- Still approximate when the in-scope match lies beyond the retry ceiling. The ceiling becomes a correctness/performance knob.
- Selective filters can be expensive: repeated global KNN scans with larger `k` may approach whole-table search.
- If there is no in-scope match, the implementation may do more work before returning empty.
- Need care to preserve `sqlite3.Error` degradation and non-DB bug propagation.

**Effort estimate**: Medium. Most work is designing a clean retry API and writing adversarial tests for each filter type.

**Dependencies**: None.

### Option B: Predicate pushdown via sqlite-vec metadata columns

**How it works**: Recreate `chunk_vec` with metadata copied from relational tables, such as `project_name`, `session_uuid`, `cwd`, `started_at`, `is_active`, `embedding_version`, and possibly `embedding_model`. Insert/update vector rows with that metadata so KNN queries can include metadata predicates directly in the `WHERE` clause. sqlite-vec docs state metadata columns can be included in KNN `WHERE` clauses and are applied during KNN, with support for `=`, `!=`, `<`, `<=`, `>`, `>=`.

This can push down exact project equality and date ranges naturally. It can also push down current embedding version/model and active status if duplicated. However, current session and path filters use prefix/substr `LIKE`, and sqlite-vec metadata docs explicitly do not support `LIKE` as a metadata constraint. Preserving current semantics would still require post-filtering or a changed filter model.

**Pros**:
- Best performance and recall for predicates that map to sqlite-vec metadata operations, especially project equality and date ranges.
- Removes some join-after-KNN filtering for version/model/active/project/date if metadata is kept current.
- Gives a cleaner long-term vector index shape if filtered vector search becomes important.

**Cons**:
- Does not fully solve Issue 100 for session prefix and path substring without a fallback strategy.
- Large integration surface: `chunk_vec` DDL, `upsert_chunk_vec()` / `write_chunk_embedding()`, any test helpers, vector self-heal, deletion triggers, possibly backfill status and migration behavior.
- Duplicates relational metadata into a derived vector table, creating synchronization risk when sessions/branches are updated after chunk vectors are written.
- sqlite-vec is pinned at `0.1.9`; docs fetched are for `0.1.10-alpha.4`, though the v0.1.9 release notes mention metadata text-column fixes, indicating metadata existed by then. Exact behavior should still be verified against the pinned wheel before relying on it.

**Effort estimate**: Large. This is closer to a vector-index schema redesign than a localized bug fix.

**Dependencies**: No new library, but relies more heavily on sqlite-vec vec0 metadata semantics.

### Option C: Scope-first candidate set plus chunk-id filtering

**How it works**: First query SQLite relational tables for all in-scope current chunk IDs, then restrict KNN to those chunk IDs. In an ideal sqlite-vec API this would be `chunk_id IN (...)` inside the KNN query, but the current vec0 table only has a primary key and docs do not present arbitrary `IN` pushdown as a supported KNN filter. A fallback variant would run global KNN with adaptive overfetch and stop once all scoped candidate IDs are covered.

Another variant is brute-force distance computation over scoped vectors using sqlite-vec scalar functions, but `chunk_vec` is a vec0 virtual table and the stored embedding column is not selected in current code. Implementing manual distance search would likely require storing duplicate vectors in a regular table or changing schema.

**Pros**:
- Conceptually matches the desired semantics: rank only the scoped universe.
- If sqlite-vec supported efficient primary-key/partition constraints for this shape, it would be exact.

**Cons**:
- Not directly supported by the current schema or documented vec0 usage.
- Large `IN` lists are awkward and may not be optimized by vec0 as prefilters.
- Manual scalar-distance search would require vector storage changes and likely worse performance.

**Effort estimate**: Medium to Large, depending on whether experiments prove `chunk_id IN (...)` works as true KNN prefilter. As a research-backed implementation choice, it is riskier than adaptive retry.

**Dependencies**: None beyond sqlite-vec, but may require pinned-version experiments.

## Concerns

### Technical risks

- **Exact recall vs bounded work**: Adaptive retry improves recall but is only exact if the ceiling reaches the scoped corpus size. For “best recall,” the implementation should either count scoped chunks and use that as a cap when filters are active, or document/log when the cap is hit.
- **Track A distinctness**: `get_vec_chunk_ids()` rolls up to one chunk per branch, then `search_sessions()` dedups by session. A filtered chunk count target may still underfill session cards. Branch/session-aware retry is more correct but more complex.
- **Error semantics**: `execute_chunk_knn()` currently catches `sqlite3.Error` and returns `[]`; tests assert non-DB errors propagate through `get_vec_chunk_ids()`. The fix should preserve that boundary.
- **Ordering**: Track B must remain nearest-distance-first after filtering; Track A vector branch order should remain best chunk per branch in distance order before RRF.

### Complexity risks

- Adding both predicate pushdown and retry creates two paths to reason about. If a metadata-schema path is chosen later, keep a fallback for non-pushdownable filters rather than duplicating filter semantics in several places.
- Counting scoped chunks/branches before retry adds another query using the same filters; if duplicated rather than shared, it can drift from `scope_filter_clause()`.

### Maintenance risks

- If `chunk_vec` metadata is introduced, every future change to project/session/branch scope semantics must update both relational schema logic and vector metadata write paths.
- Retry constants can become magic numbers. They should be named, documented, and covered by tests that force behavior with small caps/top-k.

## Open Questions

- [ ] What ceiling is acceptable for “best recall”? The code can make filtered searches exact by allowing KNN `k` to grow to the count of current in-scope chunks, but on large histories this may be expensive.
- [ ] Should Track A retry until it has enough filtered chunks, distinct branches, or distinct sessions? Current code already accepts some underfill from branch/session collapse, but Issue 100 is specifically about scope filtering.
- [ ] Does sqlite-vec `0.1.9` support metadata constraints exactly as current docs describe? Web docs for vec0 metadata confirm the concept and mention supported operators, but are published as `0.1.10-alpha.4`; v0.1.9 release notes mention metadata text-column behavior, suggesting support exists, not proving all desired semantics in the pinned version.

## Recommendation

Recommend **Option A: adaptive filtered KNN overfetch/retry**, with a design that can later accommodate metadata pushdown for exact/economic filters.

Reasoning: Issue 100 is a correctness bug across all scope filters. Predicate pushdown is attractive for project/date, but it cannot preserve current session prefix and path substring semantics because sqlite-vec metadata KNN constraints do not support `LIKE`. A metadata-only fix would either leave part of the bug in place or change user-visible semantics. Adaptive retry is the only option that fits the current schema and covers project, session, path, and date uniformly.

For “best recall,” avoid a tiny fixed ceiling. A strong implementation would:

1. Detect when scope/date filters are active, or when filtering/version/active checks underfill.
2. Compute the count of eligible scoped current chunks with the same join and `scope_filter_clause()`.
3. Grow KNN `k` until either enough filtered results are found or `k` reaches that eligible count.
4. Preserve distance ordering and existing degradation semantics.
5. For Track A, consider a branch-aware target in `get_vec_chunk_ids()` so retries stop based on distinct branches rather than raw chunks.

This can be exact for filtered searches when the cap is the scoped eligible count. It may be slower for highly selective filters, but the user explicitly prioritizes recall correctness.

### Concrete affected files

- `src/ccrecall/search_vector.py`
  - Refactor `execute_chunk_knn()` to separate “run vec KNN for k” from “filter/hydrate chunk ids to branch ids.”
  - Add adaptive retry and possibly `target_results`, `max_k`, or `retry_policy` parameters.
  - Add/count eligible scoped chunks using the same version/model/active and `scope_filter_clause()` predicates.
  - Keep return shape `list[tuple[int, int, float]]` and distance ordering.
- `src/ccrecall/search_conversations.py`
  - Pass the actual desired `max_results` target for `search_messages()`.
  - Optionally pass a distinct branch/session target for `get_vec_chunk_ids()` / Track A.
  - Keep existing ranked/fallback behavior.
- `src/ccrecall/search_query.py`
  - Likely keep `scope_filter_clause()` unchanged. If a count helper is added, ensure it reuses this function rather than duplicating filter SQL.
- `src/ccrecall/search_cli.py` and `src/ccrecall/cli/commands.py`
  - Probably no semantic change. Update comments/help only if implementation introduces a caveat or performance note.
- `tests/test_search.py`
  - Add adversarial KNN tests for post-top-k filtering failure and recovery.

### Tests to add/update

Add tests with deterministic vectors and deliberately small `top_k`/`max_results` so the global nearest chunk is out of scope and the in-scope chunk is just beyond the initial KNN window:

1. `execute_chunk_knn` / `search_messages` with `projects=[...]`: out-of-project nearest chunk first, in-project chunk second; filtered search returns in-project snippet.
2. Same for `session_id` prefix, including prefix behavior (`sess-target` matches, unrelated closer chunk does not).
3. Same for `path` substring (`/worktree-target`), proving current `LIKE '%...%'` semantics are preserved.
4. Same for `before`/`after`, proving branch-level `started_at` filters recover the in-range chunk beyond initial global KNN.
5. Stale/current chunk interaction: a stale nearest chunk should not prevent a current farther chunk from being found after retry.
6. Track A regression: `search_sessions()` still returns a scoped session card through vector fusion when the scoped chunk is outside the initial global KNN window; preserve RRF/order contracts where keyword hits also exist.
7. Error-boundary regression: sqlite3 errors inside the new retry/count path still degrade to `[]`; non-DB exceptions still propagate.

## Sources

- sqlite-vec KNN docs: https://alexgarcia.xyz/sqlite-vec/features/knn.html
- sqlite-vec vec0 metadata docs: https://alexgarcia.xyz/sqlite-vec/features/vec0.html
- sqlite-vec v0.1.9 release notes: https://github.com/asg017/sqlite-vec/releases/tag/v0.1.9
