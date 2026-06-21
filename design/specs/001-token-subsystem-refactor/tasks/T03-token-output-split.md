---
task_id: "T03"
title: "Split build_output into per-chart builders"
status: "planned"
depends_on: ["T01", "T02"]
implements: ["FR#1", "FR#7", "FR#8", "AC#6", "AC#7"]
---

## Summary
Decompose `token_output.build_output` (~710 lines) into module-level per-chart builder functions, lift the nested `_compute_seg_curve` closure to module level, consume the shared cost helper from T02 in the three cost loops, and consolidate the detail-list + total SQL pairs. `build_output` becomes thin orchestration that assembles and returns the same dict. The T01 golden pin must stay green throughout — this is behavior-preserving.

## Target Files
- modify: `src/ccrecall/token_output.py`
- read: `src/ccrecall/token_parser.py`
- read: `tests/test_token_output.py`
- read: `design/specs/001-token-subsystem-refactor/design.md`

## Prompt
Refactor `src/ccrecall/token_output.py` so no function exceeds the 50-line guideline except `build_output` itself, which becomes pure orchestration (sequenced calls to named builders + dict assembly).

Follow the design doc's `## Architecture → token_output.py` section exactly — it maps every builder to its output key. Key requirements:

1. **Reproduce every key in the current return dict** (the dict at the end of `build_output`, ~31 keys plus the `build_insights_and_trends` spread). The full return dict is the source of truth. Do not drop any key. In particular do not forget the keys that don't have an obvious 1:1 chart name: `context_segments`, `context_segments_recent`, `context_seg_summary`, `thinking_in_complexity`, `skill_usage_by_day`, `agent_model_dist`.
2. **One `build_<chart>(cur, ...)` per single-key chart**: `sessions_by_day`, `top_tools`, `model_split`, `cost_by_day`, `cost_by_project`, `cache_trajectory`, `tool_footprint`, `ephem_split`, `bash_antipatterns`, `tool_errors_by_tool`, `redundant_reads`, `edit_retries`, `agent_cost`, `hook_overhead`, `project_spend`, `project_tool_profile`, `hook_performance`.
3. **Builders producing two keys from one computation — keep these together, do not split** (splitting re-runs the loop/query and risks drift): `turn_complexity` + `thinking_in_complexity` (one bucketing loop); `skill_usage` + `skill_usage_by_day`; `agent_delegation` + `agent_model_dist`.
4. **`build_context_segments(cur)`** returns `context_segments`, `context_segments_recent`, and `context_seg_summary` together. Lift `_compute_seg_curve` to a module-level function taking **both `cur` and the `session_ids` list** as explicit args (drop the `_` prefix). It is called twice (all-sessions ≥8 turns; recent ≥8 turns within 7 days with `min_sessions=2`). `context_seg_summary` is the separate `seg_agg` aggregation (the `SELECT SUM(t1_ctx*tc), ...` query) plus `len(recent_seg_sids)`.
5. **`build_kpis(cur)`** for the top KPI block (lines 24–67 only), `date_range`, the `kpis` dict, and the intermediates that block computes: `total_output`, `total_input`, `total_cache_cliffs`, `total_max_token_stops`, `total_thinking`, `total_tool_errors`, `global_cache_ratio`, `dominant_cache_tier`, `total_tool_calls` (plus the cache-read/creation/ephem sums feeding the `kpis` dict). **Do NOT put these four in `build_kpis` — they are computed elsewhere and must be threaded by `build_output`'s orchestration:** `total_cost_usd` is `sum(m["cost_usd"] for m in model_split)` (after the `model_split` builder, line 124); `total_bash_antipatterns` (line 353), `total_redundant_reads` (line 397), and `total_edit_retries` (line 430) are each computed alongside their detail-chart query. So the detail+total builders (item 9) must **return their total too**, and `build_output` collects all four to pass into `build_insights_and_trends` and the final `kpis` dict. Decide the cleanest return shape for `build_kpis` (a small dataclass or a dict of intermediates).
6. **Orchestration data flow:** `build_insights_and_trends` requires `total_output`, `total_input`, `total_cache_cliffs`, `total_max_token_stops` (from `build_kpis`), `total_bash_antipatterns`/`redundant_reads_count`/`edit_retries_count` (from the detail+total builders), `total_thinking`/`total_tool_errors`/`global_cache_ratio`/`total_sessions` (from `build_kpis`), `response_time_dist`, the top-3 slices of `bash_antipatterns`/`redundant_reads`/`edit_retries`, `cost_by_project`/`total_cost_usd`, `context_seg_summary`, `dominant_cache_tier`, and `model_split` — see the current call at lines 661–683. `build_output` must gather these from the builder return values, not recompute them. Preserve the exact argument set.
7. **`response_time_dist`** is computed before the insight call and its value is reused (passed into `build_insights_and_trends`), not recomputed — preserve that single computation.
8. **Cost loops** call the T02 helper instead of inline `get_pricing`+`turn_cost`. The three loops have **three different layouts** — pass the correct `model_idx`/`token_indices` to each (verify against the source before writing):
   - `model_split` (rows `model, inp, out, think, cr, cc, e5, e1`): `model_idx=0`, `token_indices=[1, 2, 4, 5, 6, 7]` — skips `thinking` at index 3.
   - `cost_by_day` and `cost_by_project` (rows `<group_key>, model, inp, out, cr, cc, e5, e1`): `model_idx=1`, `token_indices=[2, 3, 4, 5, 6, 7]`.
   **Remove the inline loops** — do not leave them beside the helper call (Replacement Target).
9. **Detail+total SQL pairs** (`bash_antipatterns`/`total_bash_antipatterns`, `redundant_reads`/`total_redundant_reads`, `edit_retries`/`total_edit_retries`): express each shared predicate/join once. Where the total is reconstructable from the detail rows, derive it; where it needs a different aggregation (no LIMIT), hoist the shared join/predicate text to a module-level SQL fragment constant used by both queries so the text isn't duplicated. `_BASH_ANTIPATTERN_PREDICATE` is already a shared constant — keep using it.

After refactoring, run `uv run pytest -q tests/test_token_output.py` (the T01 golden pin must pass) and then the full suite `uv run pytest -q`.

## Focus
- This is the largest single task, but it's one architectural boundary (one file, one function decomposition) and net new logic is near zero — it's moving existing blocks into named functions. Keep each block's SQL and Python **byte-for-byte** where possible; only the wrapping (function def + return) changes.
- `build_output`'s imports: `from itertools import groupby` (used by `_compute_seg_curve`), `Instant` (for `generated_at`), and the token_parser imports. After lifting `_compute_seg_curve`, `groupby` stays a module import.
- **Ordering dependency:** `project_spend` is computed before `project_tool_profile` because the latter uses `top5_projects = [p["project"] for p in project_spend[:5]]`. Preserve that data dependency — `build_project_tool_profile` needs `project_spend`'s result (or recompute the top-5 query as the current code's inner query does; the current code actually re-queries inside the `if top5_projects:` block, so mirror it). Read lines 567-593 carefully — there's a subtlety where the profile re-runs its own `ORDER BY cost DESC LIMIT 5` query rather than reusing `project_spend`. Preserve exactly what's there.
- `cache_trajectory` runs a per-session inner query (LIMIT 30 turns) plus a project lookup per session — keep the N+1 shape as-is (behavior-preserving; not a task to optimize).
- The only nondeterministic output is `generated_at` (`Instant.now()`), which stays in `build_output`'s final assembly, not in a builder.
- `token_output.py` is also edited by no other task; T02 and T05 edit `token_parser.py` (different file). No same-file contention.
- Personal style: module-level functions (not nested), no `_` prefixes, constants at top of file.

## Verify
- [ ] FR#1: `build_output(populated_token_db)` (minus `generated_at`) equals the pre-refactor output — the T01 golden pin passes.
- [ ] FR#7: Every function in `token_output.py` is ≤50 lines except `build_output`, which contains only sequenced builder calls + dict assembly.
- [ ] FR#8: `build_output` remains importable from `ccrecall.token_output` with an unchanged signature; `token_dashboard.py`'s import is unaffected.
- [ ] AC#6: A manual/ruff scan confirms no oversized function remains in `token_output.py`.
- [ ] AC#7: The full test suite (`uv run pytest -q`) passes with zero failures.
