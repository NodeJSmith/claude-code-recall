# Research Brief: Project path identity (issue #221)

**Date:** 2026-10-20
**Issue:** #221: `upsert_project` overwrites an accurate project path with a stale or lossy one, causing a UNIQUE crash and flip-flopping.
**Evidence:** code and git-history read, a read-only probe of the real `~/.claude/projects` and `~/.ccrecall/conversations.db` (run on a copy), and peer source and tracker reading.

## Bottom line

The bug is real and it was severe. From 2026-10-17 12:58 to 2026-10-20 07:12, `ccrecall import` aborted about 14 times an hour: 963 "Import process failed" lines, 925 of them `UNIQUE constraint failed: projects.path` at `project_ops.py:70`. Every project that sorted after the failing one was never imported. The cause was the personal-apps repo moving from `~/personal-apps` to `~/source/personal-apps` while an orphan row still held the old path. Deleting that orphan by hand stopped the crashes. The underlying mechanism is unchanged, and the probe reproduces the crash on a DB copy (`repro2.py`).

The peers' consensus points at the root fix. **None of them treats project path as a unique, mutable identity.** They key on the slug, or on a derived name, and treat cwd or path as metadata. Our schema makes `projects.path` UNIQUE *and* rewrites it, and both failure modes come from that combination.

## How the code behaves today

- `upsert_project` (`project_ops.py:30-86`) finds the row by `key`, which is the normalized slug. Whenever the derived path differs from the stored one, it runs an unconditional `UPDATE path, name` (`:67-73`). The `used_lossy_fallback` flag is computed but never guards that write.
- The derived path comes from `_probe_project_dir` (`:108-132`): the first cwd of `files[0]`. `files[0]` is the alphabetically first UUID filename, so the pick is effectively random in time. The probe never looks at a second file.
- Without a cwd, the path falls back to `parse_project_key`, which turns every `-` into `/`.
- The insert branch's `ON CONFLICT(path) DO UPDATE SET key = excluded.key` (`:75-82`) silently takes over the key of whatever row already holds that path. It came in unchanged from the upstream claude-memory plugin (#212/#255), and no commit or issue explains why.
- Readers:
  - `projects.name` drives the `--project` filter, the display label, and the import exclusion check (`import_conversations.py:313-321`).
  - `projects.key` drives SessionStart session selection (`session_selection.py:259`).
  - `projects.path` is only displayed (`recent_chats.py:41`). **Its UNIQUE constraint is its only behavioral role, and that role is the source of the crash.**
- FK enforcement is on (`db_base.py:40`). `sessions.project_id` is the only reference to `projects`, so deleting a project row that still has sessions fails instead of silently orphaning them.
- Transactions:
  - `import_project` holds one explicit `BEGIN` per project (`:297-304`). Anything inside `upsert_project` must not commit, roll back, or raise for a conflict. An exception there escapes the per-file SAVEPOINT handling and aborts the whole run.
  - On the sync path, `get_connection` handles commit and rollback.
- Existing test `test_upsert_project_updates_path_when_better_data` (`tests/test_project_ops.py:46-65`) is vacuous: its stored path and its "better" path are the same string.

## Real-data findings

| Measure | Value |
|---|---|
| Project dirs / with transcripts / empty | 1800 / 1747 / 53 |
| Dirs with >1 cwd after worktree normalization | 534. Almost all are `cd` into subdirectories. 528 include a cwd now deleted from disk. |
| Real moves or renames | ~1 (personal-apps) |
| Dirs where the lossy slug decode differs from the real cwd | **1353 / 1744 (78%)** |
| Dirs taking the lossy path in the real code | 57 (53 empty, 3 with no cwd anywhere, 1 where `files[0]` lacks a cwd but another file has one) |
| `projects` rows / zero-session orphans | 1380 / 78. **68 of the 78 orphans have a lossy-decoded path.** |
| Rows whose `key` ≠ `get_project_key(path)` | 15 (underscore slugs, bare-hash keys, row 1080 left stale by the workaround) |

Three live problems the current code still has:

1. **Lossy overwrite (failure 2 in the issue), live today.**
   - The worktree dir `-home-jessica-source-otf-api--claude-worktrees-new-issues` normalizes to the key `-home-jessica-source-otf-api` and contains a file with no cwd.
   - Each import rewrites the accurate `/home/jessica/source/otf-api` to `/home/jessica/source/otf/api`.
   - Several dirs share one normalized key, so dirs compete to write the same row. That competition is the flip-flop.
2. **Silent re-key, one import away.**
   - The dir `-home-jessica-source-personal-telegram-bot` has no row.
   - Its insert would collide on path with the existing row `-home-jessica-source-personal-telegram_bot` and take over that row's key. SessionStart lookups for the underscore-keyed dir would then find nothing.
3. **A stale path left by the workaround.**
   - Row 1080 (personal-apps) still shows `/home/jessica/personal-apps`.
   - A newest-file probe would correct it with no conflict now.

The 68 lossy-path orphans are where future collisions come from. Each one is a junk row holding a made-up path that a real project may one day decode or move into.

**Probe strategies, simulated on the copy:**
- Current, newest-by-mtime, newest-file-with-cwd, and newest-with-existing-cwd all produce 0 collisions on today's DB.
- Only the newest-file variants fix personal-apps.
- The "most common cwd by message count" strategy would rewrite 29 rows to subdirectories. Reject it.
- "Path exists on disk" is a poor signal: 1277 of the probed paths are deleted worktrees.

## Peer prior art

| Peer | Identity | Path or cwd role | Conflict policy |
|---|---|---|---|
| obra/episodic-memory | slug dir name, verbatim (`sync.ts:312`) | cwd stored per exchange as metadata | none needed, project column not unique |
| ccusage | slug dir name (`paths.rs:269`) | not read | none |
| basic-memory (no transcripts) | UNIQUE name / uuid | `path` **non-unique** index | n/a |
| claude-mem | derived name (git toplevel → marker → cwd basename; opt-in git remote) | live cwd; first `cwd` line of the first sibling file for ingest | aliases plus an explicit `project merge`; worktrees kept as `parent/worktree` |

- **Nobody decodes slugs.** claude-mem only encodes cwd into a slug, and falls back to the slug's last segment.
- The one peer that re-derives identity continuously (claude-mem) has a recurring worktree and orphan bug history (#2864, #1500), and its one-shot remap migration orphaned rows.
- episodic-memory #189 shows stored cwds going stale when a worktree is deleted.
- No peer has a deterministic multi-cwd rule. claude-mem's probe is as naive as ours.

## Options

### A. Do less: guard and catch (in `project_ops.py` only)
- Skip the path and name UPDATE when this call's path is lossy and a row exists.
- Before UPDATE, check for another row holding the new path. If one exists, log a warning and keep the stored path.
- Probe the newest file by mtime, walking back to older files until one has a cwd.
- Leave the insert branch's re-key alone.
- *Fixes:* both crash paths and the otf-api flip-flop.
- *Leaves:* moved repos stuck on their old path whenever a stale orphan exists, the telegram_bot re-key hazard, and the 68 junk orphans.

### B. The issue's proposal: A plus delete zero-session orphans
- Same as A, except a conflicting row with zero sessions is deleted and the update proceeds.
- *Fixes:* the personal-apps move case automatically.
- *Leaves:* the re-key hazard (unless the insert branch is fixed too) and the junk orphans that nobody conflicts with.
- *Risk:* the import deletes rows. This is safe under FK enforcement, but it is a new kind of write on the import path.

### C. Fix the model: path is metadata, not identity (schema change)
- Drop `UNIQUE` on `projects.path`, keeping a plain index. This needs a `projects` table rebuild migration. `projects` has no vec trigger, unlike `branches`.
- The `ON CONFLICT(path)` re-key disappears. Inserts conflict only on `key`.
- Keep the lossy guard and the newest-file probe, so the stored path tracks the newest accurate cwd. No collision is possible, so a moved repo just updates.
- Optionally stop inserting a lossy-path row for empty dirs, or mark lossy rows with a `path_is_lossy` flag, so lossy rows never claim a real path.
- *Fixes:* every failure mode in the issue plus the re-key hazard. It matches all four peers.
- *Cost:* a migration (the conversations DB is a public contract, so it must be lossless, which a rebuild that copies rows is). Readers of `projects.path` must tolerate duplicates: only the display uses it, and `--project` filters by name.

## Concerns

- **The name collision is a separate, unchanged risk.** `--project` and exclusion filter on `name` (the basename), which is already non-unique. C does not make it worse.
- **One abort stops the whole import.** No path-conflict handling should raise, regardless of option. An import-level regression test should prove `ccrecall import` survives a planted collision. The model to follow is `tests/test_import_pipeline.py:829-865`.
- **Worktree-normalized skip (issue item 4).** The lossy guard subsumes it: the only bad worktree write seen in real data is lossy. Drop it.
- **Log levels** per `logging.md`:
  - `warning` for a skipped conflict where the other row has sessions.
  - `info` for an orphan delete (if B).
  - `debug` for a routine skipped lossy overwrite, which recurs on every import.
  - Use `extra=` fields that name both keys and the path. The current traceback never names the project, which is why the culprit was hard to find.

## Recommendation

**C**, with the lossy guard and the newest-file-with-cwd probe. The UNIQUE path constraint does no behavioral work: nothing looks a project up by path. It is the sole cause of both crash paths and of the re-key hazard. Every peer treats path as non-unique metadata. Dropping it removes the conflict class rather than adding delete-or-warn handling around it. If a migration is unwanted right now, **A** is the safe minimal fix. B adds row deletion to buy the one moved-repo case, and C handles that case without deleting anything.

## Open questions

- [x] **Schema change acceptable now?** **Answer: yes. Option C ships in #221.** Drop `UNIQUE(path)` with a `projects` rebuild migration, alongside the lossy guard and the newest-file-with-cwd probe.
- [x] **Lossy rows for empty or cwd-less dirs:** **Answer (superseded by ledger 017 D2: no marker, since dropping UNIQUE removes the need): insert them with a lossy flag.** The row exists so sessions can attach to it. An accurate cwd always replaces a flagged path, and a lossy call never overwrites an unflagged path.
- [x] **Clean up existing junk:** **Answer: no data deletion.** Normal imports heal it: the newest-file probe fixes personal-apps, and orphans are harmless once path is not unique. The 15 mismatched keys stay as they are.
- [x] **Insert-branch re-key (if not C):** **Answer: moot.** Choosing C removes `ON CONFLICT(path)`.

## Artifacts

- Probe scripts and outputs: `/tmp/claude-1000/-home-jessica-source-claude-code-recall/6546f201-0ffe-41c5-a6f1-dcbc48844d5e/scratchpad/probe-Wg56qp/` (`repro2.py` reproduces the crash)
- Peer clones: `/tmp/claude-1000/-home-jessica-source-claude-code-recall/6546f201-0ffe-41c5-a6f1-dcbc48844d5e/scratchpad/peers-a1/`
