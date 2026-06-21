# Design: Token Subsystem Refactor (Part 1 of Issue #20)

**Date:** 2026-06-21
**Status:** approved
**Scope-mode:** hold

## Problem

Three modules in the token-analytics read path carry oversized functions that exceed the project's 50-line guideline by an order of magnitude:

- `token_output.py` — `build_output` is one ~710-line function: a flat sequence of ~20 independent chart-builder blocks, each running a query and producing one key of the output dict.
- `token_insights.py` — `_build_insights` (~332 lines) is a chain of 9 independent `if signal:` blocks, each appending an insight **dict with the same key set**; `build_trends` (~227 lines) bundles window-KPI computation, new/retired set diffs, and hook-perf comparison into one body.
- `token_parser.py` — `parse_session` (~214 lines) is a single line-dispatch loop with assistant/user/system handling inlined.

Beyond length, three duplication patterns recur:

1. **Cost-accumulation loops.** The `get_pricing(model) → turn_cost(...)` accumulation appears in `build_output` three times (model_split, cost_by_day, cost_by_project) and again in `_window_kpis` inside `token_insights` — a SUM→price→accumulate shape copied across two modules. **Note the column asymmetry:** `cost_by_day`, `cost_by_project`, and `_window_kpis` each SELECT the six token columns contiguously in the order `turn_cost` expects (after a leading group key + model), but `model_split` selects **eight** columns — it carries an extra `thinking_tokens` at `row[3]` that `turn_cost` deliberately skips (`turn_cost(row[1], row[2], row[4], row[5], row[6], row[7], pricing)`). Any shared helper must accommodate this skip, not assume a contiguous six-column slice.
2. **Insight representation redundancy.** The insight dict's key set (`title`, `severity`, `finding`, `root_cause`, `waste_tokens`, `waste_usd`, `solution{action, detail, claudemd_rule, estimated_savings_usd}`) is hand-rolled 9 times, and `_insights_to_findings` / `_insights_to_recommendations` index those keys as strings — a typo or key rename fails silently at runtime.
3. **Detail-list + total SQL pairs.** `bash_antipatterns`, `redundant_reads`, and `edit_retries` each run a top-N detail query and a near-identical second query differing only in aggregation, repeating the same predicate/join.

This matters now because issue #15's clean-code pass (PR #19) split the worst offenders but explicitly deferred this tail to #20. Left alone, the subsystem keeps accreting chart blocks and copied cost loops, and the stringly-typed insight transformers stay fragile.

## Goals

- Every function in the three modules lands under the 50-line guideline (≤50 lines typical; the assembled top-level builders may exceed only where they are pure orchestration of named helpers with no logic).
- The cost-accumulation loop exists once, as a shared helper in `token_parser.py`, called by both `token_output` and `token_insights`.
- Insights, findings, and recommendations are represented by typed dataclasses (`Insight`, `Solution`); per-signal builders return typed `Insight` objects; serialization to dict happens once at the module boundary so the HTML dashboard and JSON consumers see the same dict shape they see today.
- The detail-list + total SQL pairs are consolidated so each predicate/join is expressed once.
- **No observable behavior change.** Every output byte that `build_output`, `build_insights_and_trends`, `build_trends`, and `parse_session` produce today, they produce after the refactor.

## Non-Goals

- `token_parser.py` modules deferred to later #20 design docs: `migrations.py`, `hooks/memory_context.py`, `hooks/backfill_embeddings.py`, `summarizer.py`, `session_ops.py` (`sync_session`), and the helper relocations/renames (`sanitize_fts_term` → `db.py`, rename `_CONFIG_KEYS`, WAL pragma alignment). Those are separate design docs against the same branch.
- No change to the SQL semantics, the output schema, pricing tables, waste-token constants, severity thresholds, or any numeric result.
- No new charts, insights, or metrics. This is structure-only.
- No change to the public import surface consumers rely on (`build_output`, `build_insights_and_trends`, `build_trends`, `parse_session`, `get_pricing`, `turn_cost`, `project_slug`, the dataclasses, `_BASH_ANTIPATTERN_PREDICATE`).

## User Scenarios

### Maintainer (Jessica): sole developer on ccrecall
- **Goal:** extend or debug the token dashboard without reading a 700-line function end-to-end.
- **Context:** adding a chart, adjusting an insight threshold, or tracing a wrong number on the dashboard.

#### Add or modify a chart
1. **Locate the chart's builder.**
   - Sees: a module-level `build_<chart>(cur, ...)` function named for the output key it produces.
   - Decides: which single function to edit.
   - Then: edits one focused function; `build_output` orchestration is untouched.

#### Adjust an insight
1. **Find the per-signal builder.**
   - Sees: a `build_<signal>_insight(...)` returning `Insight | None`.
   - Decides: edit the one builder.
   - Then: the typed `Insight` field flows to findings/recommendations automatically — no string-key edit in three places.

## Functional Requirements

- **FR#1** `build_output(conn)` returns a dict byte-identical to the pre-refactor output for the same database state (modulo the `generated_at` timestamp, which is `Instant.now()` and inherently varies).
- **FR#2** `build_insights_and_trends(conn, **kw)` returns a dict equal to the pre-refactor output for identical inputs, including the `insights`, `findings`, `recommendations`, and `trends` keys and their full nested contents.
- **FR#3** `build_trends(conn)` returns a dict equal to the pre-refactor output for identical database state.
- **FR#4** `parse_session(path, jnl)` returns a `ParsedSession` (or `None`) equal to the pre-refactor result for identical JSONL input.
- **FR#5** Each per-signal insight builder produces an `Insight` whose serialized dict equals the dict the corresponding `if`-block produced before.
- **FR#6** The shared cost-accumulation helper, given the same grouped rows and pricing, produces the same accumulated dollar totals as the inline loops it replaces.
- **FR#7** Every function in the three modules is ≤50 lines, except top-level orchestrators that contain only sequenced calls to named helpers and dict assembly.
- **FR#8** The public import surface (names listed in Non-Goals) remains importable with unchanged signatures.

## Edge Cases

- **Empty database.** `build_output(token_db)` on an empty DB must still return zeroed KPIs, empty lists, and `date_range.earliest = None` (pinned by `TestBuildOutputEmpty`).
- **No recent data.** `build_trends` short-circuits to `{}` when the current 7-day window has zero sessions (pinned by `TestBuildTrends`).
- **Unknown/None model.** Cost helper must route unknown models through `DEFAULT_PRICING` exactly as the inline loops did.
- **Insight builder returns None.** A signal that is zero/below-threshold contributes no insight; the collector must drop `None`s before sorting and priority assignment.
- **Insight sort + priority.** The post-collection sort key `(waste_usd > 0, waste_usd)` descending and the P0/P1/P2 index banding must be preserved exactly — these run after all builders, not inside them.
- **Malformed/blank JSONL lines.** `parse_session` skips them silently; the extracted line-handlers must preserve that (pinned by `test_blank_and_malformed_lines_skipped`).
- **Sidechain session id.** Subagent JSONL files derive `session_id` from the filename stem; this post-loop fixup must survive the parse decomposition.

## Acceptance Criteria

- **AC#1** A **new** golden characterization test captures `build_output(populated_token_db)` (excluding `generated_at`) before refactoring and asserts equality after — passing on the final code. (No existing test pins the full dict; the current `test_token_output.py` checks only selected keys.) (FR#1)
- **AC#2** A **new** golden characterization test captures `build_insights_and_trends(...)` for a kwargs set that fires multiple insights, and for the all-off set, asserting full-dict equality after refactor. (FR#2, FR#5)
- **AC#3** A **new** golden characterization test pins `build_trends` output (full-dict equality, not just key presence) for a DB with both current- and prior-window sessions. The existing `TestBuildTrends.test_structure_with_recent_data` only asserts key presence and is **not** this pin — it must not be mistaken for it. (FR#3)
- **AC#4** Existing `parse_session` characterization tests (`TestParseSessionCharacterization`) pass unchanged. (FR#4)
- **AC#5** The shared cost helper has a direct unit test proving equality with a hand-computed total across ≥2 models. (FR#6)
- **AC#6** `ruff check` reports no function exceeding the line guideline in the three modules (or a manual scan confirms it where ruff has no such rule). (FR#7)
- **AC#7** The full test suite passes with zero failures after the refactor. (all)
- **AC#8** `import`-ing each public name from its module succeeds with the documented signature. (FR#8)

## Key Constraints

- **Behavior-pin-before-move.** No structural change to a function ships before a characterization test pinning that function's output is green on the *current* code. The pin test is committed first (RED-baseline is the existing-output capture; it must pass on unrefactored code), then the refactor commit keeps it green. This is a `refactoring-discipline.md` requirement, not optional.
- **No smuggled behavior changes.** If the refactor surfaces a latent bug (e.g., a wrong total), do not fix it inside this refactor — note it, file it separately, preserve the existing behavior. The pins encode current behavior including any warts.
- **Immutability does not apply to the parser's accumulation.** `parse_session` builds `Turn`/`ParsedSession` by in-place accumulation today; the decomposition keeps that accumulation model (threaded through an explicit parse-state object) rather than converting to a returns-only style — converting accumulation semantics is a behavior risk and out of scope.
- **No new public API surface** beyond the `Insight`/`Solution` dataclasses (which replace anonymous dicts internally; the JSON/HTML boundary still emits dicts).
- **Existing `token_parser` dataclasses stay importable from `token_parser`.** `conftest.py` imports `ToolCall`, and `token_helpers.py` imports `JnlFile`, `ParsedSession`, `Turn`, directly from `ccrecall.token_parser`. If the parse decomposition introduces a `SessionParseState`, the existing four dataclasses must remain in (or be re-exported from) `token_parser` — do not relocate them to a new module.
- **`_normalize_worktree_path` stays importable from `token_parser`.** `tests/test_ingest_token_data.py` imports it directly (along with `record_import`, `should_skip_file`, `project_slug`). It is out of scope for the parse decomposition (it sits below `parse_session`), but must not be renamed or inlined as incidental cleanup — the test depends on the name.

## Dependencies and Assumptions

- No external systems. Pure in-process SQLite read path plus JSONL parsing.
- Assumes `sqlite-vec` availability for the populated fixture (already required by conftest).
- Assumes the existing test fixtures (`populated_token_db`, `token_db`, `token_helpers.py`) are sufficient to characterize the read-side functions — confirmed during reconnaissance.
- `token_parser.py` is the dependency base: `token_output` and `token_insights` both import from it, neither is imported by it. The shared cost helper therefore lands in `token_parser.py` with no circular-import risk.

## Architecture

### Shared cost helper (token_parser.py)

Add one module-level function next to `turn_cost`/`get_pricing`:

```python
def row_cost(row: Sequence, *, model_idx: int, token_indices: Sequence[int]) -> float:
    """Price one grouped row: look up pricing by row[model_idx], then turn_cost
    over the six token columns at token_indices (explicit indices, not a slice,
    so model_split's skipped thinking column is handled)."""
```

The exact signature is an implementation detail for planning — the contract is: given a grouped query row carrying a model column and the six token columns `turn_cost` expects (which are **not always contiguous** — see the `model_split` asymmetry in the Problem section), return that row's dollar cost. Callers accumulate `row_cost` over their rows: the single-total cases (`model_split`, `_window_kpis`) sum it; the per-key cases (`cost_by_day`, `cost_by_project`) add it into their keyed dict. Because the token columns differ per query, the helper takes **explicit token-column indices** (and an explicit `model_idx`) rather than a slice. The three layouts: `model_split` → `model_idx=0`, tokens `[1, 2, 4, 5, 6, 7]` (skipping `thinking` at 3); `cost_by_day`/`cost_by_project` → `model_idx=1`, tokens `[2, 3, 4, 5, 6, 7]` (a group key precedes the model column); `_window_kpis` → `model_idx=0`, tokens `[1, 2, 3, 4, 5, 6]`. Planning may instead choose a `sum_cost(rows, ...)` total-only helper plus a thin per-key wrapper; either way the `get_pricing`+`turn_cost` arithmetic exists exactly once.

### token_output.py

Decompose `build_output` into module-level builders. **The decomposition must reproduce every key in the current return dict** (lines 685–731) — the full set is the source of truth, not this prose list. Mapping builders to output keys:

- `build_kpis(cur)` → `total_sessions`, `date_range`, `kpis`, and the intermediate totals (`total_output`, `total_input`, etc.) that feed `build_insights_and_trends` and the global cache ratio / dominant tier.
- One `build_<chart>(cur, ...)` per chart producing a single key: `sessions_by_day`, `top_tools`, `model_split`, `cost_by_day`, `cost_by_project`, `cache_trajectory`, `tool_footprint`, `ephem_split`, `bash_antipatterns`, `tool_errors_by_tool`, `redundant_reads`, `edit_retries`, `agent_cost`, `hook_overhead`, `project_spend`, `project_tool_profile`, `hook_performance`.
- Builders producing **two keys from one computation** (do not split these — splitting re-runs the loop/query):
  - `turn_complexity` **and** `thinking_in_complexity` — both come out of the single complexity-bucketing loop (lines 469–498).
  - `skill_usage` and `skill_usage_by_day` — two queries, but cohesive; one builder returning both reads cleanest.
  - `agent_delegation` and `agent_model_dist` — two queries over the same join; one builder returning both.
- The **context-segmentation block** produces `context_segments`, `context_segments_recent`, and `context_seg_summary`. Lift the nested `_compute_seg_curve` closure to a module-level function taking **both `cur` and the `session_ids` list** as explicit args (drop the `_` prefix per personal style); it is called twice (all-sessions, recent-only). `context_seg_summary` is a **separate** aggregation query (lines 296–320) plus `len(recent_seg_sids)` — fold the two `_compute_seg_curve` calls, the two session-id queries, and the summary aggregation into one `build_context_segments(cur)` returning all three keys.
- `response_time_dist` is computed in `build_output` *before* the insight call (its result is also passed into `build_insights_and_trends`), so its builder runs early and its value is reused, not recomputed.
- `build_output` becomes orchestration: call the builders, then assemble and return the dict (plus the `build_insights_and_trends` spread). Some chart builders that share a query (e.g. the detail-list + total pairs) return a small tuple or dataclass so the single query feeds both.
- **Detail+total consolidation:** for bash_antipatterns/redundant_reads/edit_retries, a single builder runs the detail query and derives the total from it where the total is reconstructable from the detail rows; where the total needs a different aggregation (no LIMIT), express the shared predicate/join once as a Python-side SQL fragment constant and run both, so the join text is not duplicated.

### token_insights.py

- Introduce `@dataclass` `Solution` and `Insight` (fields mirroring current keys; `priority` defaults to `""` and is set by the collector).
- One `build_<signal>_insight(...) -> Insight | None` per current `if`-block (cache_cliffs, max_token_stops, bash_antipatterns, redundant_reads, edit_retries, thinking, idle_gap, cost_concentration, context_overhead). Most take a single count plus context; two are not count-gated and need wider inputs: `idle_gap` derives its trigger from both `response_time_dist` **and** `dominant_cache_tier` (the over-TTL bucket sum depends on the tier), and `context_overhead` keys off `context_seg_summary`'s `base_overhead_pct`/`avg_base_ctx`. Plan their signatures accordingly rather than assuming a uniform `(count, ...)` shape.
- `_build_insights` becomes: build the list of `Insight | None`, drop `None`s, sort by `(waste_usd > 0, waste_usd)` desc, assign P0/P1/P2 banding, return. The `_waste_usd`/`_severity` closures become small helpers (module-level functions taking the rates/sessions they need, or kept as locals if that reads cleaner — they are pure).
- `_insights_to_findings` / `_insights_to_recommendations` consume typed `Insight` fields instead of string keys.
- `build_insights_and_trends` serializes the `Insight` list to dicts at the return boundary (`asdict` or an explicit `to_dict`) so `out["insights"]` is the same list-of-dicts as today.
- `build_trends`: extract `_window_kpis` to a module-level helper; extract the new/retired-set diff into one helper called twice (skills, hooks) since both compute current-set vs prior-set over the same two windows; extract the hook-perf comparison. Hoist the repeated window-clause strings to named constants (`CURRENT_WINDOW_CLAUSE`, `PRIOR_WINDOW_CLAUSE`).

### token_parser.py

Decompose `parse_session`'s loop into per-line-type handlers operating on an explicit mutable parse-state (a small dataclass holding `session`, `current_turn`, `turn_index`, `last_assistant_ts`, `metadata_captured`). The loop body dispatches to `handle_assistant_line` / `handle_user_line` / `handle_system_line`; content-block extraction (thinking, tool_use, the Skill/Agent metadata normalization) moves into a focused `extract_tool_call` / `apply_content_block` helper. `parse_session` keeps the file-read, the dispatch loop, and the post-loop finalization (last-turn append, session-id fallback, sidechain stem fixup). State stays threaded explicitly — functions over methods where possible; if threading proves noisy, a single `SessionParseState` dataclass with the handlers as module functions taking it as first arg is acceptable (not a class with methods, to honor the no-underscore/functions-over-methods conventions).

## Replacement Targets

The inline cost-accumulation loops are replaced by the shared cost helper — remove each inline loop, do not leave it alongside the helper call. The loops being removed are: `build_output`'s three (model_split, cost_by_day, cost_by_project) **and** `_window_kpis`'s loop in `token_insights`. The anonymous insight dict literals in `_build_insights` are replaced by `Insight`/`Solution` construction — the dicts are superseded, not kept in parallel. No other code is being replaced.

## Convention Examples

### Module-level pure function with explicit cursor (preferred over nested closure)

**Source:** `src/ccrecall/token_insights.py` (`build_trends`'s `_window_kpis` is currently nested — the refactor lifts this shape to module level)

```python
def build_trends(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()
    current = _window_kpis("datetime(sm.first_turn_ts) >= datetime('now', '-7 days')")
    ...
```

### Characterization test against a shared fixture

**Source:** `tests/test_token_output.py`

```python
class TestBuildOutputEmpty:
    def test_empty_db_zeroed_not_crashed(self, token_db):
        out = build_output(token_db)
        assert out["kpis"]["total_sessions"] == 0
        assert out["top_tools"] == []
        assert out["date_range"]["earliest"] is None
```

### Pricing-derived assertion (don't hardcode dollar amounts)

**Source:** `tests/test_token_parser.py`

```python
def test_input_and_output(self):
    p = get_pricing("claude-sonnet-4-5")
    assert turn_cost(1_000_000, 0, 0, 0, 0, 0, p) == p["input"]
```

**DO** derive expected cost from the pricing dict so the test can't drift when rates change. **DON'T** assert `== 5.0`.

### Dataclass for a structure passed between functions

**Source:** `src/ccrecall/token_parser.py` (`Turn`, `ParsedSession`)

```python
@dataclass
class Turn:
    index: int
    message_id: str
    timestamp: str
    model: str | None = None
    input_tokens: int = 0
    ...
```

The new `Insight`/`Solution` dataclasses follow this exact style: typed fields, sensible defaults, no field docstrings.

## Alternatives Considered

- **Keep insight dicts, extract builders only** (the rejected fork). Smaller diff, still kills the 700-line function, but leaves the 9× repeated key set and the stringly-typed `_insights_to_findings`/`_insights_to_recommendations`. Rejected because the stated goal is reducing that exact redundancy, the user accepts larger diffs (no review-size constraint), and typed dataclasses match the personal convention for structures passed between functions.
- **Pydantic `BaseModel` for `Insight`/`Solution`** (for consistency with `models.py`). Rejected — in this repo pydantic is reserved for boundary validation of *external* JSON (`TokenLine`, `HookInput`, `is_valid`), while internally-constructed structures (`Turn`, `ParsedSession`, `ToolCall`, `CLIContext`) are dataclasses. `Insight`/`Solution` are built entirely from our own computed values and only serialize outward — they play the internal-structure role, so dataclass matches their immediate neighbors in `token_parser.py`. Pydantic's validation would add overhead with no untrusted input to validate. "Consistent with the codebase" here means matching the internal-structure pattern, not the validation one.
- **Put the cost helper in a new `token_costs.py` module.** Rejected — `token_parser.py` already owns `turn_cost`/`get_pricing`, both consumers already import from it, and a new module adds an import hop for one function (`reader-load.md`: don't add a layer that doesn't earn its keep).
- **`SessionParser` class for parse_session.** A class holding parse state with handler methods. Rejected as the default in favor of module functions threading an explicit state dataclass (personal style: functions over methods, no `_`-private methods). Kept as a documented fallback if explicit threading reads worse.
- **Do nothing.** Rejected — issue #20 exists precisely because the deferred tail keeps the subsystem hard to extend; the cost loops have already been copied four times.

## Test Strategy

### Existing Tests to Adapt
- `tests/test_token_output.py`, `tests/test_token_insights.py`, `tests/test_token_parser.py` — these should pass **unchanged** (they pin behavior at the public-function level). If any needs editing, that signals a behavior change and is a red flag, not a routine adaptation. No edits expected beyond possibly adding the new golden tests alongside.
- `tests/token_helpers.py` / `conftest.py` fixtures — reused as-is; no changes expected.

### New Test Coverage
- **Golden characterization** for `build_output(populated_token_db)` minus `generated_at` (FR#1, AC#1) — capture the full dict, assert deep equality.
- **Golden characterization** for `build_insights_and_trends` with a multi-insight kwargs set and the all-off set (FR#2, FR#5, AC#2).
- **Golden characterization** for `build_trends` with current+prior window data (FR#3, AC#3).
- **Unit test** for `row_cost` proving equality with a hand-computed multi-model total (FR#6, AC#5).
- `parse_session` is already characterized (`TestParseSessionCharacterization`); confirm it covers the assistant/user/system/content-block paths the decomposition touches — extend only if a path (e.g. hook_summary, sidechain stem fixup) is unpinned.

These golden tests are committed **first**, green on the current code, then kept green through each refactor commit.

### Tests to Remove
No tests to remove — nothing is being deleted from the public surface.

## Documentation Updates

No documentation updates required. These are internal modules with no user-facing docs, CLI help, or README references to the refactored functions. Issue #20 tracks the work; the PR description will reference it.

## Impact

### Changed Files
- `src/ccrecall/token_parser.py` — modify: add `row_cost`; decompose `parse_session` into line-type handlers + parse-state. (shared base — both other modules import it.)
- `src/ccrecall/token_insights.py` — modify: add `Insight`/`Solution` dataclasses; split `_build_insights` into per-signal builders + collector; split `build_trends`; consume the shared cost helper.
- `src/ccrecall/token_output.py` — modify: split `build_output` into per-chart builders; lift `_compute_seg_curve`; consume the shared cost helper; consolidate detail+total SQL pairs.
- `tests/test_token_output.py` — modify: add golden characterization test.
- `tests/test_token_insights.py` — modify: add golden characterization tests.
- `tests/test_token_parser.py` — modify: add `row_cost` unit test; extend parse coverage only if an affected path is unpinned.

### Behavioral Invariants
- The output dicts of `build_output`, `build_insights_and_trends`, `build_trends`, and the `ParsedSession` from `parse_session` must be byte-identical to current behavior (excluding the inherently-varying `generated_at` timestamp).
- The HTML dashboard and the slim stdout JSON consume `build_output`'s dict — their contract is the dict shape, which must not change.
- Public import names (`build_output`, `build_insights_and_trends`, `build_trends`, `parse_session`, `get_pricing`, `turn_cost`, `project_slug`, `_BASH_ANTIPATTERN_PREDICATE`, the dataclasses) keep their signatures.

### Blast Radius
- `token_output.build_output` is called by the dashboard generation path (token_analytics / CLI). Behavior-preserving, so consumers are unaffected.
- `_BASH_ANTIPATTERN_PREDICATE` is imported by both `token_output` and `token_insights`; it is not being moved or changed.
- All within `src/ccrecall/`; no cross-package or external consumers.

## Open Questions

None.
