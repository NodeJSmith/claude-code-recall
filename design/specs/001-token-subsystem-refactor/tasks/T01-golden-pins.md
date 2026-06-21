---
task_id: "T01"
title: "Add golden characterization pins for the read-side functions"
status: "planned"
depends_on: []
implements: ["AC#1", "AC#2", "AC#3"]
---

## Summary
Before any structural change, add golden (full-dict-equality) characterization tests that capture the exact current output of `build_output`, `build_insights_and_trends`, and `build_trends`. These pins are the safety net for the entire refactor: every subsequent task must keep them green. They must pass on the **current, unrefactored** code — this task changes no production code, only adds tests.

## Target Files
- modify: `tests/test_token_output.py`
- modify: `tests/test_token_insights.py`
- read: `tests/conftest.py`
- read: `tests/token_helpers.py`
- read: `src/ccrecall/token_output.py`
- read: `src/ccrecall/token_insights.py`

## Prompt
Add golden characterization tests that pin the full output of the three read-side functions on the **existing** code (no production changes in this task).

1. **`build_output` golden** (in `tests/test_token_output.py`): call `build_output(populated_token_db)`, pop/exclude the `generated_at` key (it is `Instant.now()` and varies), and assert the **entire remaining dict** equals a captured expected value. Build the expected value by computing it once from the current code and inlining it, OR assert equality of `build_output` against a second `build_output` call's structure is NOT sufficient — capture the concrete expected dict. A clean way: snapshot the dict to a module-level constant in the test (a literal dict) so a future behavior drift fails loudly. Keep `populated_token_db` (the existing conftest fixture: 2 sessions, 3 turns) as the input.
2. **`build_insights_and_trends` golden** (in `tests/test_token_insights.py`): use the existing `_insight_kwargs(...)` helper. Add (a) an all-off case asserting the full returned dict, and (b) a multi-insight case — pass `cache_cliffs`, `max_token_stops`, `redundant_reads_count`, `edit_retries_count`, and `total_thinking` nonzero (with `total_output` nonzero so the thinking-pct math runs) against the empty `token_db` — and assert the full `insights`/`findings`/`recommendations` lists and `trends` value. Capture the concrete expected structure.
3. **`build_trends` golden** (in `tests/test_token_insights.py`): seed a DB via `import_session` with one current-window session (`days_ago=1`) and one prior-window session (`days_ago=9`) using the existing `_session_at` helper, then assert the **full** `build_trends(token_db)` dict (not just key presence). This is distinct from the existing `test_structure_with_recent_data`, which only checks keys — do not modify that test; add a new one.

Reference the design doc's `## Test Strategy → New Test Coverage` for which cases are required. Follow the pricing-derived-assertion convention from `context.md` if any dollar amounts appear — never hardcode a rate.

Run `uv run pytest -q tests/test_token_output.py tests/test_token_insights.py` and confirm all new tests pass on the current code.

## Focus
- Fixtures already exist in `tests/conftest.py`: `populated_token_db` (2 top-level sessions s1/s2, 3 turns, totals output=250/input=600/1 Read tool call) and `token_db` (empty). Helpers in `tests/token_helpers.py`: `token_session`, `token_turn`, `TOKEN_JNL`. `import_session` is from `ccrecall.token_analytics`.
- `build_output`'s output has 31 keys plus the spread from `build_insights_and_trends` (`insights`/`findings`/`recommendations`/`trends`). Capturing the full dict is what makes the later 22-builder split provably safe — partial-key assertions would let a dropped key slip through.
- The golden dicts will be verbose. That's intentional and acceptable for a pin — readability is secondary to completeness here.
- `generated_at` is the ONLY nondeterministic field in `build_output`. Everything else is a pure function of DB state. `build_trends` uses `datetime('now', ...)` windows — that is why the `_session_at(days_ago=...)` helper exists; the seeded timestamps are relative to now, so the windows are stable at test time.
- Do NOT touch production code in this task. If a golden capture is awkward to express, that is a test-authoring problem, not a reason to change `token_output`/`token_insights`.

## Verify
- [ ] AC#1: A new test captures `build_output(populated_token_db)` minus `generated_at` as a full-dict equality assertion and passes on current code.
- [ ] AC#2: New tests capture `build_insights_and_trends(...)` for both an all-off kwargs set and a multi-insight set, asserting full-dict equality, passing on current code.
- [ ] AC#3: A new test pins `build_trends(token_db)` full-dict output for a DB with current- and prior-window sessions, distinct from `test_structure_with_recent_data`, passing on current code.
