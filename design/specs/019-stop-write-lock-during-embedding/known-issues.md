# Known Issues

Real issues found while building this feature and intentionally left unfixed.

## KI-001: A stale backfill marker whose PID is reused keeps both embed paths off

Status: open
Recorded: 2026-11-02 (232)
Source: ship-challenge
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/config.py
- src/ccrecall/embed_ops.py
- src/ccrecall/cli/commands.py

Issue:
Sync decides a backfill is running from a PID marker plus a signal-0 probe. If backfill dies without cleanup and a live process reuses its PID, sync skips inference, and the next backfill exits 0 as "already running". Both stay off until that process exits or the marker is removed. Messages still commit and vectors catch up afterward, so no data is lost. D7 accepted the sync half; this adds the backfill half.

Why deferred:
It needs a stale marker plus a PID reuse. Checking the command name risks two backfills at once if the check is wrong, and storing a start time changes the marker format for every job.

Recommended follow-up:
If it happens, check the holder's command name via `ps`. A SIGTERM handler that removes the marker would make stale markers rarer, though the nightly `timeout --kill-after=30s` can still outlast it.

Acceptance criteria:
- A marker whose PID belongs to an unrelated live process doesn't stop sync inference or a new backfill.

## KI-002: The watermark clear can un-stamp a finished branch or erase a content-error marker

Status: open
Recorded: 2026-11-02 (232)
Source: ship-challenge
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/embed_ops.py

Issue:
The clear acts on a diff read with no lock held. A backfill can stamp a branch between sync's read and sync's clear, so sync writes 0 over a true stamp. A sync that finds work on a content-error branch also commits 0 over the -1 marker, even if its embed then fails, so backfill retries that branch.

Why deferred:
Both cases are narrow and heal through the next sync or backfill. Guarding the clear for the marker would remove D4's way of freeing a branch whose content changed during a failing inference, and it wouldn't fix the un-stamp race.

Recommended follow-up:
Revisit only if a poisoned branch retrying nightly turns out to be expensive.

Acceptance criteria:
- A failed sync embed leaves a poisoned branch's marker in place without stranding a branch whose content changed.
