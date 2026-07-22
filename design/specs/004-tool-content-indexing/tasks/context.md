# Context: Tool Content Indexing

## Problem & Motivation
Tool_use input content in Claude Code transcripts is invisible to ccrecall search. When an agent or user tries to find specific tool actions from a past session — an AskUserQuestion prompt, a Bash command, an Agent dispatch — neither keyword nor semantic search can surface it. The root cause is in `content.py:extract_text_content`, which only pulls `type == "text"` blocks. All tool_use `input` payloads are discarded (except file paths from Edit/Write/MultiEdit and commit messages). Additionally, assistant turns that consist entirely of tool_use blocks produce no `messages` row at all.

## Visual Artifacts
None.

## Key Decisions
1. **Separate `tool_content` column** — tool action text stored separately from `messages.content` (prose). Both get concatenated into `aggregated_content` for FTS and merged in `build_exchange_pairs` for embeddings, but per-message separation enables independent querying and format evolution.
2. **Generic field-join extraction** — instead of a per-tool dispatch table (duplicating `session_tail.py:_tool_event`), iterate `input` dict top-level string-valued keys generically. Automatically covers new tool types. `session_tail.py` retains its per-type formatting for display.
3. **Backfill reuses shared primitives** — `backfill_tool_content.py` reuses `backfill_query.py`'s `BATCH_SIZE`, no-progress guard, and `backfill_status.py`'s `--status` reporting. Separate CLI command from `backfill embeddings` (different re-run costs).
4. **Embedding version reset** — backfill resets `branches.embedding_version = NULL` for touched branches so `backfill embeddings` re-selects them. Without this, already-embedded branches silently skip re-embedding.
5. **Consecutive-type collapsing** — before embedding, runs of consecutive identical tool types are collapsed (15 `[Read: ...]` → `[Read: 15 files...]`) to prevent marker repetition from diluting prose signal in the embedding vector.
6. **Dead columns left as-is** — `messages.tool_summary` and `messages.has_tool_use` are never populated. `branches.tool_counts` already serves this purpose with 4 live consumers.
7. **Vestigial regex deleted** — `summarizer.py:100`'s `[Tool: \w+]` regex is dead code. Delete it while editing the function.

## Constraints & Anti-Patterns
- Do NOT import fastembed/onnxruntime/sqlite_vec in the extraction path — `extract_text_content` runs on the hook hot path.
- Do NOT break the `if not text: return None` early return without providing an alternative guard — rows with both `content = ''` and `tool_content = ''` should still be skipped.
- Do NOT populate `messages.tool_summary` or `messages.has_tool_use` — dropped from scope.
- Extraction must never raise regardless of `input` shape — missing keys, wrong types, `None` where expected. Defensive extraction only.
- `_migrate_to_v4` runs unconditionally outside the version gate (alongside `_migrate_to_v3`), not inside the gated transaction.
- Backfill must call `build_message_row`/`insert_new_messages` directly for row construction — do not reimplement extraction logic.

## Design Doc References
- `## Architecture → Extraction layer` — generic field-join approach, marker format, cap constants
- `## Architecture → Storage layer` — migration, INSERT changes, early-return guard
- `## Architecture → FTS aggregation layer` — `__tools__` section in aggregated_content
- `## Architecture → Embedding layer` — fetch_branch_messages SELECT, build_exchange_pairs guard, consecutive-type collapsing
- `## Architecture → Context summary and rendering paths` — compute_context_summary SQL, format_markdown_session
- `## Architecture → Backfill` — shared primitives, SAVEPOINT, embedding_version reset
- `## Replacement Targets` — build_message_row early return and INSERT
- `## Migration` — v4 schema, forward path, retroactive path

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
