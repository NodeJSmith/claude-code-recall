---
task_id: "T07"
title: "Render enrichment in context and search"
status: "planned"
depends_on: ["T01", "T02"]
implements: ["FR#1", "FR#4", "FR#17", "AC#1", "AC#2", "AC#8", "AC#10"]
---

## Summary

Integrate valid Branch Resume Briefs into selected SessionStart context and search-card presentation.
Preserve deterministic topics, search ranking, JSON compatibility, and deterministic fallback behavior.

## Target Files

- modify: `src/ccrecall/hooks/session_selection.py`
- modify: `src/ccrecall/hooks/context_rendering.py`
- modify: `src/ccrecall/search_hydrate.py`
- modify: `src/ccrecall/formatting.py`
- modify: `src/ccrecall/search_cli.py`
- modify: `tests/test_context_injection.py`
- modify: `tests/test_search.py`
- read: `src/ccrecall/hooks/memory_context.py`
- read: `src/ccrecall/search_conversations.py`

## Prompt

Extend session-selection rows with the stored enrichment envelope/status/version/source hashes needed by T02's lightweight validity helper. In `context_rendering.build_context()`, compose valid enrichment with cached deterministic context and pass `is_primary_session=(i == 0)` so the Session Origin session gets the 2,400-character brief and later selected sessions get 800 characters. Do not require `context_summary_json` on this hot path and do not change fallback behavior for uncached sessions.

Extend `hydrate_cards()` to select enrichment columns, retain deterministic `topic`, and add `display_title` from `title.text` plus `summary_preview` from `where_we_left_off.text` only when the envelope is current. Update human card formatting to prefer the additive display fields and render the preview; update JSON formatting to include them additively without changing `topic`, scores, or ranking. Do not modify `search_conversations` retrieval, FTS, vector queries, or `aggregated_content`.

## Focus

`session_selection._row_to_entry()` and both SQL query constants use positional tuples, so append columns consistently and update unpacking together. `context_rendering.build_context()` already enumerates selected sessions, making `i == 0` the defined primary policy. `search_hydrate.hydrate_cards()` is summary-only and must stay that way; `search_conversations.py` remains a caller with no ranking change. The card's deterministic `topic` is a public JSON compatibility field, so never overwrite it.

## Verify

- [ ] FR#1: Existing context/search tests still render deterministic summaries and topics when enrichment is disabled, absent, failed, or stale.
- [ ] FR#1: Uncached selected-session tests preserve the existing fallback renderer and do not require `context_summary_json` solely to evaluate enrichment.
- [ ] FR#4: Context and card tests show enrichment only for current `ok` envelopes with equal source hashes.
- [ ] FR#17: Context tests prove primary/supplementary budgets and deterministic context placement.
- [ ] AC#1: Deterministic context injection and search behavior pass unchanged in no-enrichment fixtures.
- [ ] AC#2: Valid enrichment appears above deterministic context; invalid/stale enrichment disappears without changing the fallback.
- [ ] AC#8: Search cards prefer valid display title/latest-state preview while retaining deterministic topic and unchanged retrieval order.
- [ ] AC#10: Rendering tests cover both selected-session budgets and an evidenced continuation hint.
