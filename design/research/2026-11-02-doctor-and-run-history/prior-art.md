---
topic: "Doctor and status commands, and recording the outcome of background runs, in local CLI tools"
date: 2026-11-02
status: Draft
---

# Prior Art: Doctor, Status and Background-Run Records

## The Problem

ccrecall runs detached background jobs (import, repair, a sync on every Stop hook, a nightly embedding backfill) and offers no way to learn what they did or whether the install is healthy without reading logs. The milestone adds a `doctor`, a terse `status` carrying a verdict, and a bounded record of runs. This survey asks how comparable tools shape findings, use exit codes, store run outcomes, detect killed runs, stay read-only, and decide that queued work is stuck.

## How We Do It Today

`status` prints internal counts and exits 0 unless the database is missing. `--check-ingestion` opens a writable, migrating connection and writes the ok-verdict cache. Background runs leave only rotating per-process logs. Small JSON sidecars already live in `~/.ccrecall/` (`health.py`), written atomically and guarded by `flock` where two writers can race. Each job has a PID guard that says whether it is running now, and nothing about how it last ended.

## Patterns Found

### Pattern 1: Doctor as independent checks that produce findings

**Used by:** Homebrew, Flutter, npm, OpenClaw; inline in episodic-memory and Atuin.
**How it works:** Each check runs in isolation, so one crash can't abort the others (Flutter turns a thrown exception into a `crash` result). The overall verdict is a reduction over the results. Flutter has five result types, OpenClaw groups output as "Fix now" and "Recommended improvements", and npm and episodic-memory keep a flat pass/fail list.
**Strengths:** Easy to extend; per-check isolation; output can be grouped by what the user should do.
**Weaknesses:** No standard finding schema exists. Flat lists can't rank.
**Example:** https://github.com/Homebrew/brew/blob/master/Library/Homebrew/cmd/doctor.rb
**Read:** Homebrew `cmd/doctor.rb` (code); Flutter `doctor.dart` (code); npm `doctor.js` (grep); episodic-memory `src/doctor.ts` (code); Atuin `doctor.rs` (code); secondary: OpenClaw docs page, through a fetch summary.

### Pattern 2: Doctor exit codes follow severity, with an escape hatch

**Used by:** Homebrew, Flutter, npm, episodic-memory, OpenClaw, Atuin.
**How it works:** Three camps. Always 0 treats doctor as an information dump (Atuin). Any finding is non-zero (Homebrew, npm, episodic-memory). Tiered lets warnings pass (Flutter; OpenClaw's `--lint`). Homebrew added `--ignore-warnings` in PR #17864 because automation couldn't tell harmless warnings from hard errors. OpenClaw reserves exit 2 for "could not diagnose".
**Strengths:** Tiers serve humans and scripts.
**Weaknesses:** Warnings that fail the exit code erode trust; always 0 is useless to scripts.
**Example:** https://github.com/Homebrew/brew/pull/17864
**Read:** Homebrew PR and `doctor.rb` (code); Flutter `commands/doctor.dart` (code, mapping to the process exit code not traced); Atuin `doctor.rs` (code); episodic-memory `doctor-cli.ts` (code); npm (grep); secondary: OpenClaw docs, through a fetch summary.

### Pattern 3: Terse status exits non-zero only when the thing isn't operating

**Used by:** Tailscale, git, systemd, restic.
**How it works:** Tailscale `status` exits 1 only when the backend isn't running or starting, and prints health warnings with exit 0. `git status` has no exit-code option; scripts test `--porcelain`. systemd gives each question its own predicate command. restic gives missing, locked and wrong-password distinct codes.
**Strengths:** Sits in a prompt or script without noise.
**Weaknesses:** A caller that checks only the exit code misses warnings.
**Example:** https://github.com/tailscale/tailscale/blob/main/cmd/tailscale/cli/status.go
**Read:** Tailscale `status.go` (code); secondary: git-status docs, systemctl man page and restic scripting docs.

### Pattern 4: Latest outcome per kind in a sidecar

**Used by:** resticprofile; Atuin (`last_sync`); borgmatic (check timestamps).
**How it works:** After a run, write `success`, `time`, `duration`, `error` keyed by command, keeping only the latest.
**Strengths:** Trivial, and no growth to manage.
**Weaknesses:** A later success erases the last failure. A killed run writes nothing.
**Example:** https://creativeprojects.github.io/resticprofile/monitoring/status/index.html
**Read:** Atuin `doctor.rs` (code); secondary: resticprofile and borgmatic docs, through fetch summaries.

### Pattern 5: Bounded per-kind run history with a success flag

**Used by:** Kopia.
**How it works:** `Runs map[TaskType][]RunInfo`, newest first, trimmed to 50 per task type, with Start, End and Success. Logic that depends on prior runs filters to successful ones. Written when the run ends.
**Strengths:** Failures are kept, and a frequent kind can't evict a rare one.
**Weaknesses:** Every run writes. No start marker, so a killed run leaves no record.
**Example:** https://github.com/kopia/kopia/blob/master/repo/maintenance/maintenance_schedule.go
**Read:** Kopia `maintenance_schedule.go` and `maintenance_run.go` (grep of key lines).

### Pattern 6: Detecting a killed run with a start marker and PID or heartbeat

**Used by:** restic, episodic-memory (through proper-lockfile), Healthchecks.io.
**How it works:** restic's lock records PID and hostname, refreshes every 5 minutes, and is stale after 30 minutes or sooner if the PID is unreachable on the same host. proper-lockfile uses an mtime heartbeat with a 10-minute window as episodic-memory configures it. Healthchecks takes a start ping and marks the job down if no success arrives within a grace time.
**Strengths:** Tells running from dead without the job finishing.
**Weaknesses:** Heartbeats need periodic writes and a guessed threshold. A PID probe alone can be fooled by PID reuse.
**Example:** https://github.com/restic/restic/blob/master/internal/repository/lock_file.go
**Read:** restic `lock_file.go` and `lock.go` (grep of key lines); episodic-memory `src/file-lock.ts` (code); secondary: Healthchecks docs, through fetch summaries.

### Pattern 7: Cheap checks by default, deep checks behind a flag or a frequency

**Used by:** restic, borgmatic.
**How it works:** `restic check` is structural by default; `--read-data` and `--read-data-subset` are opt-in. borgmatic stores each check's last-run time and runs expensive checks at most once per `frequency`.
**Strengths:** The default stays fast.
**Weaknesses:** A cached "ok" can go stale.
**Example:** https://restic.readthedocs.io/en/stable/045_working_with_repos.html
**Read:** secondary: restic and borgmatic docs, through fetch summaries. The cost here is network and storage reads, far above parsing local files.

### Pattern 8: Read-only diagnosis with an explicit mutation switch

**Used by:** OpenClaw, Atuin, Django, git.
**How it works:** OpenClaw's read-only postures reject mutating flags. Atuin's doctor avoids a call because it has side effects. Django's `migrate --check` reports unapplied migrations by exit code without applying them. git documents `--no-optional-locks` because plain `status` writes the index.
**Strengths:** Safe during an incident or from a hook.
**Weaknesses:** No source found for how any tool reports an SQLite schema that is behind.
**Example:** https://docs.openclaw.ai/cli/doctor/running
**Read:** Atuin `doctor.rs` (code); secondary: OpenClaw, Django and git docs.

### Pattern 9: Accepting the limitation that "didn't run" is someone else's job

**Used by:** pgBackRest, borgmatic, resticprofile.
**How it works:** pgBackRest's maintainer declined to report "backup failed to start" because, without its own scheduler, any such report "would likely just be guesses". borgmatic says the same of a job runner that never starts it, and points to external monitoring.
**Strengths:** The tool makes no claim it can't back.
**Weaknesses:** The user needs a second system. ccrecall spawns some of its own jobs, but its nightly backfill is scheduled outside the package, so the argument partly applies.
**Example:** https://github.com/pgbackrest/pgbackrest/issues/2690
**Read:** pgBackRest issue #2690 (maintainer comment); secondary: borgmatic and resticprofile docs.

## Anti-Patterns

- **Warnings that fail the exit code.** Homebrew later needed `--ignore-warnings` (https://github.com/Homebrew/brew/pull/17864).
- **One non-zero meaning everything.** restic splits missing, locked and wrong-password from generic failure (https://restic.readthedocs.io/en/stable/075_scripting.html).
- **A "read-only" status that writes.** git status refreshes the index (https://git-scm.com/docs/git-status).
- **Overwrite-only last-run records.** A later success erases the last failure, as in resticprofile; Kopia's bounded list avoids it.

## Gaps

- None of the four named comparables (claude-code-log, obliscence, episodic-memory, session-indexer) records run outcomes or has an index-health doctor. episodic-memory's doctor checks integration setup only.
- No standard finding shape was confirmed. Homebrew's `Finding` class wasn't read; OpenClaw's page names only `ok` and `findings`.
- No read-only doctor was found that reports an SQLite schema that is behind.
- No tool's threshold for "queued work is stuck" was found. The nearest evidence is lock staleness and Healthchecks' grace time.
- Several documentation claims rest on fetch summaries, so re-check quotes before reusing them verbatim.

## Relevance to Us

ccrecall is ahead of its direct peers on this: none of them attempts it, so the analogues are the useful comparison, and they are general tools rather than Claude Code indexers.

The closest shape is OpenClaw's grouping by user action with a distinct "couldn't diagnose" exit, plus Tailscale's status exit semantics and Kopia's bounded per-kind history. Only ccrecall's existing PID guard and a start record are needed to catch killed runs; restic's heartbeat is more machinery than a single-host, single-user tool needs, since the PID probe already covers it and its weakness is PID reuse. Nobody defines a stuck-queue threshold, so that number is ccrecall's own call.

## Recommendation

- Group findings by user action, with the group doubling as severity. No peer provides a schema worth copying, so keep it small.
- Exit `doctor` non-zero only on "needs action" and use a separate code for "couldn't diagnose". Keep `status` at exit 0 unless it can't read the database (Tailscale, git).
- Keep a bounded per-kind history in a sidecar (Kopia's shape, ccrecall's existing sidecar mechanics). Add a start record so a dead PID marks a killed run. Skip heartbeats.
- Stay read-only with no mutation switch at all: the repair commands already exist, and `doctor` names them.
- Whether the deep ingestion check runs by default is a cost question for this codebase, since restic's reason for gating is network reads. Measure it before gating it.

## Sources

### Reference implementations
- https://github.com/obra/episodic-memory — the only direct peer with a doctor; setup checks, exit 1 on any issue, plain log file
- https://github.com/valpere/session-indexer — no doctor, status or run record
- https://github.com/beaugunderson/obliscence — no doctor, status or run record
- https://github.com/daaain/claude-code-log — no doctor, background jobs or run record
- https://github.com/Homebrew/brew/blob/master/Library/Homebrew/cmd/doctor.rb — named checks, findings, non-zero on any finding
- https://github.com/Homebrew/brew/pull/17864 — `--ignore-warnings`
- https://github.com/flutter/flutter/blob/master/packages/flutter_tools/lib/src/doctor.dart — typed results, per-check crash isolation
- https://github.com/npm/cli/blob/latest/lib/commands/doctor.js — flat `allOk`
- https://github.com/atuinsh/atuin/blob/main/crates/atuin/src/command/client/doctor.rs — information dump, always exit 0, avoids side effects
- https://github.com/tailscale/tailscale/blob/main/cmd/tailscale/cli/status.go — exit 1 only when not running
- https://github.com/kopia/kopia/blob/master/repo/maintenance/maintenance_schedule.go — 50 runs per task type
- https://github.com/restic/restic/blob/master/internal/repository/lock_file.go — lock with PID, host and staleness
- https://github.com/pgbackrest/pgbackrest/issues/2690 — declined to report "failed to start"
- https://github.com/syncthing/syncthing/issues/10385 — no per-folder CLI status

### Documentation & standards
- https://docs.openclaw.ai/cli/doctor/running — read-only postures, exit 2 for a blocker, "Fix now" grouping
- https://creativeprojects.github.io/resticprofile/monitoring/status/index.html — latest-result status file
- https://restic.readthedocs.io/en/stable/075_scripting.html — distinct exit codes
- https://restic.readthedocs.io/en/stable/045_working_with_repos.html — shallow check by default
- https://torsion.org/borgmatic/reference/configuration/consistency-checks/ — check frequency
- https://torsion.org/borgmatic/how-to/monitor-your-backups/ — external monitoring
- https://healthchecks.io/docs/monitoring_cron_jobs/ — start signal and grace time
- https://git-scm.com/docs/git-status — porcelain format, optional locks
- https://www.freedesktop.org/software/systemd/man/latest/systemctl.html — predicate commands
- https://docs.djangoproject.com/en/6.1/ref/django-admin/ — `migrate --check`
- https://clig.dev/ — general CLI baseline
