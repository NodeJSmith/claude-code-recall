---
task_id: "T04"
title: "Type insights and split build_insights/build_trends"
status: "planned"
depends_on: ["T01", "T02"]
implements: ["FR#2", "FR#3", "FR#5", "FR#7", "FR#8", "AC#6", "AC#7"]
---

## Summary
Introduce typed `Insight`/`Solution` dataclasses, split `_build_insights` (~332 lines) into one builder per signal returning `Insight | None`, split `build_trends` (~227 lines) into focused helpers, and consume the T02 cost helper in `_window_kpis`. Serialize insights to dicts at the `build_insights_and_trends` return boundary so consumers see the unchanged dict shape. The T01 golden pins for `build_insights_and_trends` and `build_trends` must stay green.

## Target Files
- modify: `src/ccrecall/token_insights.py`
- read: `src/ccrecall/token_parser.py`
- read: `tests/test_token_insights.py`
- read: `design/specs/001-token-subsystem-refactor/design.md`

## Prompt
Refactor `src/ccrecall/token_insights.py` per the design doc's `## Architecture → token_insights.py` section.

**Typed insights:**
1. Add `@dataclass` `Solution` (fields: `action: str`, `detail: str`, `claudemd_rule: str | None`, `estimated_savings_usd: float`) and `Insight` (fields mirroring the current dict keys: `title`, `severity`, `finding`, `root_cause`, `waste_tokens: int`, `waste_usd: float`, `solution: Solution`, `priority: str = ""`). No field docstrings — annotations are the documentation. Place near the top after the constants.
2. Split `_build_insights` into one `build_<signal>_insight(...) -> Insight | None` per current `if`-block: `cache_cliffs`, `max_token_stops`, `bash_antipatterns`, `redundant_reads`, `edit_retries`, `thinking`, `idle_gap`, `cost_concentration`, `context_overhead`. Each returns `None` when its signal is below threshold/zero, else a fully-populated `Insight`. **Signature note:** most take a single count plus context, but `idle_gap` needs both `response_time_dist` **and** `dominant_cache_tier` (the over-TTL bucket sum depends on the tier), and `context_overhead` keys off `context_seg_summary`'s `base_overhead_pct`/`avg_base_ctx`. Plan signatures per their actual inputs — do not force a uniform `(count, ...)` shape.
3. The `_waste_usd` and `_severity` closures are pure; lift them to module-level functions taking the rates/sessions they need (or keep as small locals passed in). Preserve their exact arithmetic.
4. `_build_insights` becomes: call each builder, collect non-`None` `Insight`s into a list, **sort by `(waste_usd > 0, waste_usd)` descending**, then assign `priority` P0 (index <2) / P1 (<5) / P2 (else) by enumerate index — exactly as today. The sort and banding run after collection, not inside builders.
5. `_insights_to_findings` / `_insights_to_recommendations` consume typed `Insight` fields (`i.title`, `i.solution.action`, etc.) instead of string keys.
6. In `build_insights_and_trends`, serialize the `Insight` list to dicts at the return boundary (`dataclasses.asdict`, which recurses into `Solution`) so `out["insights"]` is the same list-of-dicts shape as today. Verify the serialized key order/content matches the golden pin.

**build_trends split:**
7. Lift the nested `_window_kpis` to a module-level helper taking `cur` and the where-clause. Inside it, replace the inline cost loop with the T02 helper (the `_window_kpis` cost loop selects `model, SUM(input), SUM(output), SUM(cache_read), SUM(cache_creation), SUM(ephem_5m), SUM(ephem_1h)` — 7 columns, token indices `[1,2,3,4,5,6]`, contiguous; coalesce with `or 0`). **Remove the inline loop.**
8. Extract the new/retired-set diff into one helper called twice (skills, hooks) — both compute `current_set - prior_set` / `prior_set - current_set` over the same two windows from `turn_tool_calls.skill_name` and `hook_executions.hook_command` respectively. Parameterize the table/column/window.
9. Extract the hook-perf comparison (current vs prior avg ms per hook) into its own helper.
10. Hoist the repeated window-clause strings to module-level constants (e.g. `CURRENT_WINDOW_CLAUSE`, `PRIOR_WINDOW_CLAUSE`) and reuse — but preserve the exact SQL text (some call sites use `sm.first_turn_ts`, others `datetime(sm.first_turn_ts)`; check each before merging — do NOT merge two clauses that differ in text).

Run `uv run pytest -q tests/test_token_insights.py` then the full suite `uv run pytest -q`.

## Focus
- **The `asdict` serialization is the riskiest correctness point.** The current dicts are built with a specific key set and nesting; `dataclasses.asdict(insight)` must produce a dict equal to what the old `if`-block emitted, including `priority` (set post-sort) and the nested `solution` dict. Confirm against the T01 golden pin. If `asdict` reorders or the comparison is order-sensitive, dict equality in Python is order-insensitive — but if the pin serializes to JSON anywhere, watch key order. The golden pin asserts dict equality, which is order-insensitive, so this should be safe.
- Preserve the `sessions = kw["total_sessions"] or 1` guard and the `avg_input_rate`/`avg_output_rate` lookups that feed `_waste_usd`.
- The weighted avg cost-rate computation at the top of `build_insights_and_trends` (lines 78-86) stays — it computes `avg_input_cpm`/`avg_output_cpm` from `model_split` and feeds `_build_insights`. Don't fold it into a builder.
- `build_trends` short-circuits to `{}` when `current` is `None` (no recent sessions) — preserve that early return before the metrics loop.
- **Same-file edits:** only T04 edits `token_insights.py`. T02 (token_parser helper) lands first via `depends_on`. No contention.
- The existing `tests/test_token_insights.py` tests (`TestBuildTrends`, `TestBuildInsights`) must stay green unchanged — if one breaks, that signals a behavior change, which is a red flag, not something to "fix" by editing the test.
- Personal style: dataclasses with typed fields, no `_`-prefixed methods, functions over methods, constants at top.

## Verify
- [ ] FR#2: `build_insights_and_trends(...)` returns a dict equal to pre-refactor output for identical inputs — the T01 golden pins (all-off and multi-insight) pass.
- [ ] FR#3: `build_trends(conn)` returns a dict equal to pre-refactor output — the T01 `build_trends` golden pin passes.
- [ ] FR#5: Each per-signal builder's serialized `Insight` equals the dict its `if`-block produced before (verified via the golden pin).
- [ ] FR#7: Every function in `token_insights.py` is ≤50 lines except thin orchestrators (`_build_insights`, `build_trends`, `build_insights_and_trends`) that only sequence named helpers.
- [ ] FR#8: `build_insights_and_trends`, `build_trends` remain importable with unchanged signatures; `token_output`'s import of `build_insights_and_trends` is unaffected.
- [ ] AC#6: A manual/ruff scan confirms no oversized function remains in `token_insights.py`.
- [ ] AC#7: The full test suite passes with zero failures.
