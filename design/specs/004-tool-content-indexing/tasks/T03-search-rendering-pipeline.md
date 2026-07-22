---
task_id: "T03"
title: "Wire tool content into FTS, embedding, and rendering paths"
status: "planned"
depends_on: ["T02"]
implements: ["FR#5", "FR#6", "FR#7", "FR#10", "AC#3", "AC#5", "AC#9"]
---

## Summary
Thread `tool_content` through all search and rendering surfaces: add a `__tools__` section to FTS `aggregated_content`, extend `build_exchange_pairs` to include tool content in embedded text (with consecutive-type collapsing and tool-only turn guard), delete the vestigial `[Tool: \w+]` regex, extend `compute_context_summary`'s SQL to include `tool_content`, and extend `format_markdown_session` to render tool actions. Update summarizer and related tests.

## Target Files
- modify: `src/ccrecall/parsing.py`
- modify: `src/ccrecall/summarizer.py`
- modify: `src/ccrecall/formatting.py`
- modify: `tests/test_summarizer.py`
- read: `src/ccrecall/db.py` (`fetch_branch_messages` — now includes `tool_content` from T02)
- read: `src/ccrecall/embed_ops.py` (calls `build_exchange_pairs` — no changes needed there)
- read: `src/ccrecall/search_vector.py` (`hydrate_snippets` — reads `chunks.assistant_text` which now contains tool content naturally)
- read: `src/ccrecall/embeddings.py` (`cap_for_embedding` — understand truncation behavior)

## Prompt
### parsing.py — FTS aggregation

Extend `aggregate_branch_content` (line ~212) and `build_aggregated_content` (line ~226) to include `tool_content` from messages:

1. In `aggregate_branch_content`'s SELECT, add `m.tool_content`.
2. Collect non-null, non-empty `tool_content` values.
3. In `build_aggregated_content`, append a `__tools__` section after the existing `__commits__` section, containing the joined tool content. Follow the same pattern as the `__files__` and `__commits__` sections.

### summarizer.py — embedding + context summary

**`build_exchange_pairs` (line ~66):**

1. Delete the vestigial regex at line ~100: `cleaned = re.sub(r"\[Tool: \w+\]", "", m["content"]).strip()`. Replace with `cleaned = m["content"].strip()`. The regex is dead code — no upstream producer emits `[Tool: X]` markers.

2. Fix the tool-only turn guard: after computing `cleaned`, also check for `tool_content`:
   ```python
   tool_text = m.get("tool_content", "") or ""
   if cleaned:
       current_asst_parts.append(cleaned)
   if tool_text:
       current_asst_parts.append(tool_text)
   ```
   This ensures tool-only turns (empty `content` but non-empty `tool_content`) are included in exchange pairs.

3. Add consecutive-type collapsing before appending `tool_text`: collapse runs of consecutive identical tool types (e.g., 15 `[Read: ...]` lines) into a single summary line like `[Read: 15 files including path1, path2, ...]`. Parse the `[ToolName: ...]` markers, group consecutive same-name entries, and for groups of 3+, collapse to a count + first 2 examples.

**`compute_context_summary` (line ~324):**

Extend its standalone SQL query (line ~355) to also SELECT `m.tool_content`. Thread it through to the downstream summary builder so tool content appears in the SessionStart context injection.

### formatting.py — markdown rendering

Extend `format_markdown_session` (line ~105): after rendering `msg['content']`, also append `msg.get('tool_content', '')` when non-empty. Use a simple separator (newline). Tool content should appear after prose content in the rendered output.

### tests/test_summarizer.py

Update `build_exchange_pairs` tests:
- Existing tests that construct mock message dicts: add `"tool_content": ""` (or `None`) to each dict so they don't break on the new `.get("tool_content")`.
- Add new test: message with empty `content` but non-empty `tool_content` produces an exchange pair (tool-only turn guard).
- Add new test: consecutive-type collapsing — 15 `[Read: ...]` markers become a single collapsed line.
- Add new test: mixed prose + tool content both appear in the exchange pair.
- Remove or update any test that asserts the vestigial `[Tool: X]` regex stripping behavior.

## Focus
- `parsing.py:aggregate_branch_content` (line ~212) selects from `messages` joined through `branch_messages`. The SELECT must add `m.tool_content` — verify the column index in the row tuple.
- `summarizer.py:build_exchange_pairs` (line ~66) receives message dicts from `fetch_branch_messages`. After T02, those dicts include `tool_content`. The function processes assistant messages at line ~99-102 — that's where the regex deletion, guard fix, and collapsing logic go.
- `summarizer.py:compute_context_summary` (line ~324) has its own standalone SQL (line ~355-359) that does NOT use `fetch_branch_messages`. Its SQL must independently be extended to select `tool_content`. The comment at line ~355 documents why it's standalone.
- `formatting.py:format_markdown_session` (line ~105) iterates messages and prints `f"**{role}:** {msg['content']}\n"`. Add tool_content after this.
- `search_vector.py:hydrate_snippets` reads `chunks.user_text`/`chunks.assistant_text` — these get populated at embed time by `embed_ops.py` from `build_exchange_pairs` output. No changes needed in `search_vector.py` — tool content flows through naturally once `build_exchange_pairs` includes it.
- `tests/test_summarizer.py` has tests that assert `[Tool: Read]` is stripped — those tests need updating since the regex is being deleted.

## Verify
- [ ] FR#5: After syncing, `branches.aggregated_content` contains `__tools__` section with tool markers
- [ ] FR#6: After syncing and embedding, exchange pairs include tool content in assistant text
- [ ] FR#7: `search-messages` results include tool content in snippet text (via `chunks.assistant_text`)
- [ ] FR#10: Unit test verifies 5 consecutive `[Read: ...]` markers are collapsed into a single summary line
- [ ] AC#3: `aggregated_content` contains `[Bash: ...]` and `[AskUserQuestion: ...]` markers after sync
- [ ] AC#5: Exchange pairs from `build_exchange_pairs` include tool content in assistant text (end-to-end snippet verification is in T05)
- [ ] AC#9: Consecutive identical tool types are collapsed before embedding
