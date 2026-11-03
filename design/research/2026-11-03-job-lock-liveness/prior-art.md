---
topic: "How local tools guard single-instance background jobs and report their liveness"
date: 2026-11-03
status: Draft
---

# Prior Art: Single-instance job locks and liveness

## The Problem

ccrecall runs import, sync, embedding backfill and warm-model as background jobs, one at a time per kind. Each job's PID marker file holds a bare PID, and every reader infers the rest: liveness from `os.kill(pid, 0)` plus a `ps -o command=` regex to rule out PID reuse, "killed" from a dead PID, and what a live job is doing (a `--repair-gaps` import, a scoped backfill) from the marker merely existing. Two challenges of spec 020 raised nine findings against this inference: clipped `ps` output reaping a live holder, the regex matching coincidental argv or missing `python -m`, a triple read racing re-acquire, an unreadable marker blocking spawns forever, and `doctor` claiming a running job will fix things it won't. Every proposed fix was a local patch.

## How We Do It Today

A marker is `RUNTIME_DIR/.pid-<key>` holding a decimal PID. `config.try_acquire_pid_file` creates it with a temp file plus `os.link`, reaping a dead or non-ccrecall holder. `memory_setup._spawn_background` creates it empty with `O_EXCL`, spawns the child, then writes the child's PID, so the child's own acquire sees its PID and counts it as acquired. `config.read_marker_holder` is the one liveness rule (own PID, `kill -0`, then `ps` and a console-script regex), with five callers: the guard, the spawn path, `live_pid_holder`, `run_history.read_marker` and, through it, `doctor`. `fcntl.flock` is already used, but only in `health.locked_sidecar` for the JSON sidecars' read-merge-write.

## Patterns Found

### Pattern 1: Kernel-held lock (flock), lock file content is incidental
**Used by**: tox-dev/filelock `UnixFileLock`, fasteners `InterProcessLock`, `flock(1)` cron idiom
**How it works**: The job opens a well-known file (never deleted) and takes a nonblocking exclusive `flock`. Contention means another live process holds it. The kernel releases on any exit including SIGKILL, so there is no staleness and no PID reuse question. Lock holder identity is not part of the protocol.
**Strengths**: No stale-lock recovery code; no PID probing; no ps parsing; atomic.
**Weaknesses**: Forked or spawned children that inherit the fd keep the lock (flock(2)); weak or different semantics on NFS/CIFS; by itself it says nothing about who holds it or what scope they run in; leaves no record of "killed".
**Example**: https://github.com/tox-dev/filelock/blob/main/src/filelock/_unix.py
**Read**: filelock `_unix.py` (read acquire path and class docstring); fasteners `process_lock.py` (grep lines only); flock(1)/flock(2) man pages (excerpts).

### Pattern 2: OS lock plus identity written into the locked file
**Used by**: recommended by the SO answer; episodic-memory does a variant (identity file, but mutex is separate)
**How it works**: After acquiring the flock, the holder writes a record (PID, and whatever else you want: start time, argv/mode, scope) into the locked file. Readers who want to display it first try a nonblocking flock probe: if they win it, nobody is running and the content is stale; if they lose, someone is, and the content describes them. No reader ever relies on os.kill or ps to decide liveness; the content is only for display.
**Strengths**: Liveness comes from the kernel; scope/mode can be recorded without risk of the diagnostic claiming a dead holder is running. A reader that loses the probe race simply reports "running (details unavailable)" if content is empty/partial.
**Weaknesses**: Reader that wins the probe now holds the lock and must release immediately (and must not itself be mistaken for a job); a holder writes the record after the lock so there is a brief empty window; no shipped project among those I read does the full thing [no source found beyond the SO answer].
**Example**: https://stackoverflow.com/questions/220525/ensure-a-single-instance-of-an-application-in-linux
**Read**: SO page partially (highlights plus confirmed text); episodic-memory `file-lock.ts` in full (it implements only the "write pid for diagnostics" half, with a separate mutex).

### Pattern 3: Unclean-exit detection via "unlocked but non-empty"
**Used by**: SO answer only [no shipped implementation found]
**How it works**: The holder truncates the file to zero length before a clean exit, still holding the lock. A reader that can take the lock and finds content knows the previous run died without cleaning up. Combined with a final-state record, you can distinguish running (lock held), killed (unlocked, record says "running"), finished (unlocked, record says done or empty).
**Strengths**: Gives running/killed/finished from one file with no PID probing.
**Weaknesses**: Kills during the truncate window are indistinguishable; a reader needs to take the lock briefly to read safely.
**Example**: https://stackoverflow.com/questions/220525/ensure-a-single-instance-of-an-application-in-linux
**Read**: secondary: the SO answer text via exa highlights.

### Pattern 4: PID (plus hostname) marker with probe and age cap
**Used by**: git `gc.pid`, restic, borg, filelock `SoftFileLock`
**How it works**: Marker names the holder (git: `pid host`; restic: JSON with pid/host/user/time; borg: `host.pid-thread` entry; filelock soft: pid, host, start token). Contender treats it as live if a same-host process with that PID responds to a signal, and as stale otherwise; different host means assume live. git and restic add a maximum age (12 h, 30 min); restic requires a periodic refresh.
**Strengths**: Works on filesystems without flock (NFS, shared repos); identity is inspectable by a human.
**Weaknesses**: PID reuse is only fully handled by filelock (start token). git, restic and borg accept it: bounded by age cap or by operator override (`git gc --force`, `borg break-lock`). Each carries stale-breaking logic and races around it.
**Example**: https://github.com/git/git/blob/master/builtin/gc.c
**Read**: git `builtin/gc.c::lock_repo_for_gc`; restic `internal/repository/lock_file.go`, `lock_file_unix.go`; borg `fslocking.py` (`kill_stale_lock`); filelock `_identity.py`, `_soft.py`.

### Pattern 5: Start-time token to defeat PID reuse
**Used by**: tox-dev/filelock `SoftFileLock` (and cites Qt QLockFile, PostgreSQL, Mercurial for proof-of-death-only breaking)
**How it works**: Record `(boot_id, /proc/<pid>/stat starttime)` on Linux (or process creation time on Windows) in the marker. A live PID with a different token is a recycled PID. If the token can't be read, treat as alive. This avoids the `ps -o command=` regex entirely: no argv parsing, no COLUMNS truncation, no `python -m` ambiguity. macOS has no /proc; filelock's non-Linux branch was not read in full, so I can't say what it uses there [gap].
**Strengths**: Exact reuse detection without matching command lines; fail-closed.
**Weaknesses**: Linux-specific without a macOS equivalent read here; filelock needs namespace and time-namespace special cases.
**Example**: https://github.com/tox-dev/filelock/blob/main/src/filelock/_identity.py
**Read**: `_identity.py` in full for the Linux and Windows branches; macOS branch not read.

### Pattern 6: Heartbeat (mtime refresh) with stale window
**Used by**: proper-lockfile (and therefore episodic-memory), restic (5 min refresh, 30 min stale)
**How it works**: Holder touches the lock periodically; contenders treat a lock whose last touch is older than the stale window as dead. No PID probing needed (episodic-memory's PID is diagnostic only).
**Strengths**: Works on any filesystem; no process inspection.
**Weaknesses**: A killed job reads as running until the stale window elapses (10 min in episodic-memory, 30 in restic); needs a timer thread; a paused holder (SIGSTOP, laptop sleep) gets its lock stolen (`onCompromised` exists for this).
**Example**: https://github.com/obra/episodic-memory/blob/main/src/file-lock.ts
**Read**: episodic-memory `file-lock.ts` (full); proper-lockfile README (not its source); restic `lock.go` constants.

## Anti-Patterns

- **Reading the PID file to decide liveness with no owner check on a recycled PID**: Baeldung and mironsoft both name it. Source: https://www.baeldung.com/linux/bash-ensure-instance-running
- **Deleting the lock file to release an OS lock**: breaks mutual exclusion because waiters split across inodes (filelock `UnixFileLock` docstring; SO/ServerFault "create once, never delete").
- **Treating an unparseable or half-written marker as stale immediately**: filelock waits until the marker has been unchanged for an age threshold, because a creator writes the marker after creating it. git does the opposite (proceeds), which is safe for gc but wrong if the marker can be read mid-write.
- **Breaking an unreadable lock**: borg and filelock fail closed (don't call it stale) when they can't read or identify; this is the opposite of the "unreadable marker blocks forever" failure unless a manual override or age cap exists.
- **Letting a child inherit the lock fd**: flock(1) `--close` / flock(2) shared open file description; a detached child spawned from the holder keeps the lock after the parent dies.

## Emerging Trends

- tox-dev/filelock's recent versions have moved to proof-of-death-only reclaim with start tokens and PID-namespace-aware hostnames, which is notably more machinery than the older libraries.

## Gaps

- None of the four named comparable projects except episodic-memory has any locking; claude-code-log, obliscence and session-indexer were grepped across the whole tree (no flock/fcntl/lockfile/pid-marker code) and only false positives came back. I did not check their issue trackers for won't-fix discussions.
- No project found that implements Pattern 2/3 (flock + identity record + truncate-on-clean-exit). Only the SO answer describes it.
- fcntl(2) "any close releases all locks" passage not verified (extracted text lacked it). Python docs for `fcntl.lockf` vs `flock` not opened.
- filelock's macOS/BSD `process_start_token` branch not read, so no verified answer on a macOS start-time source.
- PostgreSQL `postmaster.pid`, Qt QLockFile and Mercurial locks were named by filelock's docstring but I did not open their source.
- Did not search for blog posts or issues on NFS flock failures beyond the man pages.

## Relevance to Us

ccrecall's markers are Pattern 4 (PID marker with a probe) with a `ps` regex standing in for Pattern 5's start token, which is how the regex and `COLUMNS` findings arise. Its "what is the job doing" guess has no counterpart in any project read, because the ones that answer it write the answer down (restic's JSON, filelock's soft marker, episodic-memory's diagnostic PID file).

ccrecall's runtime directory is local and per user, and Windows is unsupported, so Pattern 1's main weaknesses (NFS, Windows) don't apply. Pattern 1 plus Patterns 2 and 3 would remove the PID probe, the `ps` call, the regex and every reap loop: the kernel says whether a job is running, and the file's content says who and in what scope, for display only. The spawn path's handoff (the parent creates the marker, the child adopts it) maps onto flock's documented inheritance: the parent takes the lock and passes the descriptor to the child, which is exactly the behaviour flock(1)'s `--close` exists to prevent in the opposite case.

Of the four peers, only episodic-memory guards jobs at all, and its real mutex is not the PID file. Its PID file is display only, the same split Pattern 2 makes.

## Recommendation

Replace the PID probe with a kernel-held `flock` on the marker file (Pattern 1), and write the holder's identity and scope into the locked file for display (Pattern 2): PID, kind, start time, and what the run covers (`repair_gaps`, a backfill's `--days`/`--limit`). A job clears the record before releasing on a clean exit, so an unlocked file with a record means the run was killed (Pattern 3). This deletes the `ps` call, the regex, the `kill -0` probe and the reap loops, and answers the scope question by recording it instead of guessing.

Be honest about the gap: no shipped project implements Patterns 2 and 3 together; each half is standard (filelock's `UnixFileLock`, flock(1)'s inherited-descriptor behaviour), and the combination is a widely cited Stack Overflow recipe. The alternative peers actually ship, PID plus host with an age cap and PID reuse accepted (git, restic, borg), is the "accept and document" option: keep today's markers, add `ps -ww`, and document the remaining reuse and `python -m` limits.

## Sources

### Reference implementations
- https://github.com/obra/episodic-memory/blob/main/src/file-lock.ts — mkdir lock plus heartbeat; PID file is diagnostic only
- https://github.com/restic/restic/blob/master/internal/repository/lock_file.go — JSON lock with pid/host/user/time, refresh and stale timeout
- https://github.com/git/git/blob/master/builtin/gc.c — `gc.pid` with pid and host, 12-hour cap, `--force`
- https://github.com/borgbackup/borg/blob/master/src/borg/fslocking.py — host.pid-thread entries, fail closed on anything unreadable
- https://github.com/tox-dev/filelock/blob/main/src/filelock/_unix.py — `UnixFileLock`, nonblocking flock, file never deleted
- https://github.com/tox-dev/filelock/blob/main/src/filelock/_identity.py — start-token PID reuse detection
- https://github.com/harlowja/fasteners/blob/main/fasteners/process_lock.py — OS lock, no identity
- https://github.com/daaain/claude-code-log, https://github.com/beaugunderson/obliscence, https://github.com/valpere/session-indexer — no job locking

### Blog posts & writeups
- https://stackoverflow.com/questions/220525/ensure-a-single-instance-of-an-application-in-linux — flock plus identity, truncate on clean exit
- https://serverfault.com/questions/129814/minutely-cron-ensuring-only-a-single-instance — flock(1) over pidfiles (highlights only)
- https://www.baeldung.com/linux/bash-ensure-instance-running (not opened in full)
- https://www.mironsoft.de/en/blog/bash-locking-with-flock-pid-files-and-race-conditions (not opened in full)

### Documentation & standards
- https://man7.org/linux/man-pages/man1/flock.1.html
- https://man7.org/linux/man-pages/man2/flock.2.html
- https://github.com/moxystudio/node-proper-lockfile
