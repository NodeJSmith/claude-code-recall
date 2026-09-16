---
task_id: "T01"
title: "Extract per-session classifier from summarize_ingestion and add find_repairable_sessions"
status: "done"
depends_on: []
implements: ["FR#1", "FR#3", "FR#4", "FR#8", "AC#3", "AC#4"]
---

## Target Files

- modify: `src/ccrecall/ingestion_status.py`
- modify: `tests/test_ingestion_status.py`

## Prompt

Read `design/specs/016-stale-tail-import-repair/design.md`'s Problem and Approach sections, and `design/specs/016-stale-tail-import-repair/tasks/context.md` for constraints.

In `src/ccrecall/ingestion_status.py`, `summarize_ingestion()` (lines ~111-202) already does exactly the classification this task needs — for each session with an on-disk transcript, it computes `missing_indices` against the expected active-branch UUIDs, then buckets the session into `pending_tail`, `stale_tail`, `ingestion_gap`, `ok`, or (via the earlier loop) `missing_source`.

1. Extract the per-session classification body (the second `for session_uuid, paths in sources.items(): ...` loop, lines ~151-197) into a generator:

   ```python
   def classify_sessions(
       cursor,
       sources: dict[str, dict[str, list[Path]]],
       now: Instant,
       stale_tail_seconds: int,
   ) -> Iterator[tuple[str, str, int, list[int]]]:
       """Yield (session_uuid, category, session_id, missing_indices) for each session with a verdict.

       category is one of "ok", "pending_tail", "stale_tail", "ingestion_gap".
       Sessions confirmed via the ok-fingerprint cache are yielded as category "ok"
       with an empty missing_indices list — same as a freshly-computed zero-gap session.
       Missing-source sessions are NOT yielded here; that classification happens in
       summarize_ingestion's separate first loop over sessions with no existing files.
       """
   ```

   This is a public function (no leading underscore) rather than a module-private helper: besides `summarize_ingestion` and `find_repairable_sessions` in this same module, `hooks/import_repair.py` (a later task) calls it too, via the `reclassify_session` wrapper added in step 3 below — a genuine cross-module consumer, not just an internal implementation detail (see `coding-style.md`'s "No Default Underscore Prefixes").

   Preserve every existing behavior exactly: the `ingestion_check_cache` short-circuit (cache hit -> yield "ok" immediately, no cache write), the zero-`missing_indices` "ok" path (which still queues an `ok_cache_writes` entry — have the generator's caller, not the generator itself, own `ok_cache_writes` list-building, since that's a side effect specific to `summarize_ingestion`'s aggregation, not to classification itself), and the `_is_contiguous_suffix` + grace-window split between `pending_tail` and `stale_tail`.

   Rework `summarize_ingestion` to call this generator and aggregate `summary[...]` counts and `ok_cache_writes` from its yielded tuples, instead of inlining the loop body. Its signature, return shape (`dict[str, int]`), and every existing test in `tests/test_ingestion_status.py` must keep passing unchanged — this is a pure refactor of `summarize_ingestion`'s internals, not a behavior change.

2. Add a new public function:

   ```python
   def find_repairable_sessions(
       conn: Connection,
       *,
       stale_tail_seconds: int = STALE_TAIL_SECONDS,
       sources: dict[str, dict[str, list[Path]]] | None = None,
   ) -> list[tuple[str, int, int, list[Path]]]:
       """Return (session_uuid, session_id, project_id, filepaths) for every
       session classified as stale_tail or ingestion_gap — the categories a
       force-reimport can actually repair. Excludes pending_tail (likely still
       being written) and missing_source (no surviving JSONL to reimport from)."""
   ```

   Implementation: build `sources` the same way `summarize_ingestion` does when not provided (`import_log_source_index(cursor)`), iterate `classify_sessions`, and for each `(session_uuid, category, session_id, _missing_indices)` where `category in ("stale_tail", "ingestion_gap")`, look up `project_id` via `SELECT project_id FROM sessions WHERE id = ?` and pull `filepaths` from `sources[session_uuid]["existing"]`. Return plain tuples — no new dataclass (matches this module's existing boundary-type convention).

3. Add a small public wrapper for single-session reclassification (FR#8), used later by `hooks/import_repair.py` to check one just-processed session without a full-DB rescan:

   ```python
   def reclassify_session(
       conn: Connection,
       session_uuid: str,
       filepaths: list[Path],
       *,
       stale_tail_seconds: int = STALE_TAIL_SECONDS,
   ) -> str:
       """Return the current classification ("ok", "pending_tail", "stale_tail",
       "ingestion_gap", or "missing_source" for the rare no-session-row race) for
       one session, given its known-existing filepaths.

       Builds a one-entry sources dict and delegates to classify_sessions — the
       ingestion_check_cache short-circuit inside it naturally misses here after a
       real repair (the DB coverage fingerprint changed), so this reflects genuinely
       current state rather than a stale cached verdict.
       """
       cursor = conn.cursor()
       sources = {session_uuid: {"existing": filepaths, "missing": []}}
       result = next(classify_sessions(cursor, sources, Instant.now(), stale_tail_seconds), None)
       return result[1] if result else "missing_source"
   ```

   `classify_sessions` yields exactly one tuple for a `sources` dict containing one session with a non-empty `existing` list in the expected case. The one theoretical exception: internally, `classify_sessions` skips (does not yield) a session when `SELECT id FROM sessions WHERE uuid = ?` finds no row — e.g. a race with `import_session`'s zero-message auto-delete path (`import_conversations.py:139-167`). Practically unlikely for a repair candidate (it already has ≥1 message by definition of being a candidate), but `next(..., None)` with the fallback above degrades that race to a clear `"missing_source"` result instead of an uncaught `StopIteration`.

4. Add tests to `tests/test_ingestion_status.py`:
   - A fixture DB with one `pending_tail` session (transcript mtime within the grace window) — assert `find_repairable_sessions()` does not include it (AC#3).
   - A fixture DB with a mix of `ok`, `pending_tail`, `stale_tail`, and `ingestion_gap` sessions, built so the test knows in advance which session UUID belongs to which category (AC#4). `summarize_ingestion()` returns only aggregate `dict[str, int]` counts — it carries no per-session UUIDs — so this is not a diff between the two functions' return values. Instead: (a) assert `find_repairable_sessions()`'s returned UUIDs are exactly the known `stale_tail` + `ingestion_gap` UUIDs and nothing else, and (b) assert `summarize_ingestion()`, called against the same `sources` snapshot (pass one `import_log_source_index(cursor)` result to both calls, so the comparison isn't racing two independent stat reads), reports `stale_tail_sessions + ingestion_gap_sessions` equal to `len(find_repairable_sessions(...))`.
   - `reclassify_session()`: for a known `stale_tail` session, assert it returns `"stale_tail"`; after simulating the DB catching up (insert the previously-missing message row directly), assert a second call returns `"ok"` — confirming the cache short-circuit doesn't return a stale verdict once DB coverage actually changed.
   - Assert every existing test in this file still passes (the `summarize_ingestion` refactor must not change its observable behavior).

## Verify

- [ ] FR#1: `find_repairable_sessions()` exists, returns `(session_uuid, session_id, project_id, filepaths)` tuples for `stale_tail`/`ingestion_gap` sessions only.
- [ ] FR#3: `find_repairable_sessions()` never includes `pending_tail` or `missing_source` sessions (the filter is `category in ("stale_tail", "ingestion_gap")` — nothing else). This is enforced here, in T01; T02's `repair_sessions()` trusts this filtering rather than re-checking it.
- [ ] FR#4: `summarize_ingestion` and `find_repairable_sessions` both call the shared `classify_sessions` generator — grep confirms no second copy of the missing-indices/contiguous-suffix logic exists.
- [ ] FR#8: `reclassify_session()` exists and returns the current classification for one session, reflecting a real DB coverage change rather than a stale cache hit.
- [ ] AC#3: new test confirms a `pending_tail` session is excluded from `find_repairable_sessions()`'s output.
- [ ] AC#4: new test confirms `find_repairable_sessions()`'s UUID set equals the known-by-construction `stale_tail` + `ingestion_gap` UUIDs, and that `summarize_ingestion()`'s `stale_tail_sessions + ingestion_gap_sessions` count equals `len(find_repairable_sessions(...))` on the same fixture/`sources` snapshot.
- [ ] `uv run pytest tests/test_ingestion_status.py -q` passes, including all pre-existing tests unchanged.
