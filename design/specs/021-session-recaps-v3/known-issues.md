# Known Issues

Real issues found while building this feature and intentionally left unfixed.

## KI-001: A write-lock timeout after a paid call aborts the recap run

Status: open
Recorded: 2026-11-15 (agent-ran-summaries)
Source: ship-challenge (Finding 8)
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/hooks/backfill_recaps.py
- src/ccrecall/recaps.py

Issue:
`attempt` writes the recap through `commit_single_write`, which re-raises any `sqlite3.Error`. A write lock held past `busy_timeout` (5 s) at that moment, such as a Stop-hook sync committing, propagates out of `generate_recaps`: the generated text is lost and the remaining candidates wait for the next run. `record_run` records the run `failed` with `database_busy`, which `doctor` files under working.

Why deferred:
Rare (sync must hold the lock past 5 s at the instant of the write), loud, and the next run retries the session, since no cooldown row is written. The cost is one lost call.

Recommended follow-up:
If it shows up in `ccrecall-backfill-recaps.log`, treat a locked write (`db_base.is_database_locked`) as a per-session failure with no cooldown that counts toward the backstop, like the embedding backfill's lock-timeout handling.

Acceptance criteria:
- A locked write during a recap run costs that session alone and the run continues.

## KI-002: An exception from the import itself fails the recap run before selection

Status: open
Recorded: 2026-11-15 (agent-ran-summaries)
Source: ship-challenge (Finding 9)
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/hooks/backfill_recaps.py

Issue:
Per-project import failures don't stop the recap run, but an exception from `import_conversations.import_locked` itself (a lock timeout opening its connection, schema-ahead, an `OSError`) propagates through `record_run` and fails the run before selection, although selection could proceed on rows already imported.

Why deferred:
A fault that stops the import's connection usually stops the recap run's own connection too, the next tick retries, and the project leaves these errors to the CLI `launcher` boundary.

Recommended follow-up:
If imports fail often enough to cost recap runs, catch `sqlite3.Error`/`OSError` from `import_first`, log with `logger.exception`, and proceed to selection.

Acceptance criteria:
- A raising import is logged and the recap run still selects from imported rows.

## KI-003: A held import lock is assumed to be a full catch-up

Status: open
Recorded: 2026-11-15 (agent-ran-summaries)
Source: ship-challenge (Finding 10)
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/hooks/backfill_recaps.py

Issue:
When another process holds the import lock, the recap run proceeds without importing (D9). The holder may be a `--project`-scoped import or one partway through, so selection can run on partial data.

Why deferred:
`as_of` is the last imported message, so a recap from a stale prefix later reads as not current and is regenerated. The cost is at most one wasted call per affected session; `current` is never wrong.

Recommended follow-up:
If scoped imports are routinely running when the timer fires, read the holder's scope with `job_lock.probe` and skip the recap run when it is scoped.

Acceptance criteria:
- A recap run overlapping a scoped import makes no calls.

## KI-004: After a rewind with no new message, a recap of the discarded tail reads as current

Status: open
Recorded: 2026-11-15 (agent-ran-summaries)
Source: ship-challenge (Finding 11)
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/recaps.py

Issue:
Currency is `as_of >= last message time`, keyed per session. If a rewind shortens the active chain and no newer message follows, the last message time falls below the recap's `as_of`, and the recap of the abandoned tail reports `current: true` and is not selected again until the session continues. Whether Claude Code records a rewind before the next message is unverified.

Why deferred:
The trigger is rare and unverified, and the consequence is one misleading line that clears on the session's next message.

Recommended follow-up:
If a probe shows Claude Code writes the rewind into the transcript before the next message, store the covered message's uuid (`as_of_uuid`) and require it to equal the branch's current last message in `pick_recap`.

Acceptance criteria:
- After a rewind that shortens a branch, its recap reports `current: false`.

## KI-005: `recaps.session_id` foreign key has no delete story

Status: open
Recorded: 2026-11-15 (agent-ran-summaries)
Source: ship-challenge (Finding 12)
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/schema.py

Issue:
With `foreign_keys = ON`, a future session-delete path that doesn't delete `recaps` rows first raises `IntegrityError`. The only delete path today (empty-session cleanup) never reaches a recapped session.

Why deferred:
No other table uses `ON DELETE`; every child is deleted explicitly, and a forgotten `recaps` delete fails loudly rather than corrupting anything.

Recommended follow-up:
Any future session-delete path (such as a `forget-project` command) deletes `recaps` rows explicitly alongside messages, branches and chunks.

Acceptance criteria:
- A session-delete path's test covers a session with a `recaps` row.

## KI-006: Exact `claude-agent-sdk` pin blocks co-install with other SDK versions

Status: open
Recorded: 2026-11-15 (agent-ran-summaries)
Source: ship-challenge (Finding 13)
Reason not fixed now: accepted-limitation
Affected files:
- pyproject.toml

Issue:
`claude-agent-sdk==0.2.182` makes the resolver fail a `ccrecall` install into an environment holding any other SDK version, and every SDK release needs a manual bump.

Why deferred:
The pin also fixes the bundled `claude` CLI whose `--no-session-persistence` behavior D10 relies on, and ccrecall is normally installed as an isolated tool or plugin. A pyproject comment records why.

Recommended follow-up:
If install-conflict reports appear, move to a bounded range (`>=0.2.182,<0.3`) and rely on the classification and flag tests.

Acceptance criteria:
- ccrecall co-installs with another SDK release in the supported range.

## KI-007: `show-recaps ""` matches every stored session

Status: open
Recorded: 2026-11-15 (agent-ran-summaries)
Source: ship-challenge (Finding 14)
Reason not fixed now: accepted-limitation
Affected files:
- src/ccrecall/recaps.py

Issue:
`read_recaps` builds `escape_like(session_id) + "%"`, so an empty id becomes `%` and returns a row for every stored session.

Why deferred:
Only a caller bug (an empty interpolated variable) triggers it, and the result is noisy output, not harm.

Recommended follow-up:
Treat an empty or whitespace-only id as an unknown id if `show-recaps`'s degenerate-input contract needs pinning.

Acceptance criteria:
- `show-recaps ""` returns one row with no recap.
