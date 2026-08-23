# Known Issues

Durable issues discovered during orchestration that were intentionally not fixed in this run.

## KI-001: `status.py:print_status_report` omits draft-quality branch count in the common (backfill-available) path

Status: open
Run: 113
Source: T04
Reason not fixed now: needs-decision
Observed in: T04
Affected files:
- src/ccrecall/status.py

Issue:
`collect_status` (status.py:115) always computes the three-state
`branch_embedding_coverage` tuple and stores it in
`status["embeddings"]["watermark"]["draft_branches"]`, so the JSON payload
always carries the draft-quality count. `print_status_report`
(status.py:221-240) only reads and prints that `draft_branches` value inside
the `else` branch of `if backfill["available"]:` — i.e. only when the
embedding-backfill status query is unavailable (sqlite-vec failed to load or
`count_embedding_status` errored). In the common healthy-install path
(`backfill["available"] is True`), the `if` branch (lines 223-231) prints
branch/chunk counts sourced from `count_embedding_status` but never reads or
prints `embeddings["watermark"]["draft_branches"]`, even though that value is
already sitting in the `status` dict. A user running plain `ccrecall status`
(no `--json`) on a healthy install with draft-quality embeddings sees no
mention of draft-quality coverage, while `ccrecall search --status`
(`search_cli.py:print_status`) surfaces the identical data for the identical
scenario via `db_vec.branch_embedding_coverage`'s three-state return
directly. Verified by reading `status.py:100-174` (JSON always populates
`watermark.draft_branches`) and `status.py:221-240` (the `if` branch never
touches it) — confirms the integration-reviewer's finding.

Why deferred:
The fix is small and low-risk (add a conditional print line inside the `if
backfill["available"]:` branch mirroring the `else` branch's existing
`if draft: print(...)` pattern, or mirroring `search_cli.py:print_status`),
but `status.py`'s "available" branch and `search_cli.py`'s equivalent
function source their embedded/total counts from two different queries
(`count_embedding_status` — legacy per-branch backfill progress — vs.
`branch_embedding_coverage`'s watermark tuple), and neither the task spec
(T04) nor the design's FR#16/AC#12 mandate 1:1 text-output parity between
`ccrecall status` and `ccrecall search --status` — only that
`branch_embedding_coverage`'s three-state return not break existing callers
and that JSON status data be correct (both hold: the destructure was updated
correctly at status.py:115 and the JSON payload is correct). Whether
`print_status_report`'s human-readable "available" branch should be extended
to also surface the watermark-derived draft count (and how tersely) is a
presentation choice not settled by the current task/design scope, so it is
left for deliberate follow-up rather than folded in silently here.

Recommended follow-up:
Add a conditional print inside `if backfill["available"]:` in
`print_status_report` that reads `embeddings["watermark"]["draft_branches"]`
and prints a line when non-zero, matching the wording already used in the
`else` branch (`"  draft quality: {draft} branch(es) searchable but not full
quality"`) or `search_cli.py:print_status`'s phrasing, so `ccrecall status`
shows draft-quality coverage in the common healthy-install case as well as
the unavailable-backfill fallback case.

Acceptance criteria:
- `ccrecall status` (non-JSON) on a DB with `backfill["available"] is True`
  and at least one draft-quality branch prints a draft-quality coverage line.
- A test in `tests/test_status.py` covers this case (currently absent, per
  the integration review).
