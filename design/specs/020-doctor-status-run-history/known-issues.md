# Known Issues

Real issues found while building this feature and intentionally left unfixed.

## KI-001: Losing `runs.json` raises a false backlog alarm

Status: open
Recorded: 2026-11-03 (234-235-237)
Source: ship-challenge
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/run_history.py
- src/ccrecall/config.py

Issue:
`<db>.runs.json` is the only record of the last successful backfill. `read_json_object` treats every `OSError` like unparseable content, so after a transient read error the next `write_record` rewrites a good file with only the current kind. `atomic_write_json` doesn't fsync before the rename, so a power cut on a filesystem without ext4's rename heuristic can leave it empty. Either way backfill `last_ok` is lost and `doctor` reports an overdue backlog (exit 1) until the next unscoped backfill.

Why deferred:
Rare, the lost data is diagnostic metadata, and the result is a loud false alarm that the next backfill clears, not silent loss.

Recommended follow-up:
If it shows up in practice: have `write_record`/`write_unfixable` skip the write when the read failed with an `OSError` other than `FileNotFoundError`, and/or fsync the temp file in `atomic_write_json`.

Acceptance criteria:
- A transient read error during a run's recording leaves the existing `runs.json` intact.

## KI-002: `doctor` run as root can leave root-owned WAL files

Status: open
Recorded: 2026-11-03 (234-235-237)
Source: ship-challenge
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/findings.py

Issue:
`diagnose` opens the database with `mode=ro`, which recreates missing `-wal`/`-shm` files. Run as root against a user's database (`sudo -E ccrecall doctor`, or `--db`), those files are root-owned and the user's next sync may fail to open the database.

Why deferred:
Needs an unusual invocation; plain `sudo` usually resets HOME and points at a different database.

Recommended follow-up:
If users reach for sudo after a `not-writable` finding: warn on stderr when `os.geteuid() == 0` and the database file's owner is another user.

Acceptance criteria:
- `sudo -E ccrecall doctor` against a user's database warns before opening it.

## KI-003: A branch locked on every backfill keeps the backlog alarm up

Status: open
Recorded: 2026-11-03 (234-235-237)
Source: ship-challenge
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/hooks/backfill_embeddings.py
- src/ccrecall/findings.py

Issue:
A backfill with any lock timeout records `partial`, and only `ok` advances `last_ok`. If one writer holds a branch locked across every backfill run, `last_ok` never advances and `embedding-backlog` stays needs-action ("finished with failures") while backfills run nightly.

Why deferred:
D8 ratified this trade-off: letting lock-only failures advance `last_ok` would let a repeated lock hide a real stall.

Recommended follow-up:
If the alarm appears in practice from a long-lived lock holder, revisit whether a `partial` backfill with reason `database_busy` may advance `last_ok`.

Acceptance criteria:
- A nightly backfill that embeds everything but one perpetually locked branch doesn't keep `doctor` at needs-attention, without hiding a backfill that makes no progress.
