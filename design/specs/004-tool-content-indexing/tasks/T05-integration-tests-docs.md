---
task_id: "T05"
title: "Add integration tests and update documentation"
status: "planned"
depends_on: ["T01", "T02", "T03", "T04"]
implements: ["AC#2", "AC#3", "AC#4", "AC#5", "AC#6", "AC#10", "AC#11"]
---

## Summary
Add integration tests covering the full pipeline (sync → FTS → embedding → search → snippet display), verify the backfill command, and update CLAUDE.md with the new architecture. Run the full test suite and linter to confirm nothing is broken. This task verifies the end-to-end behavior that individual unit tests in T01-T04 can't cover alone.

## Target Files
- modify: `tests/test_integration.py`
- modify: `CLAUDE.md`
- read: `tests/conftest.py` (fixtures and test infrastructure)
- read: `src/ccrecall/search_conversations.py` (search_messages entry point)
- read: `src/ccrecall/search_vector.py` (hydrate_snippets, snippet content)

## Prompt
### tests/test_integration.py

Add integration tests that exercise the full pipeline with real database operations:

1. **Tool-only turn persistence**: Create a synthetic transcript with an assistant turn that has only `tool_use` blocks (no text). Sync it via `sync_session`. Assert that `messages` has a row with `content = ''` and `tool_content` populated with `[ToolName: ...]` markers.

2. **FTS includes tool content**: After syncing a transcript with tool calls, query `branches.aggregated_content` and assert it contains a `__tools__` section with the expected tool markers.

3. **Backfill populates tool_content**: Sync a transcript using the OLD behavior (simulate by setting `tool_content = NULL` on inserted rows). Run the backfill logic. Assert `tool_content` is now populated on those rows, `aggregated_content` is rebuilt, and `embedding_version` is reset to NULL.

4. **Backfill skips missing JSONL**: Set up an `import_log` entry pointing to a non-existent file. Run the backfill. Assert it logs a warning and continues without error.

5. **Search-messages returns tool content match**: Sync a transcript containing an AskUserQuestion about "retry task". Run the embedding pipeline. Call `search_messages` with query "retry task". Assert the result contains a snippet with the AskUserQuestion text visible in the assistant excerpt (AC#4, AC#5). This is the design's central motivating scenario.

Update existing integration tests that assert on `messages` table contents or `aggregated_content` to account for the new `tool_content` column (add it to column lists in assertions, verify it's NULL for old-format entries or populated for new ones).

### CLAUDE.md

Update these sections in the project CLAUDE.md:

1. **Architecture bullet for content.py**: Add that `extract_text_content` now returns a 5-tuple including `tool_content` (generic field-join extraction of tool_use inputs).

2. **Schema description**: Add `tool_content TEXT` to the `messages` table column list. Note that `tool_summary` and `has_tool_use` remain unpopulated dead columns.

3. **`SCHEMA_VERSION`**: Update to `4` with a note about `_migrate_to_v4` (additive, unconditional like v3).

4. **Embedding layer**: Note that `build_exchange_pairs` now includes `tool_content` in exchange text, with consecutive-type collapsing. Note deletion of the vestigial `[Tool: \w+]` regex.

5. **Backfill**: Add `backfill_tool_content.py` to the hooks list with its process name.

6. **Per-process logging**: Add `backfill-tool-content` to the process names list.

### Verification

Run:
- `uv run pytest` — full test suite must pass
- `uvx prek run --all-files` — lint and type checks must pass

## Focus
- `tests/test_integration.py` uses real SQLite databases via fixtures in `conftest.py`. Read the existing integration tests to understand the fixture pattern (transcript creation, `sync_session` calls, database assertions).
- `conftest.py` likely has helper functions for creating synthetic JSONL transcripts — reuse those.
- The CLAUDE.md updates should be surgical — update the specific sections mentioned, don't rewrite surrounding text.
- The `messages` table column list in CLAUDE.md is in the "Four invariants to preserve" section under "Session-keyed branch identity" — check the exact location before editing.

## Verify
- [ ] AC#2: Integration test verifies tool-only assistant turns produce `messages` rows with empty `content` and populated `tool_content`
- [ ] AC#3: Integration test verifies `aggregated_content` contains tool markers after sync
- [ ] AC#4: Integration test syncs a transcript with AskUserQuestion about "retry task", embeds, and verifies `search_messages` returns a matching result
- [ ] AC#5: The search-messages result snippet includes the AskUserQuestion tool content text
- [ ] AC#6: Integration test verifies backfill populates `tool_content`, rebuilds `aggregated_content`, and skips missing JSONL with a warning
- [ ] AC#10: `uv run pytest` passes with 0 failures
- [ ] AC#11: `uvx prek run --all-files` passes with no errors
