# Open questions from the state and visibility audit

Run on 2026-11-01, about 12:45-12:55, against the live 0.26.2 install. No live data was changed.

**Result:** the repair has landed on its own. Nothing needs a manual run today. The remaining real problem is 358 branches that semantic search can't fully reach.

The work is tracked in the [State Visibility milestone](https://github.com/NodeJSmith/claude-code-recall/milestone/1). Its "Done when" sets the scope, and each issue's "After" line sets the order.

## Live state then

| Area | State |
|---|---|
| Incomplete fixable sessions | 0. SessionStart's repair ran 11:30-11:31: 105 of 105, 10,587 messages. |
| Transcripts deleted, messages still stored | 425 sessions |
| Awaiting today's activity | 2 sessions, 13 turns |
| Queued for embedding | 122 branches, for the 00:30 run |
| Draft quality | 8 branches, for the 00:30 run |
| Stamped embedded but missing vectors | 358 branches, 866 exchanges |

The audit's 366 and 884 fell to 358 and 866 because the repair cleared the watermark on some of those branches.

## Why branches are stamped but missing vectors (#231)

There are two causes. Both predate the 0.26.2 install.

**No chunks at all (232 branches): #155.** In 0.22.1, inserts into `branch_messages` failed silently. A branch with no links produces no exchanges, and `embed_branch_chunks` stamps a zero-exchange branch as complete. The links were restored later without clearing the stamp.

Evidence: 196 of the 232 appear in the Aug 22 backup with zero links, stored messages and a current watermark. 181 ended between Aug 15 and Aug 22. Of the other 36, 30 aren't in that backup and 6 had links; those aren't individually explained.

**Missing the newest exchanges (126 branches): imports that didn't clear the stamp.** Import runs with `embed=False`, and before #230 only the embedding step cleared the watermark. A message imported after the last embedding left the branch marked complete. #230 added the clear today.

Evidence: in 125 of the 126, every missing exchange is newer than the newest chunk. This is inferred from that shape and the code, not reproduced on old code.

**Is it still happening?** The two newest affected branches ended at about 07:33 and 07:49 local, before the 10:30 install. Rerunning `probes/stamped_without_chunks.py` shows whether the count grows; it shouldn't.

**The nightly run won't fix these.** `build_selection` selects on stale watermarks, chunks lacking vectors, and draft chunks. A chunk that was never created matches none of those.

**They can be recovered.** On a private copy, clearing the watermark on 4 sample branches and running the backfill completed all 4 (6 exchanges, 42 s including model load).

## Why imports hit `database is locked` (#232, #233)

There were 17 import failures today, through 12:11. All were retried by a later run, so no data was lost, but every failure is a stack trace in the log and an aborted run. A one-off run can abort partway, as the repair did at 10:37 and 10:42.

- **Sync holds a write transaction through embedding.** `sync_session` runs inside one connection context, with inference between its first writes and the commit. 47 of 228 syncs today exceeded the 5 s busy timeout; the longest took 41 s. Six failures overlap one of these.
- **Import can fail without waiting.** `import_project` opens a deferred `BEGIN`, reads, then writes. In WAL mode, if another writer commits in between, the write fails immediately and the busy timeout doesn't apply (`probes/snapshot_upgrade.py`; `BEGIN IMMEDIATE` doesn't fail this way). The 12:11 failure came 3.6 s after its service started, too soon to have waited out a 5 s timeout, so a longer timeout alone won't fix this.
- **Plain imports have no concurrency guard.** Only `--repair-gaps` takes the PID file, so the timer's import can overlap a SessionStart import.

The holder at 10:37 and for other failures without a long sync overlap isn't confirmed. A concurrent import is the likely candidate.

## Subagent transcripts

Excluding them is deliberate: #86 closed as not planned on 2026-08-03, because decisions land back in the main transcript. That leaves 23,763 files and 11 GB unindexed. The README never says so, and neither does `status`.

## Status and doctor (#234, #235, #237)

Speed doesn't force the split. `status` takes about 4.4 s, nearly all startup, and the honest coverage check takes about 2 s on 7,513 branches. The reason to split is the reader: a glance versus a diagnosis.

Both should render one shared set of findings, so they can't disagree. `status` must still carry the verdict, or it repeats today's problem of "100%" alongside a real fault.

## Rerunning

`probes/` has the new read-only checks, run with the install's Python:

- `lock_timeline.py YYYY-MM-DD`: one day's import failures against overlapping syncs
- `snapshot_upgrade.py`: reproduces the immediate lock failure on a throwaway database
- `stamped_zero_detail.py <live_db> <older_backup_db>`: zero-chunk branches against an older backup
