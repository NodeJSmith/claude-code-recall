---
task_id: "T04"
title: "Add alert, schedule CLI, dismiss, and three-state caveat"
status: "done"
depends_on: ["T01"]
implements: ["FR#7", "FR#8", "FR#16", "FR#17", "FR#18", "AC#5", "AC#12"]
---

## Summary

The user-facing UX layer: add the `ALERT_DRAFT_QUALITY_VECTORS` alert class with schedule-marker suppression, the `ccrecall backfill schedule write/clear/status` subcommand, the `--dismiss` flag on `backfill embeddings`, and the three-state `compute_caveat`/`branch_embedding_coverage` distinguishing "not embedded" from "draft quality."

## Target Files

- modify: `src/ccrecall/health.py` — add `ALERT_DRAFT_QUALITY_VECTORS` constant and 3-tuple in `_ALERT_PROSE` (after line 316), add schedule-marker read helper
- modify: `src/ccrecall/hooks/context_alerts.py` — wire draft-quality alert with DB query (using `FULL_QUALITY_TOKEN_LIMIT` from health.py) and schedule-marker check
- modify: `src/ccrecall/cli/commands.py` — add `schedule` subcommand group under `backfill_app` with `write`/`clear`/`status` commands, add `--dismiss` flag to `cmd_backfill_embeddings`
- modify: `src/ccrecall/db_vec.py` — update `branch_embedding_coverage` (line 201) to return three-state: (embedded_full, embedded_draft, total)
- modify: `src/ccrecall/search_conversations.py` — update `compute_caveat` (line 184) to use three-state coverage and produce accurate prose
- modify: `src/ccrecall/status.py` — adapt `branch_embedding_coverage` call (line 115) for three-state return type
- modify: `src/ccrecall/search_cli.py` — adapt `branch_embedding_coverage` call (line 150) for three-state return type
- modify: `tests/test_health.py` — add tests for `ALERT_DRAFT_QUALITY_VECTORS`, schedule-marker suppression, dismiss marker
- modify: `tests/test_context_alerts.py` — add test for draft-quality alert wiring with DB query
- create: `tests/test_cli.py` — tests for `backfill schedule write/clear/status` and `backfill embeddings --dismiss`
- create: `tests/test_db_vec.py` — test for three-state `branch_embedding_coverage`
- create: `tests/test_search_conversations.py` — test for three-state `compute_caveat`
- read: `src/ccrecall/health.py` — full file, especially `_ALERT_PROSE` (301-316), `evaluate_alerts` (228), `build_alert_block` (331), `ALERT_SNOOZE_PATH` (42)
- read: `src/ccrecall/hooks/context_alerts.py` — `proactive_alert_block` (30-112)
- read: `src/ccrecall/cli/commands.py` — `backfill_app` (line 20), existing subcommands (123-200)
- read: `src/ccrecall/db_vec.py` — `branch_embedding_coverage` (201-222), `CHUNK_EMBEDDABLE_BRANCH_FILTER`
- read: `src/ccrecall/search_conversations.py` — `compute_caveat` (184-203)

## Prompt

Implement the alert, schedule CLI, dismiss, and three-state caveat.

**Alert constant + prose (health.py):** Add `ALERT_DRAFT_QUALITY_VECTORS = "draft_quality_vectors"` near the other `ALERT_*` constants (line 56-58). Add to `_ALERT_PROSE` (after line 316):
```python
ALERT_DRAFT_QUALITY_VECTORS: (
    "Some conversations have draft-quality embeddings — semantic search works but may miss context from long exchanges.",
    "the sync path caps embeddings at 4096 tokens for memory safety; full 8192-token quality requires a scheduled backfill",
    "run `ccrecall backfill embeddings` to upgrade existing drafts; to prevent recurrence, set up a scheduled job (`ccrecall backfill schedule write`) or dismiss permanently (`ccrecall backfill embeddings --dismiss`)",
),
```

**Schedule marker path (health.py):** Add `BACKFILL_SCHEDULE_PATH = RUNTIME_DIR / "backfill-schedule.json"` near `ALERT_SNOOZE_PATH` (line 42). Add a helper `read_schedule_marker(path: Path | None = None) -> dict | None` that reads and parses the JSON, returning `None` on any error (missing file, invalid JSON). Check that the returned dict contains `"configured_at"` or `"dismissed_at"`.

**Alert wiring (hooks/context_alerts.py):** Import `ALERT_DRAFT_QUALITY_VECTORS`, `FULL_QUALITY_TOKEN_LIMIT`, `BACKFILL_SCHEDULE_PATH`, `read_schedule_marker` from `health`. In `proactive_alert_block` (after the tool-content check around line 92), add:
1. `SELECT COUNT(*) FROM chunks WHERE cap_tokens IS NOT NULL AND cap_tokens < ?` with `FULL_QUALITY_TOKEN_LIMIT`
2. If count > 0, call `read_schedule_marker()`
3. If marker is None (absent, invalid, or missing required fields), add `ALERT_DRAFT_QUALITY_VECTORS` to `active_keys`

**Schedule CLI (cli/commands.py):** Create a `schedule_app = App(name="schedule")` sub-app, register it under `backfill_app`. Three commands:
- `write`: writes `{"configured_at": "<ISO timestamp>"}` to `BACKFILL_SCHEDULE_PATH`. Print confirmation.
- `clear`: deletes `BACKFILL_SCHEDULE_PATH` if it exists. Print confirmation.
- `status`: reads the marker, prints whether it exists, its type (schedule/dismiss/none), when written, and runs `SELECT COUNT(*) FROM chunks WHERE cap_tokens IS NOT NULL AND cap_tokens < ?` to show remaining draft chunks.

**--dismiss flag (cli/commands.py):** Add `dismiss: bool = False` parameter to `cmd_backfill_embeddings` (line 134). If `dismiss` is True, write `{"dismissed_at": "<ISO timestamp>"}` to `BACKFILL_SCHEDULE_PATH` and return early (don't run backfill).

**Three-state coverage (db_vec.py):** Change `branch_embedding_coverage` return type to `tuple[int, int, int]` — `(embedded_full, embedded_draft, total)`. `embedded_full` = branches with watermark at current version (existing query). `embedded_draft` = branches where watermark is not current BUT have at least one chunk with `cap_tokens IS NOT NULL AND cap_tokens < FULL_QUALITY_TOKEN_LIMIT`. `total` = all embeddable branches (existing query).

**Three-state caveat (search_conversations.py):** Update `compute_caveat` to use the three-state return. If there are draft-quality branches, return a message like "N% of history fully embedded; M branches have draft-quality embeddings" instead of treating them as "not embedded."

## Focus

- `context_alerts.py:proactive_alert_block` wraps everything in `try/except Exception: return ""`. The new code must be inside this defensive wrapper.
- `context_alerts.py` currently imports only `ALERT_CANT_PERSIST`, `ALERT_EMBEDDINGS_FAILING`, `ALERT_TOOL_CONTENT_INCOMPLETE` from `health`. Add the new imports to the existing import line (line 15-17).
- `build_alert_block` (health.py:331) uses `custom_causes` for overriding the default cause string — only `ALERT_CANT_PERSIST` and `ALERT_EMBEDDINGS_FAILING` use this today. The new alert uses static prose (no dynamic cause), so no `custom_causes` entry is needed.
- The `--dismiss` flag and `schedule write` both write to the same file (`BACKFILL_SCHEDULE_PATH`). `--dismiss` writes `dismissed_at`; `schedule write` writes `configured_at`. If both are present, either field satisfies FR#8's suppression check.
- `branch_embedding_coverage` is called by `compute_caveat` (search_conversations.py:196), `status.py` (the `ccrecall status` command), and potentially other status surfaces. Changes to its return type affect all callers. Check `status.py` for the exact call pattern.
- `cli/__init__.py` defines `app` and `backfill_app`. The `schedule_app` should be registered via `backfill_app.command(schedule_app)` or the cyclopts equivalent.
- **Gap check:** `status.py:115` and `search_cli.py:150` both call `branch_embedding_coverage(conn)` and destructure the result as `(embedded, total)`. Since this task changes the return type to `(embedded_full, embedded_draft, total)`, both callers will break. Update them to handle the three-tuple — they can sum `embedded_full + embedded_draft` for backward-compatible display, or show the draft count separately.

## Verify

- [ ] FR#7: With draft-quality chunks in DB and no schedule marker, `proactive_alert_block` includes `ALERT_DRAFT_QUALITY_VECTORS` in its output
- [ ] FR#8: With a schedule marker containing `configured_at`, the alert does not fire; with `dismissed_at`, the alert does not fire; with empty `{}`, the alert fires
- [ ] FR#16: `branch_embedding_coverage` returns correct counts for full, draft, and not-embedded branches; `compute_caveat` produces appropriate prose for each state
- [ ] FR#17: `ccrecall backfill embeddings --dismiss` writes a `dismissed_at` field to the schedule marker
- [ ] FR#18: `ccrecall backfill schedule write` creates the marker; `clear` removes it; `status` reports correct state
- [ ] AC#5: Insert chunks with cap_tokens=4096 → alert fires. Write schedule marker → alert silent. Remove marker → alert fires again
- [ ] AC#12: Branch with only draft-quality chunks shows "draft quality" in `compute_caveat`, not "not embedded"
