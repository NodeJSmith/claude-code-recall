# Design: Tool Content Indexing

**Date:** 2026-07-22
**Status:** approved
**Scope-mode:** hold

## Problem

Tool_use input content in Claude Code transcripts is invisible to ccrecall search. When an agent or user tries to find specific tool actions from a past session — an AskUserQuestion prompt, a Bash command, an Agent dispatch — neither keyword nor semantic search can surface it. The only path to that content today is parsing raw JSONL files manually.

This was observed directly: in session `ef69cbb0`, an agent made 8 consecutive attempts to find an AskUserQuestion prompt from session `8a232470` using `ccrecall tail` and `search`. After 6 failed `tail` invocations and 2 failed `search` attempts, it gave up and wrote Python scripts to parse the raw JSONL. The content existed — it was just never extracted or indexed.

The root cause is in `content.py:extract_text_content`, which only pulls `type == "text"` blocks from assistant messages. All tool_use `input` payloads are discarded except for file paths from Edit/Write/MultiEdit and commit messages from `git commit -m` Bash commands. Additionally, assistant turns that consist entirely of tool_use blocks (no prose text) produce no `messages` row at all — they are completely absent from the database.

## Goals

- Tool_use input content from all major tool types is searchable via both FTS keyword search and semantic (embedding) search.
- Search result snippets surface the tool content that caused a match, so the user can see WHY a result matched.
- Retroactive backfill makes existing synced sessions' tool content searchable.
- Tool-only assistant turns (no prose text) are no longer invisible — they produce database rows and are searchable.

## User Scenarios

### Agent: AI assistant using ccrecall to retrieve past session context
- **Goal:** Find a specific AskUserQuestion prompt from a prior orchestration session
- **Context:** During a conversation about orchestration retry behavior, needs to reference the exact gate prompt wording used in a past session

#### Find tool content via search

1. **Searches for the content**
   - Runs: `ccrecall search-messages --query "retry task" --session 8a232470`
   - Sees: A snippet showing the AskUserQuestion with "Fix and retry this task" label and its option descriptions
   - Then: Uses the retrieved content to inform the current conversation

### User: Developer searching conversation history
- **Goal:** Find which Bash command was run to fix a specific issue
- **Context:** Remembers running a command weeks ago but can't recall the exact syntax

#### Find command via keyword search

1. **Searches for the command**
   - Runs: `ccrecall search --query "apt install ripgrep"`
   - Sees: Session card matching the session where that command was run
   - Then: Tails the session for full context

## Functional Requirements

- **FR#1** Tool actions from assistant turns are extracted into a separate searchable text field alongside the existing conversation prose
- **FR#2** Extraction covers these tool types: AskUserQuestion (question text + option labels/descriptions), Agent (description + prompt), Bash (command + description), Skill (skill name + args), Edit/Write/MultiEdit (file path + capped diff text where available), Read (file path), Grep/Glob (pattern)
- **FR#3** Tool action text is stored separately from conversation prose in the database
- **FR#4** Assistant turns that consist entirely of tool actions (no conversation text) are stored and searchable — they are no longer silently dropped
- **FR#5** Tool action text is keyword-searchable via FTS alongside conversation prose
- **FR#6** Tool action text is semantically searchable via embeddings alongside conversation prose
- **FR#7** Search result snippets from `search-messages` include tool content when it is part of the matched exchange
- **FR#8** A `ccrecall backfill tool-content` CLI command re-parses existing JSONL files and populates tool content for previously synced sessions (best-effort — sessions whose JSONL files are missing are skipped with a logged warning)
- **FR#9** Large tool inputs are capped to prevent unbounded text — each tool type has a maximum character limit per input field
- **FR#10** Before embedding, consecutive identical tool types are collapsed into a single summary line to prevent repetitive markers from diluting prose signal

## Edge Cases

- **Missing JSONL during backfill**: Session's JSONL file was deleted or moved since import. Backfill skips the session, logs a warning, and continues.
- **Tool-only turns with no searchable content**: An assistant turn that calls only Read with no text — produces a row with `tool_content = "[Read: src/ccrecall/content.py]"`, which is short but still indexed.
- **Very long tool inputs**: An Agent prompt with 5000 characters of instructions, or a Bash command with a multi-line Python script. Each tool type has a cap; content beyond the cap is truncated with `…`.
- **Unknown tool types**: A tool_use block with a name not in the dispatch table. Produces a generic marker like `[UnknownTool]` — the tool name is captured even if input details aren't.
- **Concurrent backfill and sync**: A session is being synced by the SessionStart hook while the backfill is also processing it. The backfill operates via row-level updates matched on `(session_id, uuid)` and inserts for new tool-only rows — SQLite's busy timeout (`BUSY_TIMEOUT_MS`) handles contention. A concurrent sync may INSERT a message row that the backfill then updates, or vice versa. Both paths converge on the same final state because the backfill's extraction logic matches the forward sync's extraction logic.
- **Embedding cap overflow**: Tool-heavy sessions (orchestration runs with 50+ Bash calls) may produce exchange text that exceeds `cap_for_embedding`. The cap truncation is acceptable — the FTS path is unaffected and provides full keyword coverage.
- **Vestigial `[Tool: \w+]` regex in `summarizer.py`**: This regex only matches the literal word "Tool" (e.g., `[Tool: Read]`), not tool names (e.g., `[Bash: ...]`). Our marker format does not collide. The regex is dead code — no upstream producer emits `[Tool: X]` markers.

## Acceptance Criteria

- **AC#1** (FR#1, FR#2) `extract_text_content` returns a `tool_content` string containing markers for each tool_use block in the message. Unit tests cover all tool types with representative inputs.
- **AC#2** (FR#3, FR#4) After syncing a transcript containing tool-only assistant turns, `SELECT content, tool_content FROM messages WHERE tool_content != ''` returns rows including those tool-only turns.
- **AC#3** (FR#5) After syncing a transcript, `SELECT aggregated_content FROM branches WHERE ...` contains tool content markers (e.g., `[Bash: ...]`, `[AskUserQuestion: ...]`).
- **AC#4** (FR#6) After syncing and embedding, `ccrecall search-messages --query "retry task"` against a session containing an AskUserQuestion about retrying tasks returns a result with the matching exchange.
- **AC#5** (FR#7) The search-messages result for a tool-content match includes the tool content in its snippet text.
- **AC#6** (FR#8) `ccrecall backfill tool-content` populates `tool_content` for existing sessions, updating both `messages.tool_content` and `branches.aggregated_content`. Sessions with missing JSONL files are logged and skipped.
- **AC#7** (FR#9) Tool inputs exceeding their cap are truncated. Unit test verifies an Agent prompt of 1000 chars is capped.
- **AC#8** (FR#1, FR#2) Extraction never raises regardless of tool_use `input` shape — missing keys, wrong types, `None` where a list is expected. Unit tests cover malformed input for each tool type (not just well-formed input).
- **AC#9** (FR#10) Consecutive identical tool types are collapsed before embedding. Unit test verifies 15 consecutive Read markers become a single summary line.
- **AC#10** Full test suite passes (`uv run pytest`).
- **AC#11** Lint and type checks pass (`uvx prek run --all-files`).

## Key Constraints

- **Delete the vestigial `[Tool: \w+]` regex in `summarizer.py:100`** — it is dead code (no upstream producer emits that pattern) and the function is already being edited. Removing it eliminates a constraint on future marker format decisions.
- **Do not import fastembed/onnxruntime/sqlite_vec in the extraction path** — `extract_text_content` runs on the hook hot path. Tool content extraction must remain lightweight (string manipulation only).
- **Do not break the `if not text: return None` early return without providing an alternative guard** — tool-only turns should produce rows, but rows with both `content = ''` and `tool_content = ''` should still be skipped.

## Dependencies and Assumptions

- **JSONL file availability**: Retroactive backfill depends on the original JSONL transcript files still existing on disk at the paths recorded in `import_log`. Sessions whose files are gone cannot be backfilled.
- **Schema migration**: The v4 migration adds `tool_content TEXT` to `messages`. Existing rows get `NULL` (populated by backfill). New syncs populate it automatically.

## Architecture

### Extraction layer (`content.py`)

Extend `extract_text_content` to return a fifth value: `tool_content` (a string of concatenated tool markers). The existing four-value return (`text, has_tool_use, has_thinking, tool_summary`) is unchanged.

**Generic field-join extraction** — rather than a per-tool dispatch table (which would duplicate `session_tail.py:_tool_event()`'s 9-type dispatch), use a generic approach: for each `tool_use` block, iterate the `input` dict's top-level keys, collect string-valued fields (skipping keys whose values are not strings or are obviously non-searchable), and join them into a marker. This automatically covers new tool types Claude Code may ship without code changes.

`session_tail.py:_tool_event()` retains its per-type formatting for display readability — search and display have different goals (searchability vs. readability), so sharing one dispatch table would serve neither optimally.

Marker format: `[ToolName: joined field values]` — one per tool_use block, newline-separated. The tool name is always present; field values are joined with spaces, each capped individually. Examples of what the generic extraction produces:
- `[Bash: ls -la /tmp]` (from `input.command`)
- `[Bash: Install dependencies npm install]` (from `input.description` + `input.command`)
- `[AskUserQuestion: What would you like to do? Approve as-is Revise the plan Abandon]` (from recursive string extraction of `input.questions`)
- `[Agent: researcher Investigate proposed change for design document...]` (from `input.subagent_type` + `input.prompt`)
- `[Skill: mine-define add rate limiting]` (from `input.skill` + `input.args`)
- `[Edit: src/ccrecall/content.py old_value new_value]` (from `input.file_path` + capped `input.old_string` + `input.new_string`)
- `[Read: src/ccrecall/content.py]` (from `input.file_path`)
- `[Grep: extract_text_content]` (from `input.pattern`)
- `[UnknownNewTool: field1_value field2_value]` (automatically captured by generic extraction)

Cap: `TOOL_CONTENT_CAP = 300` per tool_use block (applied to the joined field values, not the tag). Individual string fields are capped at 200 chars before joining.

### Storage layer (`message_ops.py`, `schema.py`, `db.py`)

`build_message_row` calls the extended `extract_text_content`, receives `tool_content`, and includes it in the INSERT. The early return changes from `if not text: return None` to `if not text and not tool_content: return None`. The existing dead columns `tool_summary` and `has_tool_use` are left as-is — `branches.tool_counts` already serves this purpose with 4 live consumers.

Schema migration `_migrate_to_v4`: additive `ALTER TABLE messages ADD COLUMN tool_content TEXT`, following the v3 pattern (self-guarded with duplicate-column error handling). Runs unconditionally outside the version gate, alongside `_migrate_to_v3` in `_apply_migrations` — the conservative default, since `ALTER TABLE ADD COLUMN` is cheap, self-guarding, and this avoids multi-process-safety issues if old and new code run concurrently against one DB file. Bump `SCHEMA_VERSION` from 3 to 4.

### FTS aggregation layer (`parsing.py`)

`build_aggregated_content` / `aggregate_branch_content` already concatenate `messages.content` plus file-path and commit sections. Extend the SELECT to include `tool_content` and append it as a `__tools__` section in the aggregated text, similar to the existing `__files__` and `__commits__` sections.

### Embedding layer (`db.py`, `summarizer.py`, `embed_ops.py`)

**Data supply**: `db.py:fetch_branch_messages` provides the message dicts that `build_exchange_pairs` consumes. Its SELECT currently reads only `m.role, m.content, m.timestamp, ...` — it must add `m.tool_content` to the SELECT and include it in the returned dicts. Without this change, `build_exchange_pairs` has no `tool_content` key to read regardless of how it's modified.

**Exchange pairing**: `build_exchange_pairs` reads `messages.content` to pair user/assistant text. Extend it to also read the `tool_content` key from each message dict and append it to the assistant's text in each exchange pair. This makes tool content part of the embedded vector, enabling semantic search.

**Vestigial regex deletion**: `build_exchange_pairs` has a dead regex at line ~100: `cleaned = re.sub(r"\[Tool: \w+\]", "", m["content"]).strip()`. No upstream producer emits `[Tool: X]` markers (confirmed — the regex is exercised only by tests that hand-construct synthetic content). Since this function is already being edited for the tool-only turn guard below, delete the regex entirely — it removes a permanent "don't collide with this format" constraint from future marker decisions at zero marginal cost.

**Tool-only turn guard**: After removing the regex, the remaining guard (`if cleaned: current_asst_parts.append(cleaned)`) skips assistant messages with empty prose content — which is exactly the tool-only turns this design exists to surface. The guard must be extended: if `cleaned` is empty but the message has non-empty `tool_content`, that tool content must still be appended to `current_asst_parts`. Otherwise tool-only exchanges are silently excluded from embedding, defeating the core scenario (FR#4, AC#4).

**Consecutive-type collapsing**: Before appending tool_content to the exchange text for embedding, collapse runs of consecutive identical tool types into a single summary line. For example, 15 consecutive `[Read: ...]` lines become `[Read: 15 files including src/ccrecall/content.py, src/ccrecall/db.py, ...]`. This prevents repetitive markers in tool-heavy exchanges (orchestration sessions with 50+ tool calls) from pushing meaningful prose out of `cap_for_embedding`'s head+tail truncation window. Prior art research confirms the dilution risk is real for repetitive structured content but that prose-like markers at moderate density actually help retrieval (Anthropic's contextual retrieval reports 35-67% improvement). The collapsing targets the specific failure mode — marker repetition — without removing content.

The `chunks` table's `user_text` and `assistant_text` columns (materialized at embed time) will now contain tool content in the assistant text, so `hydrate_snippets` in `search_vector.py` will naturally surface it in search result excerpts without additional changes to the hydration query.

### Context summary and rendering paths (`summarizer.py`, `formatting.py`)

Two additional paths consume message content independently of `fetch_branch_messages` and must also include `tool_content`:

**`summarizer.py:compute_context_summary`** runs its own standalone SQL query (`SELECT m.role, m.content, m.timestamp ...`) to build the SessionStart context injection. This query must be extended to select `m.tool_content` and the downstream summary builder must include it — otherwise the proactive-recall surface (the design's own motivating use case) stays blind to tool content.

**`formatting.py:format_markdown_session`** renders messages as `**role:** content` for `ccrecall recent` output. It must append `tool_content` after `content` when present, so tool actions appear in plain-markdown recall.

### Backfill (`hooks/backfill_tool_content.py`)

New backfill command reusing `backfill_query.py`'s shared primitives (`BATCH_SIZE`, no-progress guard, `BACKFILL_BATCH_DELAY_SECONDS`) and `backfill_status.py`'s `--status` reporting — not re-implementing them:
- Batch-processes sessions from `import_log` where the JSONL file still exists on disk
- Re-parses each JSONL file using `parse_all_with_uuids` + `find_all_branches` (the same parsing pipeline as `sync_session`) to reconstruct the branch→message UUID mapping
- updates `messages.tool_content` for existing rows by matching on `(session_id, uuid)`
- For tool-only turns that were previously skipped (no existing `messages` row): calls `build_message_row` for row construction and `insert_new_messages` for insertion (reusing its `ON CONFLICT(session_id, uuid) DO NOTHING` guard for race safety against concurrent forward syncs, and ensuring extraction logic is shared — not reimplemented — so future changes to `build_message_row`'s guard or field logic propagate to both paths). Links the new row to the correct branch via `branch_messages` using the branch→UUID mapping from the parse step. Without this linkage, new rows would be orphaned — invisible to `aggregate_branch_content` (which joins through `branch_messages`) and to `build_exchange_pairs` (which reads messages through the same join)
- Rebuilds `branches.aggregated_content` for affected branches (re-runs `aggregate_branch_content` with the updated message set)
- Wraps all writes for one session in a single `SAVEPOINT`, released only after all succeed — mirroring `backfill_embeddings.py`'s per-row pattern. A crash rolls back to "session untouched," never "half-linked"
- Resets `branches.embedding_version = NULL` for every branch it touches, so those branches re-enter `backfill embeddings`'s existing watermark-based eligible set — without this, already-embedded branches would silently skip re-embedding and tool content would never reach the semantic search index
- Does NOT re-embed itself (that's the existing `backfill embeddings` command's job — run it after tool-content backfill to pick up the new text)
- Progress logging, no-progress guard, best-effort skip for missing files

CLI registration: `ccrecall backfill tool-content [--days N] [--limit N] [--status]` in `cli/commands.py`.

### Code leverage

| Sub-problem | Existing code | Coverage |
|---|---|---|
| Per-tool input extraction | `content.py:extract_text_content` | Partial — framework exists, add tool dispatch |
| Per-tool dispatch pattern | `session_tail.py:_tool_event()` | Partial — reuse dispatch structure, raise caps |
| Store tool_content in DB | `message_ops.py:build_message_row` | Partial — add column to INSERT, fix early return |
| Include in FTS | `parsing.py:build_aggregated_content` | Partial — add `__tools__` section |
| Supply tool_content to embedding | `db.py:fetch_branch_messages` | Partial — add `tool_content` to SELECT and returned dicts |
| Include in embeddings | `summarizer.py:build_exchange_pairs` | Partial — append tool_content to assistant text, fix empty-content guard |
| Schema migration | `db.py:_migrate_to_v3` | Full — reuse as-is |
| Backfill orchestration | `hooks/backfill_embeddings.py` | Partial — reuse batch/progress pattern |
| Populate dead columns | N/A — dropped from scope | `branches.tool_counts` already serves this purpose |

## Implementation Preferences

No specific implementation preferences — follow codebase conventions.

## Replacement Targets

- **`message_ops.py:build_message_row` early return** (line 61-62): `if not text: return None` is replaced with `if not text and not tool_content: return None`. The old behavior (dropping tool-only turns entirely) is the bug being fixed.
- **`message_ops.py:build_message_row` INSERT columns** (line 94-101): The INSERT statement gains `tool_content`. The existing dead columns `tool_summary`/`has_tool_use` are left as-is.

## Migration

**Schema change**: `ALTER TABLE messages ADD COLUMN tool_content TEXT` — additive, nullable, no default needed. Existing rows get `NULL` until backfilled. Self-guarded (v3 pattern) so concurrent connections don't error.

**Forward path**: New syncs automatically populate `tool_content` via the updated `build_message_row`. No user action needed.

**Retroactive path**: `ccrecall backfill tool-content` re-parses JSONL files and updates existing rows + inserts tool-only turn rows. After running, `ccrecall backfill embeddings` should be run to re-embed branches with the new text.

**Reversibility**: Reversible for sessions whose JSONL files are still present on disk — re-running the backfill reconstructs `tool_content` from source. For sessions whose JSONL has been deleted or rotated, historical tool content is permanently lost on column drop. The `content` column is untouched.

## Convention Examples

### Extraction with per-tool dispatch (`content.py`)

**Source:** `src/ccrecall/content.py:103-113` (`extract_files_modified`)

```python
def extract_files_modified(content: str | list) -> list[str]:
    if isinstance(content, str):
        return []
    files: list[str] = []
    for item in content:
        if item.get("type") != "tool_use":
            continue
        name = item.get("name", "")
        inp = item.get("input", {}) if isinstance(item.get("input"), dict) else {}
        if name in ("Edit", "Write", "MultiEdit") and "file_path" in inp:
            files.append(inp["file_path"])
    return files
```

### Additive schema migration (`db.py`)

**Source:** `src/ccrecall/db.py:432-446` (`_migrate_to_v3`)

```python
def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    for column, decl in (("file_size", "INTEGER"), ("file_mtime", "REAL")):
        try:
            conn.execute(f"ALTER TABLE import_log ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
```

Note: no `conn.commit()` inside the function — the caller (`_apply_migrations`) commits after invoking the migration. New migrations should follow this same boundary.

### Backfill batch pattern (`hooks/backfill_embeddings.py`)

**Source:** `src/ccrecall/hooks/backfill_embeddings.py` (orchestrator loop)

```python
last_batch_ids: set[int] = set()
while True:
    rows = cursor.execute(f"SELECT id FROM branches {where} ORDER BY id LIMIT ?", (*params, BATCH_SIZE)).fetchall()
    if not rows:
        break
    current_ids = {r[0] for r in rows}
    if current_ids == last_batch_ids:
        logger.warning("no-progress: same batch as last iteration, aborting")
        break
    last_batch_ids = current_ids
    for (branch_id,) in rows:
        # process branch...
    conn.commit()
    time.sleep(BACKFILL_BATCH_DELAY_SECONDS)
```

## Alternatives Considered

**Same column (append to `messages.content`)**: Simpler — zero plumbing changes downstream. Rejected because it muddles prose and tool content, prevents independent querying, and makes future format changes to tool markers require re-processing the prose column.

**Index tool_result content (command output)**: Would make command output searchable. Rejected for this iteration — tool results can be enormous (40KB+ observed), highly variable in structure, and would dominate embedding vectors. Tool inputs are higher signal-to-noise.

## Test Strategy

### Existing Tests to Adapt

- `tests/test_content.py` — `extract_text_content` tests need updating to verify the new `tool_content` return value. Existing tests that assert on the 4-tuple return will break and need the 5th value added.
- `tests/test_summarizer.py` — `build_exchange_pairs` tests need updating to include `tool_content` in the mock message data and verify it appears in exchange text.
- `tests/test_integration.py` — Integration tests that assert on `messages` table contents and `aggregated_content` need updating for the new column.

### New Test Coverage

- **FR#1, FR#2**: Unit tests in `test_content.py` for each tool type's extraction — AskUserQuestion, Agent, Bash (with and without description), Skill, Edit (with and without old/new), Read, Grep, Glob, Write, unknown tool.
- **FR#3, FR#4**: Integration test verifying tool-only assistant turns produce `messages` rows with empty `content` and populated `tool_content`.
- **FR#5**: Integration test verifying `aggregated_content` includes tool markers after sync.
- **FR#7**: Integration or unit test verifying search-messages snippet includes tool content.
- **FR#8**: Integration test for the backfill command — sync a session, verify `tool_content` is NULL, run backfill, verify populated.
- **FR#9**: Unit test verifying cap truncation for oversized tool inputs.
- **FR#10**: Unit test verifying consecutive-type collapsing before embedding.

### Tests to Remove

No tests to remove.

## Documentation Updates

- **CLAUDE.md**: Update the `content.py` / architecture description to mention `tool_content` column and tool extraction. Update the `messages` table column list. Update the `build_exchange_pairs` description to mention tool content inclusion.
- **CLI help text**: The new `backfill tool-content` subcommand needs help text (handled by cyclopts decorator).

## Impact

### Changed Files

- **modify** `src/ccrecall/content.py` — add per-tool extraction to `extract_text_content`, new cap constants
- **modify** `src/ccrecall/message_ops.py` — update `build_message_row` return and INSERT to include `tool_content`; change early-return guard
- **modify** `src/ccrecall/schema.py` — add `tool_content TEXT` to `SCHEMA_CORE` messages table definition
- **modify** `src/ccrecall/db.py` — add `_migrate_to_v4`, bump `SCHEMA_VERSION` to 4, wire into `_apply_migrations`; extend `fetch_branch_messages` SELECT to include `tool_content`
- **modify** `src/ccrecall/parsing.py` — extend `build_aggregated_content` / `aggregate_branch_content` to include `tool_content`
- **modify** `src/ccrecall/summarizer.py` — extend `build_exchange_pairs` to include `tool_content` in exchange text
- **modify** `src/ccrecall/summarizer.py` — extend `compute_context_summary`'s SQL to select `tool_content`; thread it through context summary builder
- **modify** `src/ccrecall/formatting.py` — extend `format_markdown_session` to append `tool_content` after `content`
- **modify** `src/ccrecall/session_tail.py` — update 4 call sites of `extract_text_content` from 4-value to 5-value tuple unpacking (lines 139, 172, 208, 269)
- **modify** `src/ccrecall/cli/commands.py` — register `backfill tool-content` subcommand
- **create** `src/ccrecall/hooks/backfill_tool_content.py` — backfill orchestrator
- **modify** `tests/test_content.py` — update extraction tests, add tool content tests
- **modify** `tests/test_summarizer.py` — update exchange pair tests
- **modify** `tests/test_integration.py` — update integration assertions
- **modify** `CLAUDE.md` — update architecture documentation

### Behavioral Invariants

- Existing `messages.content` column values must not change — tool content goes into the new column, not the existing one.
- Existing FTS search behavior for prose content must not regress — adding tool content to `aggregated_content` adds results, never removes them.
- Hook stdout protocol (`{"continue": true}` / `{}`) must not be affected.
- Hook hot path import weight must not increase — no new heavy imports in the extraction path.

### Blast Radius

- **Embedding vectors change**: After backfill + re-embed, semantic search results may shift because exchange text now includes tool content. This is intentional — matches improve — but existing result rankings may differ.
- **DB size increase**: Tool content adds text to every message row. For tool-heavy sessions (orchestration runs), this could measurably increase database size.
- **Aggregated content growth**: Tool markers multiply the size of `aggregated_content`, which is fully rebuilt on every sync. Expected worst case: an orchestration session with ~1,000 tool calls × 200 chars per marker ≈ 200KB re-read/re-joined/re-written per Stop event. Acceptable for this iteration — sync runs off the hot path in a detached process — but a stated, reviewed trade-off rather than an unexamined one.
- **Backfill runtime**: Re-parsing JSONL files for all synced sessions could take significant time for large databases. The batch + progress pattern keeps this observable.

## Open Questions

None.
