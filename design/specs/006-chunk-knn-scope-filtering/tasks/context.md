# Context: Chunk-KNN Scope Filtering

## Problem & Motivation
Filtered vector recall currently runs sqlite-vec KNN globally once, then applies project, session, path, date, active-branch, and current-embedding filters afterward. If the nearest global chunks are outside the requested scope, farther valid in-scope chunks never enter the relational filter window. This affects both `ccrecall search` session cards and `ccrecall search-messages` snippets. The fix must recover scoped current chunks without changing CLI output, schema, embedding format, or keyword fallback contracts.

## Visual Artifacts
None.

## Key Decisions
1. Keep filtering in `src/ccrecall/search_vector.py` around `execute_chunk_knn()` instead of changing sqlite-vec metadata or schema.
2. Serialize the query vector once, then retry sqlite-vec with a larger `k` only when the filtered result set underfills the requested target.
3. Use `scope_filter_clause()` as the single source of truth for project, session-prefix, path-substring, before, and after SQL semantics.
4. Use total `chunk_vec` row count as the exact retry ceiling because scoped eligible count cannot prove where scoped chunks appear in global KNN order.
5. Keep DB degradation narrow: `sqlite3.Error` inside vector retrieval returns empty vector results; non-DB programming errors still propagate.
6. Do not add extra count queries for unfiltered searches that fill from the initial KNN window.

## Constraints & Anti-Patterns
- Do not change the `chunk_vec` schema, add metadata columns, or require re-embedding.
- Do not change CLI flags, help text, JSON output, markdown output, or search result shapes.
- Do not add keyword fallback to `search-messages`.
- Do not redesign Track A branch/session collapse; keep `get_vec_chunk_ids()` as best-chunk-per-branch over raw chunk hits.
- Do not catch broad exceptions in `search_vector.py`; only `sqlite3.Error` should degrade.
- Do not bypass `scope_filter_clause()` or duplicate scope predicate semantics by hand.
- Use no lazy imports and no `from __future__ import annotations`.

## Design Doc References
- ## Problem — explains the global-KNN-then-filter bug and affected search surfaces.
- ## Functional Requirements — enumerates unchanged scope semantics, currentness checks, ordering, fusion, and error handling.
- ## Architecture — describes adaptive filtered KNN retry, count helpers, retry ceiling, and caller targets.
- ## Test Strategy — lists required pytest regressions in `tests/test_search.py`.
- ## Impact — names changed files and behavioral invariants.

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

This pattern should remain, but the `execute_chunk_knn()` call should pass `target_results=max_results`.

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
