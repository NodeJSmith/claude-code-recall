---
task_id: "T02"
title: "Add tool_content column and wire storage layer"
status: "planned"
depends_on: ["T01"]
implements: ["FR#3", "FR#4", "AC#2"]
---

## Summary
Add `tool_content TEXT` to the `messages` table schema, write the v4 migration, update `build_message_row` to include `tool_content` in the INSERT and fix the early-return guard so tool-only turns produce rows, extend `fetch_branch_messages` to include `tool_content` in its SELECT, and fix `session_tail.py`'s 4 call sites that unpack `extract_text_content` as a 4-tuple.

## Target Files
- modify: `src/ccrecall/schema.py`
- modify: `src/ccrecall/db.py`
- modify: `src/ccrecall/message_ops.py`
- modify: `src/ccrecall/session_tail.py`
- read: `src/ccrecall/content.py` (the 5-tuple return from T01)
- read: `tests/test_db.py` (check for migration test patterns)

## Prompt
### schema.py
Add `tool_content TEXT` to the `messages` table definition in `SCHEMA_CORE`. Place it after the existing `has_tool_use` column. Do NOT modify `tool_summary` or `has_tool_use` — they are dead columns left as-is.

### db.py
1. Bump `SCHEMA_VERSION` from 3 to 4.
2. Add `_migrate_to_v4(conn)` following `_migrate_to_v3`'s pattern — `ALTER TABLE messages ADD COLUMN tool_content TEXT` with duplicate-column error handling. No `conn.commit()` inside the function (the caller commits).
3. Wire `_migrate_to_v4` into `_apply_migrations`: call it unconditionally alongside `_migrate_to_v3`, before the `if current >= SCHEMA_VERSION` early return. This is the conservative default for additive migrations.
4. Extend `fetch_branch_messages` (line ~153): add `m.tool_content` to the SELECT column list and include `"tool_content": row[N]` in the returned dict.

### message_ops.py
1. Update `build_message_row` (line ~40): unpack the 5th return value from `extract_text_content` as `tool_content`. The current line `text, _has_tool_use, has_thinking, _tool_summary = extract_text_content(content)` becomes `text, _has_tool_use, has_thinking, _tool_summary, tool_content = extract_text_content(content)`.
2. Change the early-return guard (line ~61): from `if not text: return None` to `if not text and not tool_content: return None`. This ensures tool-only turns (no prose, but with tool content) produce rows.
3. Add `tool_content` to the INSERT statement in `insert_new_messages` (line ~94): add it to both the column list and the VALUES placeholder. `build_message_row` returns a positional tuple (not a dict) — append `tool_content` as a new element at the end of the returned tuple, and add the corresponding column name and `?` placeholder in the INSERT.

### session_tail.py
Update 4 call sites that unpack `extract_text_content` from 4 to 5 values:
- Line 139: `text, _, _, _ =` → `text, _, _, _, _ =`
- Line 172: `text, _, _, _ =` → `text, _, _, _, _ =`
- Line 208: `text, _, _, _ =` → `text, _, _, _, _ =`
- Line 269: `text, _, _, _ =` → `text, _, _, _, _ =`

## Focus
- `db.py:_migrate_to_v3` (line 432) is the exact pattern to follow for v4. The key: runs outside the version gate, no commit inside the function.
- `db.py:_apply_migrations` (line 448) — `_migrate_to_v3` is called at line 491 before the `if current >= SCHEMA_VERSION` check. Place `_migrate_to_v4` right after it.
- `message_ops.py:build_message_row` (line 40) returns a positional **tuple** (not a dict) — `tool_content` must be appended as a new element, and the INSERT column list in `insert_new_messages` (line 94) must match the tuple order.
- `fetch_branch_messages` (line 153) returns dicts built from `cursor.fetchall()` — verify the column index when adding `tool_content` to the SELECT.
- `session_tail.py` has exactly 4 call sites. If any are missed, `uv run pytest tests/test_session_tail.py` will fail immediately with `ValueError: too many values to unpack`.

## Verify
- [ ] FR#3: `messages` table has a `tool_content TEXT` column (verify via `PRAGMA table_info(messages)`)
- [ ] FR#4: After syncing a transcript with tool-only assistant turns, those turns have rows with `content = ''` and `tool_content` populated
- [ ] AC#2: `SELECT content, tool_content FROM messages WHERE tool_content IS NOT NULL AND tool_content != ''` returns rows including tool-only turns after sync
