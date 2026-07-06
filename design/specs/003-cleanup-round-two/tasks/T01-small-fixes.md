---
task_id: "T01"
title: "Fix tail mtime bug and relocate sanitize_fts_term"
status: "done"
depends_on: []
implements: ["FR#6", "FR#7", "AC#7", "AC#8"]
---

## Summary
Two independent small fixes bundled because they touch disjoint files and have no dependencies. First, fix `ccrecall tail` to sort sessions by JSONL event timestamps instead of filesystem mtime (issue #45). Second, move `sanitize_fts_term` from `content.py` to `search_query.py` where its only consumer lives.

## Target Files
- modify: `src/ccrecall/session_tail.py`
- modify: `src/ccrecall/search_query.py`
- modify: `src/ccrecall/content.py`
- modify: `tests/test_session_tail.py`
- modify: `tests/test_security.py`
- read: `src/ccrecall/parsing.py` (reference for timestamp field extraction pattern)
- read: `src/ccrecall/recent_chats.py` (reference for correct ordering via `ended_at`)

## Prompt
### Tail mtime fix (FR#6)

In `src/ccrecall/session_tail.py`, replace the mtime-based sort in `list_transcripts()` (lines 258-263). The current code sorts by `p.stat().st_mtime` which is unreliable after reboots.

Add a helper function `_last_event_timestamp(path: Path) -> str` that:
1. Opens the JSONL file and reads the last ~20 lines using `collections.deque(fh, maxlen=20)` (already imported in this file as `deque`)
2. Iterates those lines in order, parsing each as JSON
3. Extracts the `timestamp` field from each entry, keeping the latest one seen
4. Returns the latest timestamp string (ISO 8601 format — string comparison works correctly)
5. Falls back to mtime as an ISO string if no parseable timestamp exists: use `Instant.from_timestamp(path.stat().st_mtime).format_common_iso()` from `whenever` (the project uses `whenever` instead of stdlib `datetime`)

Update `list_transcripts()` to sort by `_last_event_timestamp(p)` instead of `p.stat().st_mtime`.

Also update the `resolve_target()` docstring (lines 266-273) which references "the newest file by mtime" — change it to reflect timestamp-based ordering.

In `tests/test_session_tail.py`, update `TestResolveTarget.test_picks_second_newest` (which currently uses `os.utime()` to set mtimes) to instead write JSONL entries with explicit `timestamp` fields in opposite order to their mtime, then assert the timestamp-ordered file comes first.

### sanitize_fts_term relocation (FR#7)

Move the `sanitize_fts_term` function definition from `src/ccrecall/content.py` (lines 11-32) to `src/ccrecall/search_query.py`. Add `import re` to `search_query.py` — it is not currently imported there but `sanitize_fts_term` calls `re.sub` three times. Remove the function and its imports from `content.py`. The `content.py` module retains `extract_text_content`, `parse_origin`, `extract_plain_text`, and other helpers — verify it does not become empty.

Update `tests/test_security.py` line 3: change `from ccrecall.content import sanitize_fts_term` to `from ccrecall.search_query import sanitize_fts_term`.

## Focus
- `deque` is already imported in `session_tail.py` (line 23) — no new import needed for the tail window
- `json` is NOT currently imported in `session_tail.py` — add `import json` at the top
- `whenever` / `Instant` is NOT currently imported in `session_tail.py` — add `from whenever import Instant` for the mtime fallback
- The project uses `whenever` instead of stdlib `datetime` for the mtime fallback — use `Instant.from_timestamp()` not `datetime.fromtimestamp()`
- `search_query.py` does NOT currently import `re` — add `import re` at the top when moving `sanitize_fts_term` (it calls `re.sub` three times)
- `content.py`'s `import re` must NOT be removed — `extract_text_content` (5 `re.sub` calls) and `extract_commits` (1 `re.search` call) depend on it. Only remove the `sanitize_fts_term` function definition and any imports used exclusively by it (none — `re` is shared)
- The `test_picks_second_newest` test currently creates temporary files and sets mtimes with `os.utime()` — the new test should write actual JSONL content with `{"timestamp": "..."}` entries

## Verify
- [ ] FR#6: `list_transcripts()` sorts by JSONL timestamp, not mtime — verified by a test with timestamps out of mtime order
- [ ] FR#7: `sanitize_fts_term` is defined in `search_query.py` and removed from `content.py`
- [ ] AC#7: A test writes two JSONL files with timestamps in opposite order to their mtime, and `list_transcripts()` returns them in timestamp order
- [ ] AC#8: `from ccrecall.search_query import sanitize_fts_term` succeeds; `from ccrecall.content import sanitize_fts_term` raises ImportError
