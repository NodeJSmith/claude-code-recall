# Known Issues

Real issues found while building this feature and intentionally left unfixed.

## KI-001: A non-exec `ccrecall` wrapper makes the background import skip itself

Status: open
Recorded: 2026-11-01 (168)
Source: ship-challenge
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/config.py
- src/ccrecall/hooks/memory_setup.py

Issue:
`try_acquire_pid_file` decides the marker is its own from `existing_pid == os.getpid()`. `_spawn_background` writes the PID of whatever `ccrecall` resolves to on PATH. If that is a wrapper that forks instead of execs, the marker holds the wrapper's PID, the child sees a live foreign holder, and it exits 0 with one INFO line.

Why deferred:
Every documented install path (`uv tool`, `pipx`, `pip`) runs the console script in the same PID, and no such launcher has been observed. Nothing is lost when it happens: Stop-hook sync still indexes live sessions and a manual `ccrecall import` still works.

Recommended follow-up:
If a real wrapper turns up, have the spawner tell the child it owns the marker, for example through an env var the child checks against `os.getpid()`.

Acceptance criteria:
- A background import spawned through a forking wrapper repairs a gapped session.

## KI-002: A live session whose chain crosses transcript files can flip its links

Status: open
Recorded: 2026-11-01 (168)
Source: ship-challenge
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/hooks/import_conversations.py
- src/ccrecall/ingestion_status.py

Issue:
Ordinary import and Stop-hook sync compute links from one file, while the ingestion check and group repair merge a session's files. If a chain crosses files, a per-file sync drops links that repair added and the next spawn repairs them again. Each flip clears the embedding watermark and resets the branch summary. It needs a file still changing, because repair writes real hashes and unchanged files are then skipped, so it stops when the session ends.

Why deferred:
The ledger measured 0 of 199 boundaries crossing files, and no data is lost. Routing multi-file sessions through `import_session_group` during ordinary import would add a second path and still wouldn't cover the Stop hook.

Recommended follow-up:
Revisit if `ccrecall-import.log` shows the same multi-file sessions repaired run after run.

Acceptance criteria:
- A session whose chain crosses files keeps a stable link set across repeated syncs and repairs.

## KI-003: A lock timeout on the ok-verdict writes skips that run's repair

Status: open
Recorded: 2026-11-01 (168)
Source: ship-challenge
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/ingestion_status.py

Issue:
`find_repairable_sessions` writes and commits every 100 verified sessions. If another writer holds the database past the busy timeout during one of those writes, the `OperationalError` aborts the run and that run repairs nothing. Committed batches survive.

Why deferred:
It heals on the next session start and loses nothing, and the run's other writes already abort on the same error, so guarding this one wouldn't make the run lock-safe.

Recommended follow-up:
If `ccrecall-import.log` shows this write failing in practice, treat a lock error on the ok-verdict writes as non-fatal.

Acceptance criteria:
- A lock timeout during ok-verdict writes no longer prevents that run's repair.

## KI-004: A boundary that is the last entry, with a dangling pointer, doesn't index its summary

Status: open
Recorded: 2026-11-01 (168)
Source: ship-challenge
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/parsing.py

Issue:
A boundary is timestamped just after its summary and the summary isn't its ancestor. If the boundary is the transcript's last entry and its `logicalParentUuid` doesn't resolve, the chain is just the boundary, so the summary isn't indexed until a later entry descends from it.

Why deferred:
It needs two conditions never seen locally: 0 dangling pointers across 121 compacted sessions, and a boundary as the last entry. Sessions that end at a compaction are common (20 of those 121) but carry trailing entries that descend from the summary, so the walk reaches it; `test_session_left_idle_after_a_compaction` pins that shape. The transcript stays intact.

Recommended follow-up:
If dangling pointers start appearing, as upstream reports suggest they can, add the summary in `active_chain` when an unresolved boundary is the leaf, and bump `ACTIVE_CHAIN_VERSION`.

Acceptance criteria:
- A transcript ending on a boundary with a dangling pointer indexes that boundary's summary.
