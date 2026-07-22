---
task_id: "T04"
title: "Add ccrecall backfill tool-content CLI command"
status: "planned"
depends_on: ["T01", "T02", "T03"]
implements: ["FR#8", "AC#6"]
---

## Summary
Create a new `ccrecall backfill tool-content` CLI command that retroactively populates `tool_content` for already-synced sessions by re-parsing their JSONL files. Reuses existing backfill infrastructure (`backfill_query.py`, `backfill_status.py`) rather than reimplementing batch/progress machinery. Includes SAVEPOINT-per-session atomicity, branch_messages linkage for new tool-only rows, and embedding_version reset so re-embedding picks up the new content.

## Target Files
- create: `src/ccrecall/hooks/backfill_tool_content.py`
- modify: `src/ccrecall/cli/commands.py`
- read: `src/ccrecall/content.py` (`extract_text_content` — called directly for the UPDATE path)
- read: `src/ccrecall/hooks/backfill_embeddings.py` (pattern reference)
- read: `src/ccrecall/hooks/backfill_query.py` (shared primitives: `BATCH_SIZE`, no-progress guard)
- read: `src/ccrecall/hooks/backfill_status.py` (shared `--status` reporting)
- read: `src/ccrecall/session_ops.py` (`sync_session` pipeline for reference)
- read: `src/ccrecall/message_ops.py` (`build_message_row`, `insert_new_messages`)
- read: `src/ccrecall/parsing.py` (`parse_all_with_uuids`, `find_all_branches`, `build_aggregated_content`)
- read: `src/ccrecall/import_log_ops.py` (file path lookup)

## Prompt
### hooks/backfill_tool_content.py

Create a new module with a `run()` function following `backfill_embeddings.py`'s structure. Key differences from the embedding backfill:

**Selection**: Query `import_log` for sessions with JSONL files that still exist on disk. Use `os.path.exists()` to check each file before processing. Skip missing files with a logged warning.

**Per-session processing** (all wrapped in a single `SAVEPOINT` per session):
1. Re-parse the JSONL file using `parse_all_with_uuids` + `find_all_branches` to reconstruct the branch→message UUID mapping.
2. Query existing UUIDs for this session from the `messages` table.
3. For each assistant entry in the parsed transcript, extract tool content by calling `extract_text_content` directly (not `build_message_row` — that function's `existing_uuids` guard returns `None` for already-existing rows, which is exactly the rows the UPDATE path needs).
   a. If a `messages` row already exists for this `(session_id, uuid)`: UPDATE `messages SET tool_content = ? WHERE session_id = ? AND uuid = ?`.
   b. If no row exists (tool-only turn that was previously skipped): call `build_message_row` + `insert_new_messages` (which has `ON CONFLICT DO NOTHING` for race safety) and link the new row to the correct branch via `branch_messages`.
3. Rebuild `branches.aggregated_content` for affected branches using `build_aggregated_content`.
4. Reset `branches.embedding_version = NULL` for every touched branch — this is critical so `backfill embeddings` re-selects them for re-embedding.
5. `RELEASE SAVEPOINT` on success; `ROLLBACK TO SAVEPOINT` on exception.

**Shared primitives**: Import `BATCH_SIZE`, `BACKFILL_BATCH_DELAY_SECONDS` from `backfill_query`. Use the same no-progress guard pattern (compare `current_ids` to `last_batch_ids`). Use `backfill_status.py`'s `run_status` for the `--status` flag.

**Logging**: Use `config.setup_logging(settings, process_name="backfill-tool-content")`. Log progress every N sessions (elapsed time, sessions processed, sessions remaining).

**CLI flags**: `--days N` (limit to sessions synced in the last N days), `--limit N` (max sessions to process), `--status` (show progress and exit).

### cli/commands.py

Register `tool-content` as a subcommand under the `backfill` sub-app. Follow the pattern of the existing `backfill embeddings` and `backfill summaries` commands. Wire through `--days`, `--limit`, `--status`, and `--json` flags.

## Focus
- `backfill_embeddings.py` is the primary pattern reference. Key structural elements: `os.nice(10)` for background priority, model availability check (skip for tool-content — no model needed), connection with `load_vec=False` (tool-content backfill doesn't need vec), per-row SAVEPOINT, progress logging, no-progress guard.
- `import_log` table has `filepath` column with the JSONL path. The backfill queries this to find files to re-parse.
- `session_ops.py:sync_session` (line ~1) shows the full parse→insert→branch pipeline. The backfill reuses pieces of this (parsing, `build_message_row`, `insert_new_messages`) but NOT `sync_session` itself (which would be a no-op via the import_log skip check).
- The `embedding_version = NULL` reset is the fix for the challenge's CRITICAL Finding #1. Without it, `backfill embeddings` silently skips already-embedded branches, so tool content never reaches the semantic search index.
- `branch_messages` is a many-to-many table linking branches to messages. New tool-only rows must be linked here or they're invisible to `aggregate_branch_content` and `build_exchange_pairs`.

## Verify
- [ ] FR#8: `ccrecall backfill tool-content` processes existing sessions and populates `tool_content` on their message rows
- [ ] AC#6: After running `backfill tool-content`, `messages.tool_content` is populated for backfilled sessions, `branches.aggregated_content` is rebuilt, and sessions with missing JSONL files are logged and skipped
