# Context: Token Subsystem Refactor (Part 1 of Issue #20)

## Problem & Motivation
Three modules in the token-analytics read path carry oversized functions far past the project's 50-line guideline: `token_output.build_output` (~710 lines, ~20 chart blocks), `token_insights._build_insights` (~332 lines, 9 insight blocks) and `build_trends` (~227 lines), and `token_parser.parse_session` (~214 lines). Three duplication patterns recur: the `get_pricing → turn_cost` cost-accumulation loop appears four times across two modules; the insight dict's key set is hand-rolled 9 times and consumed via string keys; and three detail-list + total SQL pairs repeat the same predicate/join. Issue #15's clean-code pass (PR #19) split the worst offenders but deferred this tail to #20. This is the first design doc of #20; later docs cover migrations/hooks/summarizer/session_ops/helper-relocations against the same branch.

## Visual Artifacts
None.

## Key Decisions
1. **Behavior-preserving, pins first.** No structural change to a function ships before a golden characterization test pinning its output is green on the *current* (unrefactored) code. The pins are committed first (T01), then kept green through every refactor task. This is a hard constraint, not optional.
2. **Shared cost helper in `token_parser.py`.** A single `row_cost`/`sum_cost` helper next to `turn_cost`/`get_pricing` replaces the four inline cost loops. `token_parser` is the dependency base (both other modules import from it; it imports neither), so no circular-import risk. **Column asymmetry:** `model_split`'s query selects 8 columns with `thinking_tokens` at `row[3]` that `turn_cost` skips — the helper takes *explicit token-column indices*, not a contiguous slice.
3. **Typed `Insight`/`Solution` dataclasses** replace the anonymous insight dicts. Per-signal builders return `Insight | None`; serialization to dict happens once at the `build_insights_and_trends` return boundary so the dashboard/JSON consumers see the unchanged dict shape. Dataclass (not pydantic) because pydantic in this repo is reserved for boundary validation of external JSON; internally-constructed structures (`Turn`, `ParsedSession`, `ToolCall`, `CLIContext`) are dataclasses, and `Insight`/`Solution` play that internal role.
4. **parse_session decomposed into line-type handlers** over an explicit mutable parse-state dataclass (`session`, `current_turn`, `turn_index`, `last_assistant_ts`, `metadata_captured`) — module functions threading the state, not a class with methods (personal style: functions over methods, no `_`-private methods).
5. **Detail+total SQL consolidation** — express each shared predicate/join once (Python-side SQL fragment constant) rather than copying it between the list query and the total query.

## Constraints & Anti-Patterns
- **No observable behavior change.** Every output byte of `build_output`, `build_insights_and_trends`, `build_trends`, and `parse_session` must be identical post-refactor (excluding `build_output`'s `generated_at`, which is `Instant.now()` and inherently varies).
- **No smuggled behavior changes.** If the refactor surfaces a latent bug (e.g. a wrong total), do NOT fix it here — preserve existing behavior, note it for a separate issue. The pins encode current behavior including warts.
- **Preserve the public import surface, unchanged signatures:** `build_output`, `build_insights_and_trends`, `build_trends`, `parse_session`, `get_pricing`, `turn_cost`, `project_slug`, `_BASH_ANTIPATTERN_PREDICATE`, and the dataclasses `Turn`, `ParsedSession`, `ToolCall`, `JnlFile`. `tests/test_ingest_token_data.py` and `conftest.py`/`token_helpers.py` import these directly — **do not relocate the dataclasses out of `token_parser`** even if a `SessionParseState` is introduced.
- **Do not touch SQL semantics, pricing tables, waste-token constants, severity thresholds, or any numeric result.** Structure only.
- **Out of scope (later #20 docs):** `migrations.py`, `hooks/memory_context.py`, `hooks/backfill_embeddings.py`, `summarizer.py`, `session_ops.py`, helper relocations (`sanitize_fts_term` → `db.py`), renames (`_CONFIG_KEYS`), WAL pragma. No new charts/insights/metrics.
- **Immutability exemption:** `parse_session` builds `Turn`/`ParsedSession` by in-place accumulation today; keep that accumulation model threaded through the parse-state. Do NOT convert to returns-only — that changes semantics and is a behavior risk.
- Personal coding style: no `_`-prefixed methods on principle, functions over methods, no field docstrings on dataclasses, type annotations are the documentation.

## Design Doc References
- `## Architecture` — the per-module decomposition: the `row_cost` helper contract, the full token_output builder→output-key mapping (must reproduce all 31 keys), the Insight/Solution shape, the parse-state handlers.
- `## Problem` — the column-asymmetry note for the cost helper (model_split's skipped thinking column).
- `## Test Strategy` — which golden tests are NEW (build_output, build_insights_and_trends, build_trends) and which existing tests must stay green unchanged.
- `## Key Constraints` — the pin-before-move discipline and the dataclass-import-surface constraint.
- `## Impact → Behavioral Invariants` — the output dicts that must stay byte-identical.

## Convention Examples
### Module-level pure function with explicit cursor (preferred over nested closure)
**Source:** `src/ccrecall/token_insights.py` (`build_trends`'s `_window_kpis` is currently nested — lift this shape to module level)
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
**DO** derive expected cost from the pricing dict. **DON'T** assert `== 5.0`.

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
