# Design: Index Conversation History Across Compaction

**Date:** 2026-10-31
**Status:** built
**Mode:** sketch
**Issues:** #168
**Prior art:** design/research/2026-10-31-compaction-transcript-stitching/prior-art.md, design/research/2026-11-01-repair-negative-cache/prior-art.md, design/research/2026-11-01-parser-change-invalidation/prior-art.md

## Summary

A compacted session loses everything before its last compaction from the index. Claude Code writes each `compact_boundary` with `parentUuid: null` and a `logicalParentUuid` naming the earlier chain's last entry. `find_all_branches` (`src/ccrecall/parsing.py:229`) walks `parentUuid` only, so its walk stops at the boundary. `insert_new_messages` only inserts on-branch messages, so the earlier turns get no `messages` row unless an earlier sync already wrote one, and `exchange_count`, keyword search and embeddings all see just the last chain.

Issue #168 attributes this to bot reconnects. Measurement says compaction: its reported session `f84165c7` has one `compact_boundary` as its second root. Across 25,692 local transcripts, 185 have multiple roots. Following `logicalParentUuid` raises reachable real user turns in them from 1584 to 3312 of 3456, and sessions under half indexed fall from 98 to 2.

The change:

- A single walk helper in `src/ccrecall/parsing.py` follows `parentUuid`, and `logicalParentUuid` at a boundary. It is the only place the active set is computed. `find_all_branches` and `ingestion_status._expected_uuids` both use it, replacing the second copy of the walk inside `_expected_uuids`.
- The compact summary's treatment lives in that helper, so indexing and the ingestion check agree on it for the same input.
- Existing sessions are repaired by the background import that `ccrecall-setup` spawns at SessionStart (`src/ccrecall/hooks/memory_setup.py`), whose argv gains `--repair-gaps`, reusing that path in `src/ccrecall/hooks/import_conversations.py` (D3). The spawn's PID rule lives in `try_acquire_pid_file` in `src/ccrecall/config.py` (D6), the ok-verdict writes and cache invalidation in `src/ccrecall/ingestion_status.py` (D7, D10), and the repair summary's logging in `import_conversations._run` (D9).
- The embedding watermark reset lives in `branch_ops.sync_branch`, next to the link diff that detects the change. `diff_branch_messages` returns nothing today and starts reporting whether links changed (D4).
- Embedding adds no new code path (D5).

Out of scope: `sort_session_files`, `select_active_leaf_entry`, and the seven retained dead `branches` columns.

## Posture

- **Failure handling:** a `logicalParentUuid` that doesn't resolve ends the walk, the same outcome as a boundary today. No fallback ladder through `preservedSegment`, `preservedMessages`, or file order.
- **Compatibility:** the database is a public contract, so no row is removed from `messages` and no table changes shape. Repair adds `messages` rows and changes links and counts, and leaves chunks alone.
- **Scope:** compaction stitching only. The 8 measured user turns in truly disconnected chains stay unindexed as an accepted limit. `preservedSegment` isn't handled because nothing measured points to it.
- **Rollout:** ships in a normal release with no flag. How existing sessions get repaired is D3.
- **Hardening:** project defaults.
- **Altitude:** `build`; one session builds this, and the ledger cites files.

## Decisions

### D1: How should the indexed set span the chains in one transcript?

**Situation:** the walk from the latest leaf stops at the first entry with no parent, which for a compacted session is the last `compact_boundary`. Issue #168's comment proposes indexing every root's subtree or picking the deepest chain, written before compaction was identified as the cause. Rewound forks must stay out: they are abandoned work, and 52 measured unreached user turns are on them. `find_all_branches` keeps a visited set and `_expected_uuids` has none, so the shared helper keeps that set across hops and a revisit ends the walk, the same outcome as a pointer that doesn't resolve. Behavior to pin: a parent cycle or a hop pointing downstream ends the walk on both the indexing and ingestion-check paths.

**Deciding factor:** recover real history while keeping abandoned forks out, with the smallest change to the walk.

| | A: Hop across `logicalParentUuid` | B: Union each root's latest-leaf ancestors | C: Every user and assistant entry |
|---|---|---|---|
| User turns reached (of 3456 measured) | 3312 | 3312 plus the 8 disconnected, plus 67 `/clear` and `/compact` lines | all, including forks |
| Rewound forks | excluded | excluded within each root | indexed as live |
| `/clear` command roots | excluded | indexed as a chain | indexed |
| Code change | one extra lookup in the existing loop | new root and per-root leaf selection | removes the walk; forks need separate handling |
| Invariant 4 (one branch row) | holds | holds | holds |
| Peers | cross-code-organizer, clog, jetbrains-cc-gui | none surveyed | episodic-memory, engineering-notebook, ccusage |

**Recommendation:** A, because it reaches almost everything the other options do and leaves out what shouldn't be indexed, with one extra lookup in the walk.
**Pick B instead if** you want the 8 turns in truly disconnected chains indexed, and accept `/clear` roots and a new leaf-per-root rule. **Pick C instead if** you'd rather have abandoned forks in search than ever miss a turn.
**Reversibility:** easy (a parse change plus a repair run)
**Ratified:** Chose hopping across `logicalParentUuid` over per-root unions and indexing every entry, to recover real history while keeping abandoned forks out with one extra lookup in the walk, accepting that the 8 measured turns in truly disconnected chains stay unindexed.

### D2: What happens to the compact summary message?

**Situation:** after each boundary sits a synthetic user message, `isCompactSummary: true`, starting "This session is being continued from a previous conversation". Today it is indexed as an ordinary user turn: it adds one to `exchange_count` per compaction, is embedded, and in a stitched session would repeat the real history next to it. When the hop can't resolve, it is the only remaining record of the earlier conversation. 0 of 199 local pointers dangled, but upstream issues report dangling pointers (claude-code #49996, #55818). A hop resolves when its target exists and hasn't been visited in this walk; a dangling pointer or a revisit doesn't resolve. The ingestion check merges a session's files while sync sees one, so a hop crossing files resolves in one view and not the other; all 199 local boundaries resolve in the same file.

**Deciding factor:** keep synthetic recaps out of counts and results without making a dangling pointer worse than today.

| | A: Skip it when its boundary's hop resolves, keep it otherwise | B: Always skip it | C: Always keep it |
|---|---|---|---|
| `exchange_count` in stitched sessions | counts real turns only | counts real turns only | one extra per compaction |
| Search results | no duplicate recap | no duplicate recap | recap competes with the real turns |
| Dangling pointer | summary stays as the only record, as today | earlier conversation disappears entirely, a regression | summary stays |
| Code change | a few lines in the walk | one flag check | none |
| How often it matters | 0 of 199 locally for the dangling branch | every compaction | every compaction |
| Peers | clog | engineering-notebook, ccglass, csift, ClawMem | episodic-memory (inferred) |

**Recommendation:** A, because B's failure is a regression this change would introduce on public installs, and avoiding it takes only a few lines.
**Pick B instead if** you consider dangling pointers rare enough to accept losing the summary for the simpler rule. **Pick C instead if** you want the recap searchable, since it does contain a dense description of the session.
**Reversibility:** easy (a parse change plus a repair run)
**Ratified:** Chose skipping the summary when its boundary's hop resolves and keeping it otherwise, over always skipping or always keeping it, to keep recaps out of counts and search without a dangling pointer erasing the only record, accepting a few extra lines in the walk, and that a hop crossing into another file can leave that session flagged as a gap, because the ingestion check merges a session's files while sync sees one (0 of 199 boundaries locally).

### D3: How do already-imported sessions get repaired?

**Situation:** unchanged transcripts are skipped by `import_log`. `ccrecall import --repair-gaps` force-reimports what `find_repairable_sessions` classifies as `stale_tail` or `ingestion_gap`. Once `_expected_uuids` uses the new walk, affected sessions classify as `ingestion_gap`, except that `classify_sessions` returns a cached "ok" for unchanged sources (`src/ccrecall/ingestion_status.py:190`). So the cache has to be invalidated once. `_source_fingerprint` is an existing per-session string compared for equality, and D10 settles how the cache gets invalidated. Live sessions correct themselves on their next sync, but the Stop-hook sync only sees its own session, so old sessions need something that scans all of them. ccrecall has no daemon, so the only automatic trigger is the detached `ccrecall import` that `ccrecall-setup` spawns at SessionStart (`src/ccrecall/hooks/memory_setup.py`). It runs outside the hook's hot path. D11 covers the gate on that spawn.

**Deciding factor:** as little manual work as possible, without loading the hook's hot path.

| | A: The background import always repairs gaps | B: The background import repairs once after upgrade | C: Manual `--repair-gaps` |
|---|---|---|---|
| Old sessions fixed | yes, unprompted | yes, unprompted | only when the user runs it |
| Future gaps (`stale_tail`, `ingestion_gap`) | fixed unprompted | still need the flag | still need the flag |
| Hook hot path | untouched | untouched | untouched |
| Steady-state cost per spawned import | about 4 s of detached work here, plus parsing changed sessions (D7) | none | none |
| First run after upgrade | one detached re-parse of every session's transcript | same | same, when the user runs it |
| Sessions that can't be repaired | retried on each background import (D8) | not retried | retried only by hand |
| New machinery | the spawned import turns repair on (with D6, D7) | the same, plus a run-once marker | none |

**Recommendation:** A, because it removes the manual step for this repair and all future ones, adds no new state, and stays off the hot path.
**Pick B instead if** a few seconds of detached work per spawned import isn't acceptable. **Pick C instead if** repairs should only happen when someone asks.
**Reversibility:** easy (no stored shape changes; turning repair off is a one-line change to the spawn)
**Ratified:** Chose having the SessionStart background import always repair gaps over a one-time repair or a manual flag, to remove the manual step for this and all future gaps while staying off the hook's hot path, accepting up to a few seconds of detached work per spawned import, that a repair racing a resumed session can unlink its newest turns until the next Stop or repair, and that an older install sharing the database undoes stitching for sessions it syncs until the newer install repairs them.

### D4: How do repaired sessions reach embeddings?

**Situation:** repair and background imports run with `embed=False`, and `embed_branch_chunks` returns immediately in that case (`src/ccrecall/embed_ops.py:270`), leaving `embedding_version` at the current version. `backfill embeddings` selects on that watermark (`build_selection`), so a repaired session's new exchanges would be searchable by keyword but never embedded. `f84165c7` is at version 3 today with one chunk. The same gap already exists for any `embed=False` import that adds messages to a branch. The clear happens on any link change, before the embed attempt, and that attempt stamps the watermark again only if every exchange ends up current. Behavior to pin: a links-changed sync with embedding enabled and nothing to embed keeps its stamped watermark, and an `embed=False` import clears it.

**Deciding factor:** repaired history must reach semantic search through the existing backfill, without a separate step.

| | A: Clear the watermark in `sync_branch` when links changed and embedding didn't run | B: Clear it only in the repair path | C: Leave it, document a forced re-embed |
|---|---|---|---|
| Repaired sessions reach backfill | yes | yes | no |
| Ordinary `embed=False` imports that add messages | also fixed | gap remains | gap remains |
| Code change | `diff_branch_messages` reports a change; one conditional | new reset in `import_repair.py` | docs only |
| Fit with invariant 3's clear-first protocol | same clear, same owner of link diffs | second place that writes the watermark | n/a |
| Precedent | `embed_branch_chunks` clears on change | `backfill tool-content` resets touched branches | none |

**Recommendation:** A, because the watermark means "every current exchange has a vector", and any link change without embedding makes that false, not only a repair.
**Pick B instead if** you want this PR to leave ordinary imports' behaviour exactly as it is.
**Pick C instead if** semantic search on old compacted sessions doesn't matter to you.
**Reversibility:** easy (a watermark clear only causes an extra backfill pass)
**Ratified:** Chose clearing the watermark in `sync_branch` whenever links changed, before the embed attempt, over a repair-only reset or documenting a forced re-embed, to keep the watermark truthful for every import, accepting that ordinary imports which add messages now also queue a backfill pass. (Originally "when links changed and embedding didn't run"; the ship-time challenge showed that copied gate missed an embedding failure before its own clear, and the user chose this wording.)

### D5: Should repaired sessions get embedded without a manual command?

**Situation:** after D3 and D4, repaired sessions have correct counts and keyword search, and are marked as needing embeddings, but only `ccrecall backfill embeddings` embeds them. That command is opt-in and never auto-spawned, because full-quality inference peaks at about 8.4 GB per call (`CLAUDE.md`, `embeddings.embed_batch`), and an earlier version of this path ran out of memory and killed WSL. The sync path already embeds automatically under a 4096-token cap with a peak of about 4 GB, and only for the session being synced (`MAX_WRITE_PATH_EMBEDS_PER_SYNC`). Supported installs include machines with far less free memory than that peak.

**Deciding factor:** add no new unprompted inference, as long as a manual backfill brings repaired history into semantic search.

| | A: Background backfill at the sync-path cap, small batch per session start | B: Background backfill at the full 8192 cap | C: Stay manual, with a SessionStart alert | D: Stay manual, no alert |
|---|---|---|---|---|
| Manual command needed | no | no | yes, prompted | yes, unprompted |
| Unprompted memory peak | about 4 GB, same as sync | about 8.4 GB, new | none new | none new |
| Vector quality | draft where truncated, upgraded only by a manual backfill | full | full | full |
| Time for a large backlog | spread over many session starts | fast, heavy | whenever the user runs it | whenever the user runs it |
| New machinery | a spawn, a cap flag, a batch size, plus kill recovery, an off switch and a selection change | a spawn, plus the same recovery and off switch | one alert reason | none |
| Effect on machines with little memory | same risk the sync path already carries | new OOM risk | none | none |

**Recommendation:** D, because D4 already queues repaired sessions for the existing command, and an unprompted backfill would embed all unembedded history on every install, with a memory-kill risk that needs its own recovery machinery.
**Pick A instead if** semantic search on old sessions must arrive with no command, and you accept that machinery and about 4 GB background spikes. **Pick B instead if** you only run this on machines with plenty of memory. **Pick C instead if** users should be prompted to run the backfill.
**Reversibility:** easy (no stored shape changes; a later release can still add an automatic run)
**Ratified:** Chose staying manual with no new alert over a background backfill or a prompted alert, to add no new unprompted inference while a manual backfill still fixes old records, accepting that until `ccrecall backfill embeddings` next runs, a repaired session's earlier history has keyword search only, and its old chunks, including the compact-summary recap, stay in semantic search with stale exchange indices.

### D6: How does the spawned import get past its own PID marker?

**Situation:** `_spawn_background` writes the child's PID into the marker (`src/ccrecall/hooks/memory_setup.py:46-78`). Today's spawned import runs without `--repair-gaps` and never touches that marker. Once D3 adds the flag, the child calls `try_acquire_pid_file`, finds a live PID (its own), and skips the whole run with exit 0, because the guard sits at the top of `_run` (`src/ccrecall/hooks/import_conversations.py:522`). That would skip the ordinary import loop as well, and nothing visible would report it. `sync_current` and `backfill embeddings` share the helper, and neither acquires twice in one process.

**Deciding factor:** make the spawned child run with the fewest new moving parts, keeping one running instance per job.

| | A: Spawner stops pre-creating the marker | B: A marker holding the caller's own PID counts as acquired | C: Child skips acquiring when told the parent holds it |
|---|---|---|---|
| Code change | a second mode in `_spawn_background` | one branch in `try_acquire_pid_file` | a flag or env var, plus cleanup logic in `run()` |
| One instance per job | holds; if two starts race, the loser exits after CLI startup | holds | holds only if no manual run is in flight when the flag is trusted |
| New interface or state | a spawner mode | none | a new flag |
| Existing tests | spawner tests change | `tests/test_backfill_embeddings.py:1168` pins the old contract and reverses; `tests/test_sync_hook.py:757, 775` and `tests/test_import_pipeline.py:1656` need a different live PID | unaffected |
| Peers | unknown | unknown | unknown |

**Recommendation:** B, because one branch fixes it with no new interface, and the existing cleanup already removes a marker the process owns. Behavior to pin: an import spawned by `ccrecall-setup` repairs a gapped session, and a marker held by another live process still refuses.
**Pick A instead if** you want the child to be the marker's only writer. **Pick C instead if** you have a reason not to change the shared helper's semantics.
**Reversibility:** easy (one branch in a helper)
**Ratified:** Chose counting the caller's own PID as acquired over a second spawner mode or a parent-holds flag, to fix the self-skip with no new interface, accepting a change to the shared helper's contract and to the tests that pin it.

### D7: How does the background path avoid re-parsing every transcript on each import?

**Situation:** only `summarize_ingestion` writes `ingestion_check_cache`; `classify_sessions` and `find_repairable_sessions` return "ok" without recording it (`src/ccrecall/ingestion_status.py:190, 216-221, 327-335`). So after invalidation nothing on the background path refills the cache. Classification took 3.8 s wall over the 6,959 sessions it covers with a warm cache. With a cold one, a 300-session sample extrapolates to about 69 CPU seconds across all 9,931 sessions with surviving transcripts (7.82 GB). That is an upper bound, because classification skips sessions with no `sessions` row.

**Deciding factor:** keep D3's always-on repair at about the cost it was chosen for, with no new state.

| | A: Scan only after a walk-version change | B: Repair path records ok verdicts | C: Keep it, restate the cost |
|---|---|---|---|
| Cost per spawned import | none | about 4 s wall here, plus parsing changed sessions | up to about 69 CPU s here |
| Future gaps fixed unprompted | no | yes | yes |
| New state | a run-once marker | none; existing table | none |
| Code change | marker logic | batched writes in the repair path | none |
| Peers | unknown | episodic-memory 1.6.0 fixed the same kind of repeated work | the same peer treated this as a bug |

**Recommendation:** B, because it restores the cost using the table that already exists and keeps future gaps automatic. The writes live in `find_repairable_sessions` and commit in batches so progress survives a kill, and all of them finish before the first repair candidate, because `src/ccrecall/hooks/import_repair.py:6-16` relies on no open transaction at each candidate's SAVEPOINT. Behavior to pin: a second import over unchanged sessions parses none of them, and a repair candidate still commits independently.
**Pick A instead if** future gaps don't need automatic repair. **Pick C instead if** a minute of niced CPU per spawned import doesn't matter.
**Reversibility:** easy (no stored shape changes)
**Ratified:** Chose recording ok verdicts on the repair path over a run-once scan or restating the cost, to keep repair automatic at about 4 s per spawned import with no new state, accepting writes in a path whose docstring says it is pure-SELECT.

### D8: What happens to sessions repair can't fix?

**Situation:** a session still non-ok after a force-reimport counts as `sessions_unrepairable` (`src/ccrecall/hooks/import_repair.py:253-254`) and nothing records it, so each background import tries it again. That costs about 3 parses (about 26 ms each for a 3 MB transcript, the 99th percentile) plus a force-reimport per session. The set is 0 under the current walk here, and may be a few under the new one, including any session with a hop that crosses files. A negative cache needs a verdict column, which means a migration and the schema-ahead alert for older installs, plus rules for when a verdict is written, which outcomes count, who reads it, and what expires it.

**Deciding factor:** whether a persisted record of unrepairable sessions earns its rules, given what it saves.

| | A: Retry every import | B: Negative cache with those rules | C: Negative cache with expiry instead of a flag |
|---|---|---|---|
| Cost per unrepairable session | about 3 parses plus a reimport, each import | none until the source or version changes | one attempt per N days |
| Migration and older-install alert | none | both | both |
| Rules to build | none | write after the attempt, two categories only, read in one place, version rename plus a spawn flag, migration placement | the same, with a time check in place of the flag |
| `--repair-gaps` and `status` | unchanged | unchanged, using those rules | `--repair-gaps` skips unexpired sessions |
| A later importer fix | reaches them on the next run | only if the version bump is remembered | within N days |
| If a machine has hundreds | tens of seconds of niced CPU per import, plus reimports | cheap | cheap |
| Peers | rclone, restic, Syncthing, claude-mem | Recoll, basic-memory (unconfirmed) | episodic-memory |

**Recommendation:** A, because peers with a skip-list are avoiding costly retries, and this retry is cheap (design/research/2026-11-01-repair-negative-cache/prior-art.md).
**Pick B instead if** a repair log shows a large unrepairable count. **Pick C instead if** you want that record to heal itself with no flag.
**Reversibility:** easy (a cache can be added later with no data lost)
**Ratified:** Chose retrying on every background import over a negative cache, to avoid a migration and rules for a retry this cheap, accepting the repeat work; a large unrepairable count in the repair log is the trigger to revisit.

### D9: How does anyone learn repair is failing?

**Situation:** the spawned import runs with stdout and stderr on DEVNULL, and the repair summary is a `print` (`src/ccrecall/hooks/import_conversations.py:613-621`), so it disappears. `embedding-status.json` holds a single `{reason, since}` record that backfill's success path clears unconditionally (`src/ccrecall/hooks/backfill_embeddings.py:327`), its alert text is fixed to embedding failures, and nothing stores a run count.

**Deciding factor:** whether a repair that keeps failing justifies new state at session start, or the log and `status` are enough.

| | A: Log only | B: Dedicated `repair-status.json` with its own alert | C: Embedding sidecar becomes a `{reason: since}` map |
|---|---|---|---|
| You learn of persistent failure | from the log or `status` | at session start | at session start |
| Clobbering between subsystems | none | none | none, per reason |
| Alert text accurate | n/a | yes | needs per-reason prose |
| "Persistent" defined | not needed | N consecutive failed runs, reset on a clean run | needs a counter somewhere |
| New state | none | one small file, one alert key | a format change to an existing file |
| Older installs reading it | unaffected | unaffected | see a changed format |
| Peers | episodic-memory logs a per-run skip count; rclone and restic report per run | unknown | unknown |

**Recommendation:** B, because repair runs unattended and a failure nobody sees stays silent indefinitely.
**Pick A instead if** you're fine checking the log when search looks wrong, which removes this code and matches what peers do. **Pick C instead if** you strongly prefer one status file.
**Reversibility:** easy (an alert can be added later)
**Ratified:** Chose logging the repair summary with no SessionStart alert over a dedicated sidecar or a reshaped embedding sidecar, to add no new state, accepting that a repair failing on every run shows only in `ccrecall-import.log` and `status --check-ingestion`.

### D10: How does the ok cache get invalidated when the walk changes?

**Situation:** `classify_sessions` returns a cached "ok" when `_source_fingerprint` and the DB-coverage fingerprint match, so without invalidation no affected session becomes a repair candidate. `_source_fingerprint` is a per-session string compared for equality (`src/ccrecall/ingestion_status.py:61-75`), so a constant in it invalidates every row. A schema delta could instead delete the rows once.

**Deciding factor:** invalidate once now, and again after any later walk change, with the least surface.

| | A: Walk-version token in `_source_fingerprint` | B: A `user_version` delta that clears the table |
|---|---|---|
| Later walk changes | bump one constant | another migration each time |
| Older installs sharing the database | no alert | schema-ahead alert until they upgrade |
| Schema | unchanged | version bump, no shape change |
| Code change | one constant in `src/ccrecall/ingestion_status.py` | one delta in `src/ccrecall/db_base.py` |
| Older install re-checks a session | its row carries the old fingerprint, so the newer install re-checks it once | its row matches, so the newer install trusts it again |
| Peers | claude-code-log keys its cache on a content version, with a manual salt for changes that keep the shape | basic-memory clears checksums in a migration, on the rows it merged only; obliscence uses `user_version` for embeddings only |

**Recommendation:** A, because it also covers later walk changes, survives an older install writing to the cache, and D8 removed the only other reason to bump the schema (design/research/2026-11-01-parser-change-invalidation/prior-art.md).
**Pick B instead if** you want older installs told that the database moved on.
**Reversibility:** easy (no stored shape changes)
**Ratified:** Chose a walk-version token in `_source_fingerprint` over a migration that clears the table, to cover this and later walk changes and survive an older install writing to the cache, accepting that the constant is bumped by hand and older installs get no warning.

### D11: Should the gate on the background import change?

**Situation:** the spawn runs only when `_needs_reimport` is true, meaning any `import_log` row has a NULL `file_hash` (`src/ccrecall/hooks/memory_setup.py:81-91, 156`). The Stop-hook sync writes NULL hashes in normal use (`src/ccrecall/session_ops.py:7-9`), so the gate opens at the next session start after any turn: 232 rows are NULL here, and at least 282 imports completed over Oct 30-31.

**Deciding factor:** reach repair without adding work to session starts that have nothing to import.

| | A: Keep the gate | B: Spawn the import at every session start |
|---|---|---|
| First repair after upgrade | at most one session start later | the first session start |
| Session start with no turn since the last import | no spawn | one more detached process, about 4 s of classification |
| Code change | none | remove the condition |
| Peers | unknown | episodic-memory indexes automatically at session start |

**Recommendation:** A, because the gate already opens after any turn, so B only gains one session start.
**Pick B instead if** repair must not depend on a Stop hook having run.
**Reversibility:** easy (one condition)
**Ratified:** Chose keeping the `_needs_reimport` gate over spawning at every session start, to add no work to session starts with nothing to import, accepting that repair can wait one session start and depends on a Stop hook having run.

## Assumed

- All 199 local `compact_boundary` entries have `parentUuid: null` and a `logicalParentUuid` that resolves in the same file; no sidechain roots appear in top-level transcripts. Evidence: read-only probe over 25,692 transcripts in `~/.claude/projects`, 2026-10-31.
- 72 of the 199 boundaries carry `compactMetadata.preservedSegment`, and none of the unreached turns after the hop are attributable to it: 52 are on forks, 67 are `/clear` or `/compact` lines, 8 are disconnected, and the remainder are summaries and other commands. Evidence: same probe.
- Off-branch messages get no `messages` row until they are on the active set, so repair recovers rows as well as links. Evidence: `insert_new_messages(..., valid_branch_uuids, ...)` in `src/ccrecall/message_ops.py:96`; `f84165c7` has 83 rows against 248 transcript entries.
- Exchange assembly orders by timestamp, so stitched chains read chronologically with no ordering change. Evidence: `src/ccrecall/branch_ops.py` `branch_msgs.sort`, `fetch_branch_messages` `ORDER BY m.timestamp` in `src/ccrecall/db_vec.py:102`.
- `find_all_branches` keeps its single-element return, so `hooks/backfill_tool_content.py:488`, `tool_content_status.py:112` and invariant 4 (`UNIQUE(session_id)`) are unaffected. Evidence: those call sites use `branches[0]` or a truthiness check.
- Force-reimport is idempotent and each repair candidate commits independently. Evidence: `src/ccrecall/hooks/import_repair.py` module docstring.
- `backfill embeddings` replaces a repaired session's chunks whose exchange indices shifted: it re-embeds an index whose content hash changed and prunes indices that no longer exist. Evidence: `_diff_exchanges` in `src/ccrecall/embed_ops.py:127-155`.
- `status --check-ingestion` surfaces `ingestion_gap` counts, so affected sessions are visible before repair. Evidence: `summarize_ingestion` in `src/ccrecall/ingestion_status.py:240`, `cmd_status` in `src/ccrecall/cli/commands.py`.

## Build

- [x] Implementation and tests committed
- [x] Docs: the `src/ccrecall/hooks/import_repair.py` module docstring, CLAUDE.md's architecture notes on the walk and repair, the `--repair-gaps` help text, and any other user-facing text that presents that flag as the only way to repair (README's hook table and flow diagram)
- [x] Ship-time challenge

**Calls made during the build:**

- A hop needs `subtype: compact_boundary` and an empty `parentUuid`: every measured boundary has both, and a `logicalParentUuid` anywhere else has no measured meaning.
- `logicalParentUuid` joins `TranscriptEntry`, so a non-string value is rejected at the parse boundary instead of raising `TypeError` in the walk's dict lookup.
- A session ending on its own summary after a resolved hop reports the boundary as `leaf_uuid`: `leaf_uuid` is diagnostic only, and this keeps it inside `uuids`.
- `classify_sessions` yields a `SessionVerdict` carrying the row to cache when parsing just verified a session ok, and `summarize_ingestion` uses it too: re-computing the coverage fingerprint for every ok session would have roughly doubled D7's 4 s, and the verdict now records the fingerprints it was checked against.
- D9's log line is WARNING when anything failed or was unrepairable and INFO otherwise, with counts in the message. It has no log-capture test (repo testing convention); it was observed on a real run against a database copy.
- The `project_id` test fixture moved to `tests/conftest.py`, since this change would have added a third copy.
- Ship-time challenge: `--repair-gaps` runs at `BACKFILL_NICE_LEVEL`, because D7 and D8 priced this work as niced and nothing set it (the user chose this). Manual repair runs are niced too.
- Measured against a copy of the real database, in place of D3's and D7's "about 4 s": the first run took 150 s wall and 114 s CPU, repairing 109 sessions and recovering 10,716 messages with a 164 MB peak; the next took 11 s wall with 0 candidates, including the ordinary import loop.
- Ship-time challenge: a transcript that can't be read counts as `missing_source` and logs, so one bad file can't stop repair for every other session; database errors raise outside that containment and still stop the run. `run()` always decides marker cleanup by comparing the stored PID, since the spawned import now always passes `--repair-gaps` and would otherwise leak its marker on failure.

## Addendum
