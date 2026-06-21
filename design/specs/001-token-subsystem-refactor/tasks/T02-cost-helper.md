---
task_id: "T02"
title: "Add shared cost-accumulation helper in token_parser"
status: "planned"
depends_on: ["T01"]
implements: ["FR#6", "AC#5"]
---

## Summary
Extract the repeated `get_pricing(model) → turn_cost(...)` accumulation into a single helper in `token_parser.py`, next to `turn_cost`/`get_pricing`. This is the foundational shared unit consumed by the token_output and token_insights refactors (T03, T04). Add a direct unit test proving it matches a hand-computed multi-model total. This task adds the helper and its test only — it does not yet migrate callers (T03/T04 do that as they refactor each module).

## Target Files
- modify: `src/ccrecall/token_parser.py`
- modify: `tests/test_token_parser.py`
- read: `src/ccrecall/token_output.py`
- read: `src/ccrecall/token_insights.py`

## Prompt
Add a module-level cost helper to `src/ccrecall/token_parser.py`, placed immediately after `turn_cost`. The contract: given a query row that carries a model column and the six token columns `turn_cost` expects, look up pricing via `get_pricing(row[model_idx])` and return that row's dollar cost via `turn_cost`. Because the token columns are **not contiguous in every query** (see below), the helper must take **explicit token-column indices**, not a slice.

Suggested shape (planning may adjust names, keep the contract):
```python
def row_cost(row: Sequence, *, model_idx: int, token_indices: Sequence[int]) -> float:
    """Price one grouped query row: pricing from row[model_idx], then turn_cost
    over the six token columns at token_indices."""
    pricing = get_pricing(row[model_idx])
    cols = [row[i] or 0 for i in token_indices]
    return turn_cost(*cols, pricing)
```
If a total-only convenience reads cleaner for the single-total callers, you may additionally add `sum_cost(rows, *, model_idx, token_indices) -> float` that sums `row_cost` over an iterable. Do not add anything the consumers won't use.

Add a unit test in `tests/test_token_parser.py` (new test class, alongside `TestTurnCost`) that covers **all three real caller layouts** — the helper must work for each, and a layout-specific off-by-one must fail the test (not slip through to be caught only by T03/T04's golden pins):
- Builds 2+ fake rows with **different models** (e.g. one opus-4-6, one sonnet) and known token columns.
- Asserts the accumulated total equals the sum of explicit `turn_cost(...)` calls with `get_pricing(model)` for each row — derive the expected value from the pricing dict, do NOT hardcode a dollar figure (see the pricing-derived-assertion convention in `context.md`).
- **Layout A — `model_split` (skip):** row `(model, inp, out, think, cr, cc, e5, e1)`, `model_idx=0`, `token_indices=[1, 2, 4, 5, 6, 7]`; assert `think` at index 3 does NOT enter the cost (set it to a large value and confirm the total is unchanged).
- **Layout B — `cost_by_day`/`cost_by_project` (offset model):** row `(group_key, model, inp, out, cr, cc, e5, e1)`, `model_idx=1`, `token_indices=[2, 3, 4, 5, 6, 7]`; proves the helper reads the model from index 1 and tokens from 2–7, not a hardcoded `model_idx=0`/`token_indices` starting at 1.
- **Layout C — `_window_kpis` (contiguous from 1):** row `(model, inp, out, cr, cc, e5, e1)`, `model_idx=0`, `token_indices=[1, 2, 3, 4, 5, 6]`; include a row with a `None` token column to confirm the `or 0` coalescing.

Run `uv run pytest -q tests/test_token_parser.py` and confirm green. Also run the full suite to confirm T01's pins still pass (this task adds code but changes no existing behavior).

## Focus
- **Column asymmetry (the whole reason for explicit indices):** In `token_output.build_output`, `model_split`'s query selects `model, SUM(input), SUM(output), SUM(thinking), SUM(cache_read), SUM(cache_creation), SUM(ephem_5m), SUM(ephem_1h)` — 8 columns — and calls `turn_cost(row[1], row[2], row[4], row[5], row[6], row[7], pricing)`, skipping `thinking` at `row[3]`. The other three loops (`cost_by_day`, `cost_by_project` in token_output; `_window_kpis` in token_insights) select the six token columns contiguously after their group key(s). The helper must serve all four.
- The `or 0` coalescing matters: `_window_kpis` passes `crow[i] or 0` (SUM can be NULL on empty groups). Preserve that — coalesce inside the helper as shown.
- `turn_cost`'s signature is `(input_tok, output_tok, cache_read, cache_creation, ephem_5m, ephem_1h, pricing)`. The `token_indices` order must map to exactly that argument order.
- Do not change `turn_cost` or `get_pricing` themselves — both have existing characterization tests (`TestTurnCost`, `TestGetPricing`) that must stay green.
- T03/T04 will edit `token_parser.py`'s consumers; T05 also edits `token_parser.py` (parse_session). Keep this change localized to the new helper so those later same-file edits stay clean.

## Verify
- [ ] FR#6: The new helper, given grouped rows + pricing, produces the same per-row/accumulated dollar totals as the inline `get_pricing`+`turn_cost` calls it will replace — proven for ≥2 distinct models.
- [ ] AC#5: A direct unit test in `tests/test_token_parser.py` asserts equality with a hand-computed multi-model total (pricing-derived, not hardcoded) and covers all three caller layouts — `model_split` skip (`model_idx=0`, `[1,2,4,5,6,7]`), `cost_by_day`/`cost_by_project` offset (`model_idx=1`, `[2,3,4,5,6,7]`), and `_window_kpis` contiguous (`model_idx=0`, `[1,2,3,4,5,6]`) with `None`-coalescing.
