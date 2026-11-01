# State and visibility audit

Run on 2026-11-01 against the maintainer's live 0.26.2 install.

**Result: 0 of 11 questions pass.** Two get partial answers and nine fail. A user cannot tell from ccrecall whether it is healthy, what is wrong, or what to do next. Answering these questions took log archaeology, direct SQL, and purpose-written scripts against the package's internals.

The current install is degraded but is not losing data. The audit also found a problem nobody was looking for: 366 branches report as embedded but are missing vectors (see Q5).

**Update, same day:** this is a snapshot from before 11:30. SessionStart's repair then finished on its own (105 of 105), so the repair findings below no longer describe the live install and no manual run is needed. The causes of the missing vectors and lock errors are now known. See `followup.md`, which supersedes "Suggested order" here.

**Test:** one command answers the question in about 30 seconds, without reading logs or code.

## Results

| # | Question | Result | What ccrecall shows | True answer on this install |
|---|---|---|---|---|
| 1 | Did the release's repair work? | Fail | Nothing. The log shows "109 repaired", from test runs before the install. | Partly. 4 of 109 sessions fixed; runs aborted at 10:37 and 10:42 with `database is locked`. |
| 2 | Is my history complete? | Partial | Only with `--check-ingestion`. Plain `status` says "not checked". | No. 105 sessions are incomplete. |
| 3 | Is anything broken? | Fail | `status` exits 0 and never mentions failed runs. | Yes: the repair keeps aborting, and Q5's missing vectors are unexplained. |
| 4 | What should I do now? | Fail | Nothing. | Run the repair; the nightly backfill handles the rest. |
| 5 | Are all embeddings done and healthy? | Fail | "100%", "18307/18307 chunks at current version". | The model and vector extension work. 15 branches are queued. 366 more are stamped embedded but lack vectors. |
| 6 | Are all embeddings full quality? | Partial | "7 branches" in `status`, "11 chunks" in `backfill embeddings --status`. No word on when they clear. | No. All 7 are from today, and the nightly backfill upgrades them. |
| 7 | Are the missing ones normal or a problem? | Fail | One "remaining" number. | Normal. At the check with 14 remaining: 10 from today (7 held back only by draft chunks), 4 just repaired. |
| 8 | Am I missing anything fixable? | Fail | Counts only, with no way to list sessions. | Yes: those 105 sessions. |
| 9 | If a session has a transcript, why isn't it fully ingested? | Fail | Nothing. | Lock contention. On a copy with no contention, all 105 repaired. |
| 10 | What runs when, and by whom? | Fail | No single place, and the docs conflict (below). | See "What runs when". |
| 11 | Overall health, with or without numbers? | Fail | No verdict, and undefined internal terms. | Degraded, no data lost. Run the repair. |

## Evidence

### Repair (Q1, Q2, Q8, Q9)

- Live state: 85 gap sessions (15,857 turns) and 20 stale-tail sessions (3,634 turns).
- Simulated `ccrecall import --repair-gaps` on an isolated copy with its own `HOME`: 105 of 105 repaired, 10,587 messages recovered, 30 seconds. Gaps and stale tails both went to 0.
- The copy had no competing writers, so a live run can still abort.
- Writers are the 5-minute `ccrecall import` timer, SessionStart imports, Stop-hook syncs and the nightly backfill. The busy timeout is 5 s, and one lock error aborts the entire repair run.
- Three normal-import failures at 10:38 overlap a 41-second `sync-current`. Nothing logged explains the 10:37 and 10:42 failures, so the lock holder isn't confirmed for those.
- An aborted run's log doesn't say what it finished before aborting. The 10:42 run fixed 4 sessions, visible only because the gap count dropped from 89 to 85.
- Test runs against a database copy write to the live `~/.ccrecall` log unless `HOME` is overridden.

### Sessions that look lost but aren't (Q9)

"Missing or unreadable source: 425 sessions" reads like data loss. Those transcripts are gone from disk, but the database still holds their 54,070 messages, and they remain searchable. Repair can't and needn't do anything for them.

### Transcripts status never mentions (Q8)

- 15,837 top-level transcripts aren't imported. All are in projects matching `exclude_projects` in `config.json`, so this is deliberate.
- 23,763 subagent transcripts have no `import_log` rows, so subagent work isn't indexed. Status doesn't mention this. Whether it is intended is an open question.

### Embeddings (Q5, Q6, Q7)

- **"Remaining" mixes unrelated things.** The watermark stays below current while any chunk is draft quality, so a searchable draft branch also counts as not embedded. Repaired sessions land in the same number. On the copy, repair moved "remaining" to 121.
- **Coverage only counts chunks that exist.** "18307/18307" can't show a chunk that was never created.
- **Unexplained: 366 stamped branches lack vectors.** Comparing stored chunks with `fetch_branch_messages` + `build_exchange_pairs`, the pipeline's own input:
  - 366 branches at the current `embedding_version` have 884 exchanges with no chunk, about 4.6% of 19,072 exchanges.
  - 239 of them have no chunks at all (for example `25de4d00`, 36 exchanges). 240 ended in August 2026.
  - The nightly backfill selects by watermark, so it probably won't pick these up. That needs confirming. The root cause is unknown.
- **Four surfaces report the same state:** `status`, `search --status`, `backfill embeddings --status` (its help calls it legacy) and `backfill schedule status`.

### Docs and help (Q10, Q11)

- `ccrecall status` isn't documented in the README. Neither are draft quality or `backfill schedule`.
- The README lists `ccrecall import` under "don't call these manually", but it is the only manual way to run a repair.
- The README describes three alert conditions and says catching-up coverage never alerts. The code has five, including `draft_quality_vectors` and `tool_content_incomplete`, and both are in this machine's snooze file.
- `--repair-gaps` help says SessionStart "already does this". That is conditional: `_needs_reimport` only spawns the import when `import_log` has a NULL `file_hash` or the database is absent.
- User-facing help and output contain "Entrypoint B", "chunk-KNN", "active-leaf", "stale tail", "pending tail" and "watermark".

## What runs when

| Work | Trigger | Ships with ccrecall? | If it fails, who finds out |
|---|---|---|---|
| Sync current session, draft embeddings | Stop hook | Yes | `ccrecall-sync.log` only |
| Import plus repair | SessionStart, if `_needs_reimport` | Yes | `ccrecall-import.log` only |
| Context summaries backfill | SessionStart, if needed | Yes | Its log only |
| Model pre-warm | SessionStart | Yes | Its log only |
| Inject context and alerts | SessionStart | Yes | Inline |
| Plain import, no repair | Every 5 minutes | No, user-written systemd timer | Log and journal |
| Embedding backfill, draft upgrade | 00:30 nightly | No, user-written systemd timer | Log and journal |
| Tool-content backfill | Manual only | Yes, opt-in | Terminal |

On this machine, normal operation needs no manual steps once the repair lands. A default install has no scheduled embedding backfill, so drafts stay drafts until someone runs one.

## Target

`ccrecall status` should answer the questions above without flags or log reading:

```
ccrecall: needs attention

Your history is safe, but 105 past sessions are only partly searchable.
The automatic repair has failed 2 times, most recently at 10:42 (database busy).

Next: ccrecall import --repair-gaps

History     7,400 of 7,505 complete · 105 fixable · 425 transcripts deleted, still searchable
Search      Working · 15 sessions queued, next run tonight at 00:30
Quality     7 sessions using draft embeddings, upgraded by tonight's run
Last runs   sync 2 min ago · import 3 min ago · repair failed · backfill 00:33 ok
```

Requirements this implies:

1. **A verdict first:** healthy, working on it, or needs attention, with one next action. Exit code reflects it.
2. **Plain language:** no internal terms in default output. Numbers are grouped by what they mean to the user.
3. **Fixable, normal and deliberate kept apart,** and each group listable by session.
4. **A record of past runs** in place of logs as the only history. Run results include partial progress.
5. **Honest coverage:** expected exchanges compared with existing chunks.
6. **One status surface,** retiring the other three. Existing `--json` fields are a contract, so that change needs a deliberate breaking-change decision.

## Suggested order

Superseded by the update above. The order as of this snapshot:

1. **Fix the repair abort** so a lock doesn't end the run. It blocks the release's own fix.
2. **Find the cause of the 366 branches.** It determines what honest coverage has to report.
3. **Sketch the status redesign** with `/mine-sketch`. It adds run-history state and changes a public `--json` surface.
4. **Rewrite the README's operations sections** to match, and rename the user-facing terms.

Existing issues to fold in: #223, #172, #146, #182.

## Rerunning the checks

Run with the install's Python, passing the database path. All open the database read-only.

- `probes/ingestion_breakdown.py`: categories, ages, top projects, unimported transcripts
- `probes/missing_and_skipped.py`: what deleted-transcript sessions still hold, skipped-transcript totals
- `probes/stamped_without_chunks.py`: branches stamped embedded with missing chunks
