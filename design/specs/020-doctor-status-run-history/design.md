# Design: Doctor, a terse status, and a record of background runs

**Date:** 2026-11-02
**Status:** ratified
**Mode:** sketch
**Issues:** #234, #235, #237
**Prior art:** design/research/2026-11-02-doctor-and-run-history/prior-art.md

## Summary

A user can't tell from ccrecall whether it's healthy, what's wrong, or what to do. An audit of 11 plain questions got 0 passes (`design/audits/2026-11-01-state-visibility/audit.md`). Background runs leave only logs, and `ccrecall status` prints internal counts, has no verdict, and exits 0 while real faults exist.

This change adds three things that depend on each other:

1. **A record of background runs (#234).** Import, repair, sync and embedding backfill each write a finish record to a small per-database file that keeps the newest run and the last success of each kind. A run that never finished is read from the PID marker it left behind.
2. **`ccrecall doctor` (#235).** It reads the database without changing it and reports findings grouped by what the user should do, with exact commands.
3. **A terse `ccrecall status` (#237).** A verdict line and a few plain numbers, rendered from the same findings as `doctor`.

The already-decided split: `doctor` is the diagnosis, `status` is the glance, and both render one shared set of findings so they can't disagree.

**Out of scope:** retiring `search --status` and `backfill embeddings --status` (#238), a default scheduled backfill and the schedule marker's future (#236), renaming terms in other commands (#239), the README operations rewrite (#240, this change adds only a short entry for the two commands), raw traceback messages elsewhere (#223), and SessionStart alerts, which keep their own hot-path checks (invariant 3 forbids the deep checks there).

**Where things live:**

- `src/ccrecall/run_history.py` (new): both sidecars' paths and read/write (`<db>.runs.json` and `<db>.unrepairable.json`), the finish record, and deriving `running` or `killed` from a kind's PID marker. The `backfill` kind means embedding backfill only; `backfill tool-content` and `backfill summaries` record nothing. Imports no vector or embedding modules. Called from `hooks/import_conversations.py`, `hooks/sync_current.py` and `hooks/backfill_embeddings.py`.
- `src/ccrecall/findings.py` (new): the `Finding` type, the catalog of checks, per-check isolation, and reducing findings to a verdict. It decides what a state means for `doctor` and `status`; SessionStart alerts keep their own hot-path checks.
- `src/ccrecall/health.py`: owns, as constants, the fix commands alerts and findings share, and `findings.py` imports them. The direction is forced: `health.py` can't import `findings.py`, which imports `db_vec` (invariant 3). Alert behavior doesn't change.
- `src/ccrecall/status.py`: renders both commands from findings: the verdict and second line for `status`, the full report, listing and exit codes for `doctor`, and their shared `--json` shape. `collect_status` and `print_status_report` are removed (D11); `findings.py` gathers what it needs through the existing counters, and `_readonly_connection` moves there, since `status.py` imports `findings.py`.
- `src/ccrecall/ingestion_status.py`: `summarize_ingestion` stops writing the ok-verdict cache (D1), so it can run on a read-only connection. `find_repairable_sessions` keeps writing it.
- `src/ccrecall/hooks/import_repair.py`: `repair_sessions` also returns the ids of sessions it couldn't fix, and the repair path writes them to `<db>.unrepairable.json`, through `run_history.py` (D9).
- `src/ccrecall/config.py`: the `backfill_stale_hours` setting (D7), and the ccrecall-process check in `read_marker_holder` (D4).
- `src/ccrecall/hooks/memory_setup.py`: `_spawn_background` reads its marker through `read_marker_holder` (D4).
- `src/ccrecall/cli/commands.py`: the `doctor` command and the changed `status` flags. `cli/__init__.py` and `recent_chats.py` update their references.

## Posture

- **Failure handling:** recording is best-effort and can never fail a run or hook, per #234. Each `doctor` check is isolated, so one that raises becomes a "couldn't check this" finding and the others still run. No retries.
- **Compatibility:** the PyPI package is public, so `--json` output is a contract and any break is deliberate, with a `BREAKING CHANGE:` footer (D11 takes one). Human output and the new sidecars' format can change freely.
- **Scope:** #234, #235 and #237 only, as listed in the Summary.
- **Rollout:** ships outright with no flag and no schema change. History starts empty on upgrade.
- **Hardening:** project defaults, no metrics. `doctor` and `status` can each take tens of seconds on a cold database (D1), so neither is fast enough for a shell prompt.
- **Altitude:** `build`; one session builds it, and the prerequisites #231, #232 and #233 have landed.

## Decisions

### D1: Where does `status` get the "is my history complete" answer?

**Situation:** Knowing whether history is complete means classifying every session against its transcript. Measured on the live database (7,689 sessions, 912 MB), that costs about 1.2s with the database in the OS file cache and 12-13s with it evicted. The cost is disk reads, not computation: each session's database fingerprint joins to the `messages` table, whose rows hold full message content. A first run after a gap looks mostly cold, since two such runs took about 9.5s. `status` can't simply leave the answer out: the audit's Q2 case is an install reporting fine while 105 sessions were incomplete.

A variant reading only indexes produced identical fingerprints for all 7,691 sessions and halved the classify step when cold (about 5.7s). It barely moved the total, from about 16.5s to about 14s cold and about 4.7s to about 4.6s warm, because the missing-vectors check then reads those same pages itself (2s rises to 6s). This change doesn't include it.

**Deciding factor:** one answer with no staleness to explain, given how little a recorded verdict saves.

| | A: Live in both | B: Recorded verdict for `status`, live for `doctor` | C: `status` leaves ingestion out |
|---|---|---|---|
| `status` time, warm (today: 3.0s) | About 5.7s | About 4.5s; 5.7s when the record is missing or old | About 4.5s |
| `status` time, cold (today: 11s) | About 19s, estimated from measured parts | About 13-17s, estimated | About 13-17s, estimated |
| Can the two disagree? | No | Yes, while the record is stale, up to 24h | Yes, indefinitely |
| Falsely reassuring? | No | For up to 24h, on a gap that appeared since the last scan | Yes, this is the audit's Q2 failure |
| New state | None | One field in the run-history sidecar, with three writers | None |
| What peers do | Tailscale and git run no deep check in `status`, but theirs cost far more relative to their job | Closest: borgmatic stores the last deep check's time | Tailscale and git don't run deep checks in `status` |

**Recommendation:** A, because a recorded verdict saves about 1.2s warm and little more cold, in exchange for a staleness window, three writers, and a same-verdict guarantee that would hold only right after `doctor`. `status` and `doctor` collect the same facts on a read-only connection. Neither writes the ok-verdict cache, which the background repair scan keeps warm. All database checks in one run share one deferred read transaction, opened after the read-only connection and closed at the end, so a sync committing mid-scan can't count a session as queued in one check and embedded in another; checks never commit on it. Transcript files aren't covered, and a WAL checkpoint can't pass that snapshot during the scan.
**Pick B instead if** first-run `status` time on large databases matters more than the two commands always agreeing. **Pick C instead if** you'd accept `status` ignoring incomplete sessions; this contradicts #237's "terse must not mean falsely reassuring".
**Reversibility:** easy (a recorded verdict can be added later if the time proves a problem)
**Ratified:** Chose live in both over a recorded verdict for `status`, to keep one answer with no staleness and no new state, accepting a slower `status` (about 5.7s warm, about 19s estimated cold), and that a run finishing during a read can show a brief contradictory finding, since run history and the database share no snapshot.

### D2: Where is the run history stored?

**Situation:** Runs need a record somewhere that a read-only `doctor` can read with no vector imports. Runs against one database must not appear in another's history. The audit found test runs against a database copy writing lines to the live `~/.ccrecall` log that looked like real ones. `RUNTIME_DIR` is global to the user, while `--db` can point anywhere.

**Deciding factor:** can record a run that couldn't open the database, and separates databases with no extra machinery.

| | A: JSON file beside the database (`<db>.runs.json`) | B: JSON file in `~/.ccrecall/`, named by a hash of the DB path | C: Table in the database |
|---|---|---|---|
| Records a run that couldn't open the DB | Yes | Yes | No |
| Separated per database | Automatic | Needs a hash key | Automatic |
| Survives moving or deleting the DB correctly | Moves if moved alongside it; a stale file can remain after deleting only the DB | Orphaned file, and a moved DB loses its history | Yes |
| Schema version or migration | None | None | Needs one |
| Readable by a read-only `doctor` | Yes | Yes | Yes |
| Follows existing conventions | `flock` and atomic write from `health.py`, in a new location | Same mechanics, same location as other sidecars | No sidecar; follows `schema.py` |
| What peers do | Kopia and resticprofile keep per-repo or per-profile state | resticprofile uses one status file | unknown |

**Recommendation:** A, because a run's history belongs with the database it describes, and a run that can't open the database is exactly what must be recorded. Writes use `health.py`'s `flock` pattern and `config.py`'s `atomic_write_json`.
**Pick B instead if** database directories may be read-only while `~/.ccrecall` isn't. **Pick C instead if** you'd accept not recording runs that fail before the database opens, in exchange for transactional history.
A recording failure is logged and never fails the run. The read, merge and write of `<db>.runs.json` all happen inside the lock, as `health.clear_embedding_failure` does; `_locked_status_file` only yields a lock, so a read outside it can overwrite another kind's fresh record.
**Reversibility:** easy (history is disposable; moving it loses only recent runs)
**Ratified:** Chose a JSON file beside the database over a hashed file in `~/.ccrecall` or a table, to record runs that can't open the database and separate databases automatically, accepting that a stale file can remain after deleting only the database, which can delay the backlog alarm by up to `backfill_stale_hours` and raise a failure from the old database until that kind's next run, that a stopped lock holder or hung filesystem can stall a run's recording, that with `--db` the job and embedding findings describe the whole install because PID markers and the embedding-status sidecar are global, and that a hand-edited sidecar can mislead a finding until the next run rewrites it. An unparseable `<db>.runs.json` is rewritten with only the current kind, so other kinds read as never-run until their next run.

### D3: Does every sync write a record, and how much is kept?

**Situation:** Sync runs on every Stop (228 on one audited day), while backfill runs nightly and repair runs at session starts. A sync's own work is much heavier than a small JSON write. A shared bound would let sync push out the only backfill record. Every consumer reads the newest run or the last success of one kind (D7, D8 and D13 each state which). No command shows a list.

**Deciding factor:** store only what a command reads, with one code path for all kinds. The format isn't a contract, so a list can be added when a command needs one.

| | A: Every run, last 20 per kind | B: Sync writes only failures, `last ingested` from `import_log` | C: Every run, one shared list | D: Every run, `{last, last_ok}` per kind |
|---|---|---|---|---|
| Time of the last successful sync | Read directly | Becomes `last ingested`, which also counts import and repair | Read directly | Read directly |
| Rare kinds can be evicted | No | No | Yes, within hours | No |
| Code paths | One, plus eviction | Two, plus clearing an old failure on success | One, plus eviction | One |
| File size | Under about 20 KB | About 1 KB | Bounded but lossy | About 1 KB |
| Yesterday's failure after a later success | Kept, but no command shows it | Logs only | Kept until evicted | Logs only |
| What peers do | Kopia: 50 per task type, every run | unknown | unknown | resticprofile, Atuin, borgmatic. The brief lists overwrite-only as an anti-pattern because a later success erases a failure; D8 clears on success anyway |

**Recommendation:** D. A run that skips because another holds its PID guard, or a sync that exits early for an invalid or excluded session, records nothing: no work was attempted. `import --repair-gaps` writes two records, `import` and `repair`.
**Pick B instead if** you'd rather have no routine write and accept the changed label. **Pick A instead if** you plan a recent-runs view soon.
**Reversibility:** easy
**Ratified:** Chose recording every run with the newest and last success kept per kind over a list or a failures-only sync, to store only what a command reads with one code path, accepting one small file write per sync and that older failures live only in the logs.

### D4: How is a killed run detected?

**Situation:** A run killed mid-way writes no finish record. The PID guards already mark a run's lifetime: each job writes its marker at acquire and removes it when it finishes, and `live_pid_holder` never reaps a stale one, so a killed run leaves a marker naming a dead PID until the next run of that kind acquires it. The logs show this happens: 6 of 104 backfills since July started and logged no completion.

**Deciding factor:** keep #234's "killed is detectable" with no new PID state.

| | A: Finish records only, killed not reported | B: Start and finish records with a PID | C: Finish records; `running` or `killed` read from the PID marker |
|---|---|---|---|
| #234 "killed is detectable" | Fails | Met | Met |
| Sidecar writes per recorded run | 1 | 2 | 1 |
| New PID state | None | PID in every record | None |
| Killed run against a `--db` copy | Not reported | Stays in the copy's history | Shows in every database's `doctor` until the next run of that kind |
| PID-reuse exposure | Existing guard only | Guard and record | Only another ccrecall process on that PID, once the holder must be ccrecall: any other reused PID reads as dead, and the next acquire reaps it |
| What peers do | Kopia writes on finish, no start marker | Healthchecks takes a start ping | restic's PID lock is the closest match |

**Recommendation:** C. A marker with a live PID reads as `running`, and one with a dead PID reads as `killed`. The marker's modified time is when the run started (it's written at acquire, or at spawn, and never touched again), so findings say "started at", never a kill time. Only a parseable, dead PID reads as `killed`; an empty or corrupt marker (`_spawn_background` creates it empty and writes the PID after `Popen` returns) reads as no marker, using `read_marker_holder`'s existing exception types. A PID counts as live only when it's a ccrecall process. When the signal-0 probe finds the PID alive and it isn't the caller's own (the own-PID case skips every probe, as today, since `_spawn_background` writes the child's PID first), `read_marker_holder` runs `ps -p <pid> -o command=` and looks for a whitespace-separated token whose basename is one of the package's console scripts: `ccrecall`, or `ccrecall-` followed by letters and hyphens only (so `.../bin/ccrecall import` and `ccrecall-warm-model`, which holds its own marker, match, and `tail -f ccrecall-import.log` doesn't). When `ps` succeeds and prints a command line with no such token, the holder raises `ProcessLookupError`, as a dead holder does, so `try_acquire_pid_file` reaps it and `live_pid_holder` returns None. When `ps` fails, is missing, or prints nothing, the PID stays live, as a `PermissionError` does today: a guard that can't identify its holder must not let a second backfill start. Tests that stand in a live holder need one whose command line has a `ccrecall` token. `_spawn_background` uses `read_marker_holder` instead of its own `os.kill` probe, so the guard, the spawn path and `doctor` share one liveness rule; today the spawn path counts `PermissionError` as dead while the others count it as alive. The finish record carries `started_at`, which with the marker's time is how #234's "record start" is met. `repair` has no marker of its own: it runs inside `import --repair-gaps`, so a killed one reads as one killed import. No heartbeat: the age limit in D6 and D7 covers a live run that stopped making progress.
**Pick B instead if** killed runs must be attributed to a specific database. **Pick A instead if** you'd amend #234 and rely on D7's clock alone.
**Reversibility:** easy
**Ratified:** Chose finish records with `running` and `killed` read from the existing PID marker over start records or not reporting kills, to keep killed runs detectable with one write and no new PID state, accepting that a killed run against a `--db` copy shows in the live `doctor` until that kind's next run. A live PID must be a ccrecall process, checked with `ps` in the one shared liveness reader, over rewording the stuck fix or accepting reuse, so a PID reused by anything but another ccrecall process never reads as running, never blocks that kind's runs, and never gets a kill instruction, accepting a `ps` call whenever a marker's PID is alive, including on the hook path, and that a `ps` failure leaves the PID read as live. Also accepted as known limitations: a spawned job that dies before reaching its own code reads as `killed`, and a repeating one stays "working on it" while its harm shows through `fixable-sessions`; and a kill between a finish record and the marker's removal reads as `killed` until that kind's next run.

### D5: What shape is a finding, and how does the verdict come from findings?

**Situation:** `status` and `doctor` must render the same findings. Neither peers nor analogues define a standard shape. Homebrew and OpenClaw emit `findings` lists with little documented structure. #235 groups output by what the user should do: needs action, working on it, nothing to do.

**Deciding factor:** one field that can't disagree with itself.

| | A: The group is the severity | B: Separate severity and group fields | C: Severity only, group derived at render time |
|---|---|---|---|
| Invalid combinations possible | None | Yes (for example "error" in "nothing to do") | None |
| Verdict rule | Any needs-action gives "needs attention"; else any working-on-it gives "working on it"; else "healthy" | Needs a second mapping | Same as A, through a mapping |
| `--json` readers | `group` is enough | Must learn two fields | Must learn severity names |
| What peers do | OpenClaw groups by action | Flutter and Homebrew use tiers | Flutter's result types |

**Recommendation:** A, because "working on it" describes who has the next move rather than how bad something is, so it fits a severity scale poorly, and severity names matching the groups would just be A with a mapping layer. A finding is a frozen dataclass: `id` (stable, kebab-case, part of the `--json` contract), `group` (`action`, `working` or `none` in `--json`, with the display text living in the renderers), `message` (plain language, says what's true and whether it's normal), `count` (optional), `fix` (optional exact command, or a named step where no one command fits, such as upgrading), `listable` (whether it names sessions). A `fix` is allowed in any group, so an optional step can be offered without demanding action. The `--json` verdict is `needs-attention`, `working` or `healthy`.
**Pick B instead if** you expect to rank findings within a group. **Pick C instead if** you want more levels than groups, or conventional severity words in `--json`.
**Reversibility:** hard for `id` and field names once released (`--json` is a contract), easy otherwise; a rank or severity field can be added later without a break
**Ratified:** Chose the group as the severity over a separate or derived severity, to keep one field that can't disagree with itself, accepting no ranking within a group until a field is added.

### D6: Which group does each finding belong in?

**Situation:** This catalog is what makes the verdict mean something. The live question is ordinary backlog: on a normal install, a few sessions are nearly always queued for embedding, so if queued work counts as "working on it", that becomes the usual verdict during the day. #235's example lists queued sessions under "Working on it". The catalog below applies the recommended option. Sessions in progress are kept out of the backlog either way, because `status` is usually run from inside one.

`<kind>` is `import`, `repair`, `sync` or `backfill` for failed runs. Marker-derived findings cover the kinds with a marker (D4): `import`, `sync` and `backfill` for killed runs, and `import` and `backfill` for running and stuck, since a sync is over in seconds. Findings marked listed name sessions (D12).

| Finding | `id` | Group | Fix |
|---|---|---|---|
| No database yet | `no-database` | Needs action | `ccrecall import` |
| Schema behind (`user_version` below this install's); database checks skip because they can't read an old shape | `schema-behind` | Needs action | `ccrecall import` |
| Schema ahead (`describe_schema_ahead`); checks still run best-effort | `schema-ahead` | Needs action | upgrade ccrecall |
| Fixable incomplete sessions (see D9), listed | `fixable-sessions` | Needs action; working on it while an import holds its PID guard | `ccrecall import --repair-gaps` |
| Embedding unavailable: vector extension won't load, the `embedding-status.json` record says failing, or the model isn't cached on disk. Read from those sources only; `doctor` never loads the model | `embedding-unavailable` | Needs action | named per cause |
| Backlog past the limit, unless a live run applies (see D7), listed | `embedding-backlog` | Needs action | `ccrecall backfill embeddings` |
| Failed run (see D8) | `<kind>-failed` | Needs action or working on it | rerun that command |
| Runtime dir or database not writable (`os.access`; no write probe) | `not-writable` | Needs action | check permissions and disk |
| More than one active branch for a session | `duplicate-active-branch` | Needs action | none; report a bug |
| Run looks stuck (import or backfill PID live, older than `backfill_stale_hours`). Says when it started and its PID, without claiming it's dead | `<kind>-stuck` | Needs action | kill that PID and rerun, if it's no longer making progress |
| Job running now (import or backfill PID live, younger than `backfill_stale_hours`) | `<kind>-running` | Working on it | none |
| Killed run (dead PID marker, see D8) | `<kind>-killed` | Working on it; nothing to do once the marker is older than `backfill_stale_hours` | rerun that command |
| Backlog within the limit: queued sessions, repaired sessions awaiting embedding, and exchanges missing vectors, each named separately, listed | `embedding-backlog` | Working on it | none |
| Sessions in progress (`pending_tail`, or queued and active within the last 15 minutes), listed | `sessions-in-progress` | Nothing to do | none |
| Deleted transcripts, messages still stored and searchable, listed | `deleted-transcripts` | Nothing to do | none |
| Deliberately skipped: a fixed sentence naming `exclude_projects` and stating subagent transcripts aren't indexed. No file walk, so no counts | `skipped-by-design` | Nothing to do | none |
| Draft-quality sessions only (searchable, not full quality), listed. Never needs action | `draft-sessions` | Working on it while the last successful backfill is within `backfill_stale_hours`; otherwise nothing to do | `ccrecall backfill embeddings`, only when nothing to do |
| Sessions that can't be embedded (content error, not retried), listed | `unembeddable-sessions` | Nothing to do | none |
| Sessions repair can't fix (see D9), listed | `unfixable-sessions` | Nothing to do | none |
| Tool content not backfilled (opt-in) | `tool-content-pending` | Nothing to do | `ccrecall backfill tool-content` |
| A check raised (Posture's per-check isolation); one finding per failed check, its message naming the check and the error's type | `check-failed` | Needs action | none; report a bug |

**Deciding factor:** the verdict says what's true about the install, as #235's example does, rather than optimizing for a reassuring default.

| | A: Backlog within the limit is "working on it" | B: Backlog within the limit is "healthy" with a note |
|---|---|---|
| Usual daytime verdict on an active install | Working on it | Healthy |
| Matches #235's example | Yes | No |
| "Healthy" means no backlog is waiting | Yes | No |
| Noise | The word appears often, but is true | Quiet, but a sizable backlog sits under "healthy" |
| What peers do | Tailscale prints warnings separately from running state | Same, seen from the other side |

**Recommendation:** A with the catalog above, because "healthy" should keep its plain meaning, and #235 and the audit's Q7 ("are the missing ones normal or a problem?") are answered by the group itself.
**Pick B instead if** you want "healthy" to be the usual answer and treat only overdue work as worth a change of verdict.
Missing vectors differ from #235's example, which says "tonight's run won't fix them": #243 made every backfill heal them first, so they count as backlog.
**Reversibility:** easy, one catalog row; ids are the contract, not groups' membership
**Ratified:** Chose the catalog as listed with backlog within the limit as "working on it" over "healthy" with a note, to keep "healthy" meaning no backlog is waiting, accepting that "working on it" will be a common daytime verdict, and that a hung sync isn't named: its effect shows as `fixable-sessions` once the 15-minute stale-tail window passes.

### D7: When does queued work count as stuck?

**Situation:** #235 asks when queued work stops being normal. A default install has no scheduled backfill, so its queue never drains; an install with a nightly timer can still have a dead timer. #236 adds that `doctor` must say so "whatever the marker says", so the schedule marker can't suppress this. Sessions with draft-quality vectors only are excluded (D6), because they're searchable. The run history is the only evidence, and on upgrade it's empty. No analogue defines this threshold; the nearest are restic's lock staleness and Healthchecks' grace time.

The right limit differs by install, so it's a `backfill_stale_hours` setting in `DEFAULT_SETTINGS` (`config.py`), defaulting to 72. A value that isn't a positive number falls back to the default with a logged warning, as `sync_path_token_limit` does. A limit below the backfill interval flags most days, because a session can't be embedded until the next run. The maintainer's nightly install would use 36. What remains is what the limit measures.

**Deciding factor:** a dead job surfaces within days, with no false alarm when nothing is actually overdue.

| With a 36h limit | A: Last backfill and oldest queued session both past the limit | B: Last successful backfill only | C: Oldest queued session only |
|---|---|---|---|
| Normal day, nightly job working | No alarm | No alarm | No alarm |
| Timer dies, last success Mon 01:15, next session ended Mon 09:00 | Tue 21:00 | Tue 13:15 | Tue 21:00 |
| Machine off for a long weekend, a session ends minutes after it's back | No alarm | False alarm until that night's run | No alarm |
| Repair recovers a months-old session, backfill healthy | No alarm | No alarm | False alarm until the next run |
| Backfill has never run, old sessions queued | Alarm | Alarm | Alarm |
| Cost | One `MIN(ended_at)` query | None | One `MIN(ended_at)` query |
| What peers do | unknown | Healthchecks' period and grace time are closest | unknown |

**Recommendation:** A. A backlog is "needs action" when no successful backfill has finished within the limit (including none recorded) and a backlog session (queued, repaired awaiting embedding, or with exchanges missing vectors) ended longer ago than the limit. An install with a working nightly job can see "needs action" right after upgrade until its first recorded run; the fix shown is harmless, and no baseline timestamp is stored to soften this. A `partial`, `failed` or `killed` backfill doesn't count as successful, and neither does a scoped one: a run with `--limit` or `--days` writes `last` but never advances `last_ok`. The finding names the last backfill's outcome and when it started ("last run, started 01:15, was killed"); a killed run's marker records only its start (D4). A live backfill PID means "working on it" until the run is older than the limit, when D6's stuck row applies instead: the longest real backfill on the maintainer's machine took 2.45h, and a full re-embed is estimated at 11-13h. Draft-only sessions use the last-successful-backfill clock alone (`last_ok`), since they never need action; D6 gives both sides of it.
**Pick B instead if** you'd accept the false alarm after a machine was off for a simpler rule. **Pick C instead if** you think of this as "nothing waits longer than N hours" and repaired old sessions flagging is fine.

A scoped run (`--limit`, `--days`, both in the README's seeding section) can finish `ok` after touching a sliver of the backlog:

| | A: Only unscoped runs advance `last_ok` | B: Record the scope; readers decide | C: Scoped runs count as successful |
|---|---|---|---|
| Nightly `--days 14` job, old sessions queued | Alarm fires; the printed unscoped fix clears it | Alarm fires | Never fires |
| One-off `--limit 1` | Doesn't reset the clock | Doesn't reset the clock | Resets it |
| New state | None; one condition at the write | One record field and a rule at each reader | None |

How long a live run counts as working on it, with A chosen because it needs no second number and no real run here approaches the limit. Pick D instead if hung runs must surface sooner than the limit:

| | A: Under `backfill_stale_hours` | B: No exception; the clocks apply during a run | C: Always, while the PID is alive | D: Separate fixed limit of 24h |
|---|---|---|---|---|
| Hung run flagged | At the limit | At the limit | Never | At 24h |
| Real long run | Flagged only past the limit, which no real run here approaches | False alarm on every first backfill, with a fix the guard skips | Never flagged | A machine about twice as slow could false-alarm on a full re-embed |
| New state | None; the marker's modified time | None | None | One constant |

**Reversibility:** easy for the clock; the setting's name is a contract once released
**Ratified:** Chose a `backfill_stale_hours` setting (default 72) with both clocks past the limit over a single clock, to fit each install's schedule with no false alarm after downtime, accepting one more documented setting, likely changed by few installs, and one extra query. A live run counts as working on it only under the same limit, so a hung job surfaces with no second number, accepting that a run legitimately longer than the limit is reported by `<kind>-stuck`. Only an unscoped backfill advances `last_ok`, over recording the scope or counting scoped runs, so `last_ok` means the whole backlog was worked, accepting that a scoped schedule alone keeps the alarm up.

### D8: Which failed or unfinished runs need action?

**Situation:** Most runs retry on their own: sync on the next Stop, import on the next timer or session start. Before #232 and #233 there were 17 import lock failures in a day, all retried with no data lost. Calling each of those "needs action" would teach users to ignore the verdict. Some failures don't clear by retrying, and a default install has no timer, so a plain "will retry" can be false.

**Deciding factor:** report what the user can act on, and rely on state findings for lasting harm.

| | A: Newest run per kind; "database busy" and killed are "working on it", other failures need action | B: Newest failure of any reason needs action | C: Needs action after 3 consecutive failures |
|---|---|---|---|
| A single lock timeout on import | Working on it | Needs action, until the next run | Not reported |
| Vector extension broken | Needs action | Needs action | Needs action after 3 runs; a manually run kind may never reach 3 |
| Backfill killed by a reboot | Working on it | Needs action | Not reported |
| A busy failure with no retry coming | State findings catch it: gaps stay fixable (D6), backlog passes the limit (D7) | Reported directly | Never reported |
| What peers do | restic splits locked from other failures | Homebrew's non-zero-on-anything, later softened | Healthchecks' grace time is similar in spirit |

**Recommendation:** A, because a run failing is only worth action when no later run or state finding will take care of it, and those findings already catch a retry that never comes. A later success of that kind clears it, and the next run's acquire removes a dead marker. Outcomes are `ok`, `partial` (finished with some failures) and `failed`, plus `running` and `killed`, read from the marker (D4). Failure reasons are plain categories: `database_busy`, `vector_unavailable`, `model_unavailable`, `error`. A `partial` import or repair follows the same rule by its reason. A backfill is `partial` when a branch failed for a reason the next run can retry, such as a lock timeout. A per-row content error is marked and never retried, so it doesn't make a run `partial`: otherwise `last_ok` could never advance and D7's alarm would point at a command that can't clear it.
**Pick B instead if** you'd rather see every failure and clear it by rerunning. **Pick C instead if** repeated failure is the only signal you care about.
A killed run only stays "working on it" while a retry is plausible:

| Killed marker older than `backfill_stale_hours` | A: Nothing to do | B: Needs action | C: Still working on it |
|---|---|---|---|
| Killed backfill, empty backlog, no schedule | Verdict can return to healthy, with the note and rerun still shown | Needs action, with nothing left to do | "Working on it" indefinitely |
| Killed backfill with sessions queued | D7 flags it and names the kill | Two needs-action findings for one cause | D7 flags it and names the kill |
| New rule | One comparison | One comparison | None |

A Ctrl-C (`KeyboardInterrupt`) runs `finally` and removes the marker, so it never reads as killed. Only attended runs get one; spawned jobs run detached with stdin on DEVNULL (`subprocess_utils.py:14-21`):

| Ctrl-C on a manual run | A: Records nothing | B: Records `partial`, reason `interrupted` | C: Records `failed` |
|---|---|---|---|
| False needs-action after a deliberate Ctrl-C | No | No | Yes, until the next success |
| `last` shows the interrupted run | No; shows the previous run | Yes | Yes |
| New reason name in `--json` | None | One | None |

An `Exception` records `failed` with reason `error`, including the one sync swallows before its `sys.exit(0)` (`sync_current.py:276-280`). An import that finishes with failed projects or repairs (`total_failures`, raised as `SystemExit(1)` at `import_conversations.py:488`) records `partial`, with reason `database_busy` when every failure was a lock (`is_database_locked`) and `error` otherwise. `SystemExit` is not an interruption: import's normal failure exit and sync's error exit raise it. Each abort path passes its own reason to the recording point, because `backfill_embeddings.run` returns the same `EXIT_ABORT` from four places (`:159`, `:190`, `:316`, `:323`) and a wrapper can't tell them apart from the return code.

**Reversibility:** easy for the grouping; outcome and reason names become a contract if `--json` exposes them
**Ratified:** Chose treating only failures a retry won't fix as needing action, with database-busy and killed runs as "working on it", over reporting every failure or waiting for three in a row, to keep the verdict actionable, accepting that a busy failure with no retry coming surfaces later through state findings rather than directly. A killed run moves to "nothing to do" once its marker is older than `backfill_stale_hours`, over needing action or staying "working on it", so the verdict stays true when no later run comes, accepting one more comparison. A Ctrl-C records nothing, over recording it as `partial` or `failed`, so a deliberate interruption raises no alarm and adds no reason name, accepting that `last` shows the run before it. Also accepted: a branch that times out on a lock every run keeps `last_ok` from advancing, so the backlog alarm stays up until that writer clears.

### D9: What about sessions repair has already tried and can't fix?

**Situation:** Repair reports `unrepairable` sessions when the transcript itself lacks the required messages. These stay classified as gaps and get retried on every background import, with no negative cache (`CLAUDE.md`, "Background repair"). If `doctor` calls all gaps "fixable", a user following its exact command would see the same finding forever. Today's live count is 0, but this is a recurring frustration: health checks telling the user to run a command that then reports nothing done. So a fix line appears only on sessions its command can change, and these sessions get their own list. Which sessions are unfixable can't be derived live: repair learns it only by reimporting and reclassifying, so the ids must be stored. Repair's behavior doesn't change; it keeps retrying, as `design/research/2026-11-01-repair-negative-cache/prior-art.md` recommends, and only `doctor` reads the list.

**Deciding factor:** the ids live where they stay small for runs that never touch them.

| | A: Own small file (`<db>.unrepairable.json`) | B: A field in the run-history file | C: Counts only |
|---|---|---|---|
| Separate unfixable list | Yes | Yes | No |
| A fix line can be wrong | Only until the next repair after a transcript changes | Same as A | Can't tell which sessions a count refers to |
| Run history stays about 1 KB (D3) | Yes | Grows with the problem | Yes |
| Ids rewritten on every sync | No, only by repair | Yes, once per sync | Not applicable |
| Files | Two sidecars, the second only when non-empty | One | One |
| What peers do | Recoll's skip-list and ripgrep-all's version in the key, per the brief | Same as A | restic and rclone log failures each run, per the brief |

**Recommendation:** A. Each entry stores the session id and its source fingerprint, which includes `ACTIVE_CHAIN_VERSION`. An entry stops applying when the fingerprint differs, so a changed transcript or a release that changes the walk makes that session fixable again until the next repair tests it. A completed repair replaces the file, or removes it when nothing is unfixable; an aborted repair only adds to it. `RepairCounts` carries the unfixable ids alongside its counts, so `repair_sessions` returns them and `RepairAborted.counts` carries them on the abort path. The file is written like the runs file: under its own `flock`, with `atomic_write_json` (D2). The "nothing to do" finding `unfixable-sessions` says the transcript is missing messages, retrying won't help, and what's stored stays searchable; it's listable and carries no fix. A session in the file with a matching fingerprint never counts toward the fixable finding. Before the first repair has run, incomplete sessions are simply fixable.
**Pick B instead if** you'd rather keep one file and expect this list to stay near zero.
**Reversibility:** easy (the file is disposable, and the next repair rebuilds it)
**Ratified:** Chose storing unfixable session ids and fingerprints in their own file written only by repair, over a field in the run history or counts alone, to give a separate list and keep every fix line true, accepting a second sidecar when any session is unfixable, and that until the next completed repair an entry can call a session unfixable because of a file change during repair or a database condition, not a transcript defect.

### D10: What exit codes do `doctor` and `status` use?

**Situation:** #235 settles that `doctor` exits non-zero when something needs action. #237 leaves `status` open: "`git status` doesn't; scripts may want it to". Today `status` exits 1 only for a missing database, and the audit asked for "exit code reflects it". Homebrew's non-zero-on-warnings later needed `--ignore-warnings`. Tailscale `status` exits 1 only when it isn't running.

**Deciding factor:** one command for scripts to gate on, with the glance safe to run anywhere.

| | A: `status` 0 unless it can't read; `doctor` 0/1, 2 for couldn't diagnose | B: Both exit 1 on needs attention | C: `status` encodes all three verdicts |
|---|---|---|---|
| Script gating | `doctor` | Either | `status` |
| `status` in a `set -e` script | Safe | Fails on any needs-action finding | Fails on "working on it" too, which is normal |
| Existing scripts calling `status` | Unchanged | Can break | Can break |
| Changing it later | Tightening is a clear, marked break | Loosening silently removes a gate | Same as B |
| What peers do | Tailscale, git, OpenClaw's exit 2 | Homebrew, npm | systemd `is-system-running` |

**Recommendation:** A. With live checks in both (D1), `doctor` costs about the same as `status`, so a second gate adds nothing. `status` exits 0 for any verdict it can produce from a database. It exits 1 when the database is missing, as today, while still rendering the `no-database` finding, human or `--json`, and exits 1 when it's unreadable, with a stderr line naming `ccrecall doctor`. `doctor` exits 0 for healthy or working on it, 1 for needs action (including no database, which is a finding), and 2 when the database exists but can't be read at all. A check that raises is a `check-failed` finding (D6), not exit 2. Exit 2 is also cyclopts' usage-error code (D12's unknown `--list` id), so a script can't tell the two apart by code alone; the usage error prints to stderr with no findings. Scripts wanting the verdict from `status` read `verdict` from `--json`.
**Pick B instead if** you'd rather have `status` itself gate scripts. **Pick C instead if** scripts need to tell "working on it" from "healthy" by exit code alone.
**Reversibility:** hard (exit codes are a script contract once released)
**Ratified:** Chose `status` exiting 0 unless the database is missing or unreadable and `doctor` exiting 0, 1 or 2 over non-zero verdicts from `status`, to keep one gate and an exit code safe in scripts and chains (its time rules out a shell prompt, per D1), accepting that scripts read `verdict` from `--json` to get it from `status`.

### D11: What happens to `status --json`, `--check-ingestion` and `--days`?

**Situation:** #237 moves `--check-ingestion` to `doctor` and requires any `--json` change to be deliberate. No skill or doc in this repo reads `status --json`, but outside scripts may. `--days` scopes the tool-content and embedding counts, which the terse human output no longer shows. `ingestion` in the JSON is `null` unless the flag was passed. With live checks in both commands (D1), `status` always runs that check, so the flag no longer changes anything. #238 will make related breaking removals later.

**Deciding factor:** one reporting shape with no legacy counts to maintain, with the break marked rather than silent.

| | A: Keep JSON fields and add to them; keep `--check-ingestion` as a hidden, ignored flag with a stderr note until #238; keep `--days` | B: Same, but remove `--check-ingestion` now as a marked break | C: Replace JSON with findings only, a marked break |
|---|---|---|---|
| Existing JSON readers | Unaffected | Unaffected | Break |
| Scripts passing `--check-ingestion` | Still get a fresh check, which is what they asked for | Unknown-option error; the `BREAKING CHANGE:` footer names `doctor` | Unknown-option error; the `BREAKING CHANGE:` footer names `doctor` |
| Release marked breaking | No | Yes | Yes |
| Leftover to remove | One hidden flag, removed with #238's other breaks | None | None |
| `collect_status` survives | Yes, with its counts | Yes | Removed |

**Recommendation:** C. `status --json` and `doctor --json` return one shape: `verdict`, `findings` and `summary`. `collect_status`, `print_status_report`, `--days` and `--check-ingestion` go away. The PR title uses `feat!:` with one `BREAKING CHANGE:` footer.
**Pick A instead if** you'd rather keep today's JSON fields and take the breaks with #238. **Pick B instead if** you want the flag gone now but the JSON fields kept.
**Reversibility:** hard for what ships in `--json`; easy for the flag
**Ratified:** Chose replacing `status --json` with findings only over keeping today's fields, to have one reporting shape with no legacy counts to maintain, accepting a breaking release ahead of #238. `status --json` and `doctor --json` share one shape: `verdict`, `findings`, and `summary`, which holds the second-line numbers from D13. `collect_status`, `print_status_report` and `--days` go away, and `--check-ingestion` is removed, so passing either flag gets the normal unknown-option error. The PR title uses `feat!:` and the body ends with one `BREAKING CHANGE:` footer naming the removed fields and flags and what to use instead.

### D12: How does a user list the sessions behind a finding?

**Situation:** #235 requires findings that name sessions to be able to list them, and leaves open what `--json` lists and how listing is requested. Counts today can reach hundreds (425 deleted transcripts, 358 missing vectors), so including every session always would make `--json` output large, and agents already pipe half of ccrecall's output through `head` (`agent-cli-friction` audit).

**Deciding factor:** the default stays readable, and the next command is printed rather than remembered.

| | A: `doctor --list <finding-id>` | B: `doctor --list` expands every finding | C: `--json` always includes sessions; `--list` for text only |
|---|---|---|---|
| Default `--json` size | Small | Small | Up to about 90 KB on the live DB |
| Discoverable | Each listable finding prints its exact command | One flag to learn | One flag, plus implicit JSON content |
| Getting one group | Direct | Filter it yourself | Filter it yourself |
| Text and JSON behave the same | Yes | Yes | No |
| What peers do | Homebrew runs named checks by argument | unknown | Syncthing leaves drill-down to its REST API |

**Recommendation:** A. Each row is session id, project and date; with `--json`, a `sessions` array under that finding, which only `--list` adds and which isn't part of the `Finding` type. `--list embedding-backlog` is one list whose rows also carry a reason: `queued`, `repaired` or `missing-vectors`. An unknown id is a usage error: it exits 2, as cyclopts usage errors do, and names the valid ones. A finding with nothing to list says so, as does a known finding that doesn't name sessions (exit 0).
**Pick B instead if** you usually want everything at once. **Pick C instead if** you expect JSON readers never to make a second call.
**Reversibility:** hard for the flag's shape once released; easy otherwise
**Ratified:** Chose `doctor --list <finding-id>` over expanding every finding or always including sessions in JSON, to keep default output small with the next command printed, accepting a second call to see sessions.

### D13: What goes on the second line of `status`?

**Situation:** #237 says a few numbers "earn their place" and sketches `7,514 sessions · 98% embedded · last sync 2 min ago`. The percentage must be honest, unlike today's 100%, which only counted chunks that exist.

**Deciding factor:** answer "is this thing alive and how much of my history can search reach" in one line.

**Recommendation:** the issue's three numbers. `embedded` is the share of embeddable sessions with no exchange lacking a current vector: draft-quality sessions count toward the numerator, queued sessions and those the missing-vectors check reports don't, and all of them stay in the denominator. `unembeddable-sessions` are the only ones left out of it. `summary`'s keys are `sessions` (every stored session), `embedded_pct` and `last_sync` (a timestamp or null). `last sync` is sync's `last_ok` from the run history, so a failing sync can't read as recent, and is left out until one has succeeded. When the verdict isn't healthy, the first line adds "(run `ccrecall doctor`)". The word "embedded" stays, since "searchable" would be untrue: keyword search works regardless.
**Pick "searchable" or more numbers instead if** you find "embedded" too internal for this line, or want last backfill shown as well.
**Reversibility:** easy for the human line; `summary`'s field names are a contract once released
**Ratified:** Chose sessions, percent embedded and last sync over also showing last backfill, to keep the line short and match #237's sketch, accepting that a stale backfill shows only through findings.

## Assumed

- The prerequisites have landed: #231 (`find_stamped_branches_missing_chunks` is read-only and its comment names doctor as a reuser), #232 (no write lock during inference) and #233 (a locked database costs one project or candidate). Evidence: all three closed on GitHub; `db_vec.py:248`.
- `find_repairable_sessions` runs at most session starts, because `_needs_reimport` spawns `import --repair-gaps` while `import_log` has a NULL `file_hash`, which sync writes. Evidence: `hooks/memory_setup.py:81`, `:157`; eight "repair complete" lines between 11:00 and 12:18 on 2026-11-02 in `ccrecall-import.log`.
- The repair path keeps the ok-verdict cache warm, so a read-only classification rarely has to parse. Evidence: one parse needed across 7,231 classified sessions in a read-only run on the live DB, 2026-11-02.
- A read-only connection can run `count_status`, `find_stamped_branches_missing_chunks` and `classify_sessions`. Evidence: all three ran on a `mode=ro` connection with `query_only` on the live DB, 2026-11-02.
- `get_connection` applies migrations, so `doctor` and `status` must use `_readonly_connection`. Removing `--check-ingestion` from `status` also resolves #182 item 1. Evidence: `status.py:29`, #182.
- `cli` already imports the vector stack, so `findings.py` importing `db_vec` costs nothing new on these commands, and neither command is on a hook path. `run_history.py` must stay free of those imports, per #234. Evidence: `CLAUDE.md` invariant 2; `status.py:16`.
- `import_conversations.run`, `sync_current.run` and `backfill_embeddings.run` each have one entry point where a run's start is known, though not one exit (abort paths pass their reason, D8), and `report_repair` already sees the repair counts, including an aborted batch through `RepairAborted.counts`. Evidence: `import_conversations.py:441`, `:492`, `:530`; `sync_current.py:157`; `backfill_embeddings.py:117`.
- Every job removes its PID marker when it finishes, and only the next acquire reaps a dead one, so a marker with a dead PID means that run never finished. Evidence: `hooks/sync_current.py:285`, `hooks/import_conversations.py:472-487`, `hooks/backfill_query.py:27`, `config.py:129-134`; no markers present in `~/.ccrecall` on 2026-11-02 with no job running.
- `ps -p <pid> -o command=` works with the same flags on Linux (procps) and macOS (BSD ps), needs no elevation for the user's own processes (markers are written only by ccrecall jobs the user runs), and costs about 13ms. Evidence: ten calls took 0.13s on this machine, 2026-11-03; `/proc/1/cmdline` (root's) was readable unprivileged; spawned jobs run the `ccrecall` console script (`pyproject.toml` `[project.scripts]`; `memory_setup.py:74`).
- `backfill embeddings --status` returns before any work, so it records nothing. Evidence: `backfill_embeddings.py:141`.
- Hook stdout stays untouched: recording prints nothing. Evidence: `CLAUDE.md` invariant 1.
- No `doctor` command exists yet. Evidence: `cli/commands.py`; grep of `src/`.

## Build

- [x] Implementation and tests committed
- [x] Docs
- [ ] Ship-time challenge

**Calls made during the build:**
- A backfill is `partial` only when a branch hit a lock timeout. A branch that changed or vanished during inference also isn't stamped and is retried, but that happens whenever a session is active, so counting it would keep `last_ok` from advancing on ordinary days.
- Sync records `ok` when its messages were committed, even when sqlite-vec is unavailable or its embed step failed and was logged. `embedding-unavailable` and the backlog report the embedding side. A swallowed lock error records `database_busy` rather than `error`, matching the other kinds.
- The backlog's `repaired` reason means a branch that has chunks but some exchanges without one. Repair produces that, but so does a sync whose embed step didn't finish, so the message says "with new messages since they were embedded" rather than naming repair.
- `summarize_ingestion` returns each category's session ids alongside its counts, since `--list` needs them. It no longer writes the ok-verdict cache (D1); tests that relied on it to warm the cache now warm it with `find_repairable_sessions`.
- Without the sqlite-vec extension, the coverage check counts any chunk as having its vector, since `chunk_vec` can't be read. `embedding-unavailable` reports the real problem.
- `doctor --list` exits 0 whatever the verdict: a listing isn't a gate.
- A WAL database in a directory ccrecall can't write to can't be opened even read-only, because SQLite must create its `-shm` file. `doctor` then exits 2 and `status` exits 1, with the not-writable reason in the error, instead of a `not-writable` finding. A read-only open also recreates the `-wal` and `-shm` files when they're missing; the database file itself is untouched.
- To tell a busy run from a failed one, `RepairCounts` gained `locked` and `unfixable`, and `import_project_contained` returns `ProjectFailed(locked)` instead of `None`.
- Sync's PID key moved into `config.py` as `PID_KEY_SYNC`, beside the other jobs' keys, so `run_history.py` can name it without importing the sync hook. `health._locked_status_file` became the public `locked_sidecar`, since the run-history files share it.
- `embedding-unavailable` is one finding listing every cause, with the first cause's fix.
- `skipped-by-design` is always present, in "nothing to do".
- An `import --repair-gaps` whose repair fails records a `partial` import as well as a failed repair, as D8 states, so one cause can show as both `import-failed` and `repair-failed`.
