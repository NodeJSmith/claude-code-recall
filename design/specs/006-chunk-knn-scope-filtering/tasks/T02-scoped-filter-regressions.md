---
task_id: "T02"
title: "Cover scoped filtered KNN recall"
status: "planned"
depends_on: ["T01"]
implements: ["FR#2", "FR#3", "FR#4", "FR#5", "FR#6", "AC#1", "AC#2", "AC#3", "AC#4", "AC#6"]
---

## Summary
Add adversarial vector-search regressions for every current scope filter. These tests should prove that project, session-prefix, path-substring, before/after, and combined predicates recover farther in-scope chunks when nearer global KNN rows are out of scope. The task locks the SQL predicate composition and parameter ordering to the existing `scope_filter_clause()` semantics.

## Target Files
- modify: `tests/test_search.py`
- read: `src/ccrecall/search_vector.py`
- read: `src/ccrecall/search_query.py`
- read: `tests/conftest.py`

## Prompt
In `tests/test_search.py`, add sqlite-vec-backed regression tests that seed multiple projects, sessions, paths, branch start dates, and chunks with deterministic vectors. Each regression should set `top_k` small enough that the initial global KNN window is occupied by out-of-scope nearer chunks, then assert that the farther in-scope current chunk is returned after T01's adaptive retry.

Cover project equality with `p.name IN (...)`, session prefix matching with escaped `sessions.uuid LIKE ? ESCAPE '\\'`, path substring matching with escaped `sessions.cwd LIKE ? ESCAPE '\\'`, branch-level strict `branches.started_at < before` and `branches.started_at > after`, and at least one combined-scope case such as project plus path or session prefix plus date. Keep these as local pytest regressions; do not alter CLI help, output formatting, schema, or embedding generation.

Use existing fixtures and helpers when possible, but it is acceptable to add a small test helper in `tests/test_search.py` if it keeps the scoped cases readable. Ensure seeded chunks have current `EMBEDDING_VERSION` and `EMBEDDING_MODEL` unless the test intentionally exercises currentness filtering.

## Focus
- `scope_filter_clause()` is the source of truth and assumes aliases `s`, `p`, and `b`; tests should fail if param ordering drifts.
- Existing date filter tests at `tests/test_search.py:1851` cover keyword and one message-search date case; this task specifically needs vector underfill regressions where valid rows are beyond initial KNN.
- Existing `_seed_branch_with_chunk()` at `tests/test_search.py:628` only seeds one project/path; scoped tests may need a richer helper that accepts project, cwd, active state, and started_at.
- sqlite-vec availability is already guarded with `@pytest.mark.skipif(not vec_available(sqlite3.connect(":memory:")), reason="sqlite-vec not available")` in this file.

## Verify
- [ ] FR#2: Project-scoped vector filtering still uses project name equality semantics and returns only matching projects.
- [ ] FR#3: Session-scoped vector filtering still uses escaped UUID prefix semantics and recovers a farther matching session chunk.
- [ ] FR#4: Path-scoped vector filtering still uses escaped cwd substring semantics and recovers a farther matching path chunk.
- [ ] FR#5: Date-scoped vector filtering still uses strict branch-level `started_at` before/after comparisons.
- [ ] FR#6: Scoped regressions continue to require current chunk embedding version/model and active branches.
- [ ] AC#1: `uv run pytest tests/test_search.py -q` includes a project-scoped vector regression beyond the initial KNN window.
- [ ] AC#2: `uv run pytest tests/test_search.py -q` includes a session-prefix vector regression beyond the initial KNN window.
- [ ] AC#3: `uv run pytest tests/test_search.py -q` includes a path-substring vector regression beyond the initial KNN window.
- [ ] AC#4: `uv run pytest tests/test_search.py -q` includes before and after vector regressions with strict branch-started semantics beyond the initial KNN window.
- [ ] AC#6: `uv run pytest tests/test_search.py -q` includes a combined-scope regression proving predicate composition and parameter ordering.
