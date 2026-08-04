# Design: Chunk-KNN Scope Filtering

**Date:** 2026-08-03
**Status:** archived
**Scope-mode:** hold
**Research:** `design/research/2026-08-03-chunk-knn-scope-filtering/research.md`

## Problem

Filtered vector recall is unreliable. `execute_chunk_knn()` asks sqlite-vec for the globally nearest `top_k` chunks, then applies project, session, path, date, active-branch, and embedding-currentness filters afterward. If the nearest global chunks are outside the requested scope, valid in-scope chunks beyond that initial KNN window are never considered.

This affects both user-facing vector paths: `ccrecall search`, which fuses keyword branch hits with vector branch hits into session cards, and `ccrecall search-messages`, which returns exchange snippets directly from chunk-KNN.

## Goals

- Filtered vector searches recover valid in-scope current chunks even when those chunks are outside the initial global KNN window.
- Current scope semantics remain unchanged for project equality, session-prefix matching, path-substring matching, and branch-level before/after date filtering.
- Existing ranking, output shape, keyword fallback, and vector degradation contracts remain unchanged.
- The fix stays code-only: no schema migration, no vector-table metadata redesign, and no re-embedding requirement.
- Tests cover every current scope filter plus stale/current filtering and error-boundary behavior.

## Non-Goals

- Do not redesign `chunk_vec` with sqlite-vec metadata columns.
- Do not change CLI flags, help text, or JSON/markdown output contracts.
- Do not add keyword fallback for `search-messages`.
- Do not solve unrelated Track A underfill caused by branch/session collapse beyond avoiding the filter-induced miss.
- Do not change embedding generation, backfill behavior, or stored vector format.

## User Scenarios

### ccrecall user: Scoped Search

- **Goal:** Find relevant conversation history inside a specific project, session, path, or strict branch-started before/after boundary.
- **Context:** The closest global vector matches are outside the requested scope, but a valid in-scope match exists farther down the KNN order.

#### Filtered Session Search

1. **Runs `ccrecall search` with a scope filter**
   - Sees: the same session-card output fields and ranked/unranked indicators as today.
   - Decides: whether the returned session is relevant.
   - Then: vector candidates include valid in-scope chunks beyond the first global KNN window before Track A rollup, RRF fusion, and session dedup run.

#### Filtered Message Search

1. **Runs `ccrecall search-messages` with a scope filter**
   - Sees: snippet results in nearest-distance order, capped by `max_results`.
   - Decides: whether to resume or inspect the matched exchange.
   - Then: the snippet path returns in-scope chunks even when nearer global chunks were filtered out.

## Functional Requirements

- **FR#1** `execute_chunk_knn()` must not permanently exclude valid in-scope current chunks only because they fall outside the initial global KNN `top_k` window.
- **FR#2** Project filters must continue to use `p.name IN (...)` semantics through `scope_filter_clause()`.
- **FR#3** Session filters must continue to use escaped prefix matching against `sessions.uuid`.
- **FR#4** Path filters must continue to use escaped substring matching against `sessions.cwd`.
- **FR#5** Before/after filters must continue to compare branch-level `branches.started_at` values.
- **FR#6** Chunk currentness filtering must continue to require current `chunks.embedding_version`, current `chunks.embedding_model`, and active branches.
- **FR#7** `search_messages()` must preserve distance ordering and apply `max_results` after filtered KNN candidates are recovered.
- **FR#8** `search_sessions()` must preserve keyword/vector RRF fusion, best-chunk-per-branch rollup, per-session deduplication, and keyword fallback behavior.
- **FR#9** sqlite3 errors inside vector retrieval, including any new count or retry query, must degrade to empty vector results as today.
- **FR#10** Non-DB programming errors in vector retrieval must still propagate instead of being masked.

## Edge Cases

- No in-scope current chunks exist: return empty vector results without changing `ranked` semantics.
- Nearest chunks are stale, inactive, wrong project, wrong session prefix, wrong path, outside strict before/after branch-started bounds, or wrong embedding model/version: continue retrieval until enough filtered results are found or the global KNN candidate corpus is exhausted.
- `session_id` values containing LIKE metacharacters remain escaped by `escape_like()` through `scope_filter_clause()`.
- `path` values containing LIKE metacharacters remain escaped by `escape_like()` through `scope_filter_clause()`.
- Track B receives more filtered KNN hits than requested: hydrate only the nearest `max_results` snippets.
- Track A receives multiple chunks from the same branch: keep the nearest chunk per branch before fusion.
- Track A receives multiple branches from the same session: existing `dedup_by_session()` behavior still keeps the first ranked branch.
- sqlite-vec or relational filter queries raise `sqlite3.Error`: callers see the same degradation behavior they see for current vector DB errors.

## Acceptance Criteria

- **AC#1** `uv run pytest tests/test_search.py -q` includes a regression where a project-scoped vector search returns an in-project chunk that is beyond the initial global KNN window.
- **AC#2** `uv run pytest tests/test_search.py -q` includes a regression where a session-prefix vector search returns a matching session chunk beyond the initial global KNN window.
- **AC#3** `uv run pytest tests/test_search.py -q` includes a regression where a path-substring vector search returns a matching path chunk beyond the initial global KNN window.
- **AC#4** `uv run pytest tests/test_search.py -q` includes before/after date-filter regressions where a branch satisfying strict `started_at < before` or `started_at > after` semantics beyond the initial global KNN window is returned.
- **AC#5** `uv run pytest tests/test_search.py -q` includes a stale-currentness regression where stale or inactive nearer chunks do not prevent a farther current active chunk from being returned.
- **AC#6** `uv run pytest tests/test_search.py -q` includes a combined-scope regression, such as project plus path or session plus date, proving predicate composition and parameter ordering are correct.
- **AC#7** `uv run pytest tests/test_search.py -q` confirms `search_messages()` preserves nearest-distance ordering and `max_results` after filtered retry.
- **AC#8** `uv run pytest tests/test_search.py -q` confirms `search_sessions()` can surface a scoped vector result through Track A fusion when that result is beyond the initial global KNN window.
- **AC#9** `uv run pytest tests/test_search.py -q` confirms sqlite3 errors in the new vector retrieval path still degrade and non-DB errors still propagate.
- **AC#10** `uv run pytest tests/test_search.py -q` includes a regression proving an unfiltered search that fills from the initial KNN window does not issue the new count queries.
- **AC#11** `uv run pytest` passes.

## Key Constraints

- Keep `scope_filter_clause()` as the source of truth for project, session, path, before, and after SQL semantics.
- Do not change the `chunk_vec` schema or require re-embedding.
- Do not catch broad exceptions in `search_vector.py`; only DB-level `sqlite3.Error` should degrade.
- Preserve current return shapes from `execute_chunk_knn()` and `get_vec_chunk_ids()`.
- Favor recall correctness over speed for filtered searches, while avoiding extra work for unfiltered searches that already fill their requested window.

## Dependencies and Assumptions

- Depends on local SQLite and the pinned `sqlite-vec==0.1.9` extension already used by the project.
- No network service, external API, or new package dependency is required.
- Query embedding remains handled by existing `embed_text()` gates in `search_conversations.py`; this change starts after a query vector already exists.
- Existing sqlite-vec KNN behavior supports increasing `k` on repeated calls with the same query vector.
- No schema migration is needed because all filtering metadata is already available through relational joins from `chunks` to `branches`, `sessions`, and `projects`.

## Architecture

Implement adaptive filtered KNN retry inside `src/ccrecall/search_vector.py`, centered on `execute_chunk_knn()`.

The current function has two logical phases in one body: run sqlite-vec KNN for `top_k`, then join/filter the returned chunk IDs through relational tables. The design should make those phases explicit so they can be repeated safely:

1. Serialize the query vector once.
2. Run the sqlite-vec KNN query for the current `k`.
3. Filter/hydrate returned chunk IDs through `chunks`, `branches`, `sessions`, and `projects`, using current embedding model/version, active-branch filtering, and `scope_filter_clause()`.
4. Preserve sqlite-vec distance order after filtering.
5. If the filtered result count is below the requested target and sqlite-vec has not exhausted the global KNN candidate corpus, increase `k` and retry.
6. Stop when enough filtered results are found, the KNN window reaches the total KNN candidate corpus, sqlite-vec returns fewer rows than requested, sqlite-vec returns no rows, or a DB error occurs.

`execute_chunk_knn()` should accept an explicit filtered target, such as `target_results: int | None = None`. When omitted, it can default to the existing `top_k` behavior for compatibility with current callers. `search_messages()` should pass `target_results=max_results` because snippets are sliced directly after raw KNN. `get_vec_chunk_ids()` should keep the existing chunk `top_k` target for Track A. That preserves the current branch/session-collapse tradeoff while the central retry prevents scope-induced misses; a branch-aware retry target is a separate future optimization, not part of this fix.

For exact filtered recall with the current schema, the retry ceiling must be the total global KNN candidate corpus, not the eligible scoped chunk count. Because KNN is still global and scope/currentness filters are still applied after sqlite-vec returns rows, many nearer out-of-scope, stale, inactive, or wrong-model chunks can occupy the first `k` rows. A scoped eligible count proves how many valid chunks exist, but it does not prove those chunks have appeared in the global KNN window.

Add two small helpers in `search_vector.py`. Run these counts lazily: use the initial KNN/filter pass first, and only count when that pass underfills or scope/currentness filtering means retry may be needed. When counts are needed, compute each count at most once per `execute_chunk_knn()` call and reuse the values across retry iterations. Unfiltered searches that fill from the initial window should not pay for count queries.

- A total KNN candidate count helper, based on `chunk_vec`, used as the exact-recall ceiling.
- An eligible scoped current vector-backed chunk count helper, based on `chunks` joined to `chunk_vec`, `branches`, `sessions`, and `projects`, plus the same currentness checks and `scope_filter_clause()` predicates used by filtering. Use this to short-circuit when no scoped vector-backed chunks exist and to avoid asking for more filtered results than can exist.

If either helper fails with `sqlite3.Error`, return `[]` from `execute_chunk_knn()` to preserve current vector-degradation behavior.

Use a named growth policy for retries: `KNN_RETRY_MULTIPLIER = 4`. Start with the caller-provided `top_k`; on underfill, grow by that multiplier toward the total KNN candidate ceiling, clamped so the final retry asks for exactly the ceiling rather than overshooting it. This aligns with the existing `OVERFETCH_MULTIPLIER = 4`, keeps the common path cheap, and bounds the number of retry queries. Avoid inline magic numbers. The initial KNN size should remain the caller-provided `top_k`, preserving existing overfetch choices in `search_conversations.py`:

- `search_sessions()` still computes `chunk_top_k = max(max_results * OVERFETCH_MULTIPLIER * CHUNK_COLLAPSE_FACTOR, OVERFETCH_FLOOR)`.
- `search_messages()` still computes `top_k = max(max_results * OVERFETCH_MULTIPLIER, OVERFETCH_FLOOR)` but passes `target_results=max_results` to avoid returning fewer snippets solely because out-of-scope chunks occupied the initial KNN window.
- `get_vec_chunk_ids()` keeps its existing raw chunk target from Track A's `chunk_top_k`; this design does not add a distinct-branch or distinct-session retry target.

The architecture intentionally does not push filters into sqlite-vec metadata. Metadata pushdown cannot preserve current session-prefix and path-substring `LIKE` semantics, so it would either leave part of the bug unresolved or change user-visible behavior.

## Implementation Preferences

- Reuse `scope_filter_clause()` for both eligible-count and candidate-filter queries; the eligible-count query must count only chunks that have a corresponding `chunk_vec` row.
- Keep all new vector retry/count logic in `search_vector.py` unless a tiny shared helper becomes clearly necessary.
- Preserve `execute_chunk_knn()` return shape: `list[tuple[int, int, float]]` as `(chunk_id, branch_id, distance)`.
- Preserve `get_vec_chunk_ids()` return shape: `list[tuple[int, float, int]]` as `(branch_id, distance, chunk_id)`.
- Keep DB degradation narrow: catch `sqlite3.Error`, return empty vector results, and let non-DB exceptions propagate.
- Prefer small private helpers over introducing new cross-module dataclasses or public API types.

## Replacement Targets

- Replace the current single-pass “global KNN once, then post-filter once” strategy in `src/ccrecall/search_vector.py:execute_chunk_knn()` with adaptive filtered retry.
- No existing module, CLI command, schema element, or test category is removed.

## Alternatives Considered

### Predicate pushdown via sqlite-vec metadata

Rejected for this fix. It is attractive for project equality and date ranges, but it cannot preserve current session-prefix and path-substring `LIKE` semantics. It also requires vector table schema changes, metadata synchronization, and likely re-embedding/backfill handling.

### Scope-first candidate set with chunk-id filtering

Rejected for this fix. It conceptually ranks only the scoped universe, but the current sqlite-vec usage does not provide a proven efficient `chunk_id IN (...)` KNN prefilter. Manual distance computation would require vector storage changes or a separate brute-force path.

### Scoped-count retry ceiling

Rejected because it is incorrect with global KNN plus post-filtering. If the scoped corpus has 3 current chunks but 100 out-of-scope chunks are closer to the query, a global `k=3` query can still return zero in-scope chunks. The scoped count is useful as a target and no-match optimization, not as an exact-recall ceiling.

### Fixed overfetch ceiling

Rejected as the primary design because it would still miss valid in-scope matches beyond the ceiling. A named growth policy is acceptable, but the final ceiling for exact filtered recall must be the total global KNN candidate corpus.

## Test Strategy

### Required Test Types

Unit/integration-style pytest coverage is required. The repo supports pytest in `tests/` and already uses in-memory SQLite and sqlite-vec fixtures through `make_vec_conn()`. No E2E infrastructure exists for this CLI path, and this change does not need browser or service-level tests.

### Existing Tests to Adapt

- `tests/test_search.py`: extend existing vector-search tests and fixtures.
- `TestExceptionNarrowing`: add or adapt coverage so the new count/retry path preserves sqlite3 degradation and non-DB propagation.
- Existing path/session/date filter tests should continue to pass unchanged for keyword paths.

### New Test Coverage

- Project filter: nearest global chunk is outside the requested project; farther in-project chunk is returned.
- Session filter: nearest global chunk has a nonmatching session; farther chunk with matching session prefix is returned.
- Path filter: nearest global chunk has a nonmatching `cwd`; farther chunk with matching path substring is returned.
- Date filters: nearest global chunks fail strict `started_at < before` or `started_at > after` bounds; a farther branch chunk satisfying those bounds is returned.
- Combined filters: at least one regression combines scope predicates, such as project plus path or session prefix plus date, to pin parameter ordering and clause composition.
- Currentness filter: stale or inactive nearer chunks do not prevent a farther current active chunk from being returned.
- Lazy count behavior: an unfiltered query that fills from the initial KNN/filter pass does not execute the new count helpers.
- Track B ordering: recovered snippets remain ordered by distance and sliced to `max_results`.
- Track A fusion: scoped vector candidate beyond the initial KNN window can still become a session card through existing fusion/dedup logic.
- Error boundary: sqlite3 errors in all new vector SQL return empty vector results; non-DB errors still propagate.

### Tests to Remove

No tests to remove.

## Documentation Updates

No documentation updates required. CLI behavior and help text remain unchanged.

## Convention Examples

### Narrow DB Error Degradation

**Source:** `src/ccrecall/search_vector.py`

```python
try:
    serialized = sqlite_vec.serialize_float32(query_vec)
    knn_rows = cursor.execute(
        "SELECT chunk_id, distance FROM chunk_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (serialized, top_k),
    ).fetchall()
except sqlite3.Error:
    return []
```

### Scope Filter Source Of Truth

**Source:** `src/ccrecall/search_query.py`

```python
scope_sql, scope_params = scope_filter_clause(
    projects=projects, session_id=session_id, path=path, before=before, after=after
)
sql += scope_sql
params.extend(scope_params)
```

### Track B Direct Snippet Slicing

**Source:** `src/ccrecall/search_conversations.py`

```python
raw = execute_chunk_knn(
    cursor, query_vec, top_k, projects=projects, session_id=session_id, path=path, before=before, after=after
)
if not raw:
    return [], True

ordered = raw[:max_results]
snippets = hydrate_snippets(cursor, ordered)
```

This example shows the current Track B pattern: raw KNN hits are sliced directly before hydration. The implementation should keep that slicing pattern but update the `execute_chunk_knn()` call as specified in Architecture so it passes `target_results=max_results`.

### Deterministic sqlite-vec Test Setup

**Source:** `tests/test_search.py`

```python
cursor.execute(
    """
    INSERT INTO chunks (branch_id, exchange_index, content_hash,
                        user_text, embedding_version, embedding_model)
    VALUES (?, ?, ?, ?, ?, ?)
    """,
    (branch_id, 0, f"hash-{uuid}", content, chunk_embedding_version, chunk_embedding_model),
)
chunk_id = cursor.lastrowid
if embed_vec is not None:
    cursor.execute(
        "INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, sqlite_vec.serialize_float32(embed_vec)),
    )
```

## Impact

### Changed Files

- Modify `src/ccrecall/search_vector.py`: refactor KNN execution/filtering into retryable helpers, add total KNN candidate counting, add eligible scoped chunk counting, add adaptive retry, preserve return shapes.
- Modify `src/ccrecall/search_conversations.py`: pass an explicit filtered target from `search_messages()`; keep Track A's existing chunk target behavior.
- Modify `tests/test_search.py`: add adversarial filtered vector recall regressions and error-boundary coverage.
- Read `src/ccrecall/search_query.py`: reuse `scope_filter_clause()`; no expected semantic change.
- Read `src/ccrecall/search_cli.py` and `src/ccrecall/cli/commands.py`: verify no output/help changes are needed.

### Behavioral Invariants

- `ccrecall search` returns session cards, not snippets or full transcripts.
- `ccrecall search-messages` returns snippet dictionaries and has no keyword fallback in this change.
- Project/session/path/before/after filter semantics remain identical to `scope_filter_clause()`.
- Track A fusion uses existing RRF behavior when vector and keyword paths are available.
- Track A vector-degradation behavior remains unchanged: pre-KNN gates such as unavailable model or unavailable `chunk_vec` still use the existing keyword path; sqlite3 errors caught inside `execute_chunk_knn()` still return empty vector candidates and continue through fusion with keyword IDs rather than forcing the explicit keyword-only path.
- Track B returns `ranked=False` only before KNN can run; if KNN runs and yields no rows, it returns `([], True)` as today.
- Existing JSON and markdown output fields remain unchanged.
- Existing embedding schema, model versioning, and backfill behavior remain unchanged.

### Blast Radius

The blast radius is limited to search retrieval. Both vector-enabled user-facing search commands benefit because they share `execute_chunk_knn()`. Keyword-only search, recent chats, import/sync, embeddings, schema migration, and hook hot paths should not change.

## Open Questions

- None.
