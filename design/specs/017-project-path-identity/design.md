# Design: Project path is metadata, not identity

**Date:** 2026-10-20
**Status:** ratified
**Mode:** sketch
**Issues:** #221

## Summary

`projects.path` is declared `UNIQUE NOT NULL` (`src/ccrecall/schema.py:19-26`), yet `upsert_project` (`src/ccrecall/project_ops.py:30-86`) rewrites it whenever a freshly derived path differs. That combination produces every failure in #221:
- A moved repo's new path collides with an orphan row holding it. The resulting `IntegrityError` aborted every `ccrecall import` for about 3 days, 963 failures in the log.
- A worktree dir with no cwd overwrites an accurate path with a lossy slug decode (live today for otf-api).
- The insert branch's `ON CONFLICT(path) DO UPDATE SET key = ...` silently takes over another project's key (pending for the `personal-telegram-bot` dir).

Nothing looks a project up by path, and nothing reads it. `src/ccrecall/recent_chats.py:41` selects `p.path` but binds it to `_project_path` and discards it (`:77`, `:92`). What matters downstream is `projects.name`, the path's basename (`formatting.py:109-111`), which drives the `--project` filter (`search_query.py:77`) and the import exclusion check (`src/ccrecall/hooks/import_conversations.py:313-321`).

The change has four parts:
1. **Drop the UNIQUE constraint on `projects.path`** with a version-9 migration that rebuilds the `projects` table (`src/ccrecall/db_base.py`). Also update `SCHEMA_CORE` in `src/ccrecall/schema.py` so fresh installs get the same shape. The new shape is otherwise unchanged: `id INTEGER PRIMARY KEY`, `path TEXT NOT NULL` (no UNIQUE), `key TEXT UNIQUE NOT NULL`, `name TEXT`, `created_at DATETIME DEFAULT CURRENT_TIMESTAMP`, plus `idx_projects_key`.
2. **Rewrite the write rules in `upsert_project`.** Identity is the normalized key. An accurate path (from a cwd) always replaces the stored path. A lossy path (slug decode) is written only when inserting a brand-new row, never over an existing one.
   - The SELECT-by-key branch stays first for existing rows, so a project whose path is unchanged remains a pure SELECT.
   - The insert is `INSERT ... ON CONFLICT(key) DO NOTHING` followed by the existing `SELECT id FROM projects WHERE key = ?`. A losing racer's insert is dropped, so a stored row is never overwritten, and no `RETURNING` (SQLite 3.35) is needed. If an accurate insert loses that race to a lossy one, the accurate path lands on that key's next write, which heals the row. This was settled by the sketch-time challenge, Finding 5 (finding numbers refer to `review-log.jsonl` in this directory).
3. **Change the cwd probe.** `_probe_project_dir` (`project_ops.py:108-132`) follows D4's probe rule instead of using `files[0]`.
4. **Contain per-project import failures** in both branches of `_run` (`src/ccrecall/hooks/import_conversations.py:461`, around the `import_project` calls at `:520` and `:537`) per D3.

Prior research, including real-data measurements and peer prior art: `design/research/2026-10-20-issue-221-project-path-identity/research.md`.

Out of scope:
- Deleting or repairing existing rows (78 zero-session orphans, 15 keys that don't match their path). Normal imports heal what matters.
- A worktree-specific skip.
- Arbitrating between multiple project dirs that normalize to the same key. The last one processed wins; in real data they resolve to the same path.
- CLI-wide friendly database error messages, including surfacing failed projects through a `health.py` sidecar. This is tracked in #223.
- The order of exclusion relative to the probe and row write. It is deliberately unchanged here and tracked in #222.

## Posture

- **Failure handling:** the migration fails loudly, rolling back and raising like the existing ladder. Path conflicts stop existing, so no conflict-handling code is added. How a failure inside one project's import is handled is D3, raised by the user's request for better database error messaging.
- **Compatibility:** stored data is a public contract (CLAUDE.md). The migration preserves every row and every `projects.id`. UNIQUE on `projects.path` is not part of any documented surface. `--json` output shapes are unchanged. The one accepted exception is D3: interactive `ccrecall import` gains per-project failure lines, a failure count in its summary, and a non-zero exit when a project fails. This was amended after the comb.
- **Scope:** minimal: the migration, the write rules, the probe, and D3's per-project import containment, which the user asked for. D3 was amended after the comb. No data cleanup, no worktree skip, no cross-dir arbitration.
- **Rollout:** ships outright as schema v9. No flag.
- **Hardening:** project defaults. Logging follows `rules/common/logging.md`.
- **Altitude:** `build`. One session builds it, and the ledger cites files.

## Decisions

### D1: How to remove the uniqueness of `projects.path`

**Deciding factor:** remove the conflict class without leaving a misleading column behind.

| | A: v9 rebuild of `projects` in the version-gated ladder | B: keep `path` UNIQUE but store the key in it, add a non-unique `display_path` | C: no schema change, guard and catch in code (research option A) |
|---|---|---|---|
| Removes collision crashes | yes, structurally | yes, the key is already unique | only where every write site is guarded |
| Removes the insert re-key hazard | yes, `ON CONFLICT(path)` goes away | yes | no |
| Column meaning afterward | `path` means path | `path` means key, a trap for future readers | unchanged |
| Migration risk | table rebuild. It must run inside `BEGIN IMMEDIATE` after `prepare` loads vec (`db_base.py:317-345`), which the v0–v7 matrix with vec0 objects already exercises (`tests/test_db.py:1551-1570`) | additive `ADD COLUMN` plus a backfill UPDATE | none |
| Moved repo heals itself | yes | yes | only if the conflicting row is deleted |
| Consistent with the research answer | yes (option C there) | partly | no, the research rejected it |

**Recommendation:** A, because it is the only option that both removes the conflict class and leaves `path` meaning what it says. The rebuild risk is the same shape v1/v2 already carry, and the matrix tests cover it.
**Pick B instead if** a table rebuild on users' databases is unacceptable for this release.
**Pick C instead if** the schema must not change at all. It leaves the re-key hazard and stuck moved repos.
**Reversibility:** hard. Re-adding UNIQUE later would require deduplicating paths.
**Ratified:** Chose A, a v9 rebuild of `projects`, over key-in-path or a code-only guard, to remove the conflict class while keeping `path` meaningful. This accepts three costs:
- A table rebuild runs on users' databases.
- A downgraded or version-skewed pre-v9 ccrecall fails on inserting any new project, because its `ON CONFLICT(path)` matches no constraint (challenge Finding 11).
- A failed v9 leaves the DB intact at v8, but every command and hook raises on open, with no friendly message, until the cause is fixed (challenge Finding 12).

### D2: How a row records that its path was invented from the slug

This revisits a research-stage answer ("insert with a lossy flag"). The premise changes once D1 removes uniqueness. The write rule "a lossy path never overwrites an existing row; an accurate path always does" needs no stored marker, because a lossy path for a key is always the same string. The flag was framed as what stops lossy rows from blocking accurate ones, but after D1 nothing blocks. So the only remaining job of a marker is to let readers tell invented paths apart.

**Deciding factor:** the least schema and code that still keeps every row correct.

| | A: no marker | B: `path_is_lossy INTEGER NOT NULL DEFAULT 0` column | C: nullable `path`, NULL when unknown |
|---|---|---|---|
| Write rule works | yes | yes | yes |
| Extra migration work | none | add the column, plus a backfill rule for 1380 existing rows (`path = normalize_cwd(parse_project_key(key))`, which also flags accurate-by-coincidence paths harmlessly) | `NOT NULL` dropped in the D1 rebuild, plus backfill to NULL by the same rule |
| Reader impact | none. `path` has no reader (`recent_chats.py` discards it). An invented path is visible only through `name` | none today. A future reader could mark the path | none today. `recent_chats.py:41` selects and discards `p.path`, so NULL reaches no output. `name` must still come from the decoded path for `--project` and exclusion |
| Honesty of stored data | invented paths are stored as if real | invented paths are stored but flagged | unknown paths are stored as unknown |
| Exclusion check (`hooks/import_conversations.py:313-321`) | unchanged (uses this call's lossy flag) | could switch to the stored flag (more precise), or stay unchanged | unchanged |
| Matches the research-stage answer | no | yes | no |

**Recommendation:** A, because once D1 removes the constraint the marker has no correctness job, and with no reader of `path` there is nothing for it to improve. B and C both add migration backfill for no observable gain today.
**Pick B instead if** you want `ccrecall status` or a later display to say "path unknown" for invented paths, or want the exclusion check to use stored state.
**Pick C instead if** you want the stored data to be honest about unknown paths. After the comb's correction, C costs only the `NOT NULL` drop and a backfill, with no output change.
**Reversibility:** easy. A marker can be added later with the same backfill rule.
**Ratified:** Chose A (no marker) over a `path_is_lossy` flag or a nullable path, to keep the migration and code minimal. `path` has no reader, and D1 removes any correctness need for a marker. This accepts that about 57 invented paths are stored as if real. It was re-ratified after the comb corrected the reader premise, and it supersedes the research-stage "insert with lossy flag" answer.

### D3: What happens when importing one project raises

Today an exception escaping `import_project` (e.g. from `upsert_project`, which runs outside the per-file SAVEPOINT containment) propagates through `_run`'s loop (`hooks/import_conversations.py:531-545`). It is logged by the top-level backstop (`:428-439`) without naming the project, and re-raised. Every project that sorts after it is skipped, on every run, and an interactive `ccrecall import` shows a raw traceback.

**Deciding factor:** one bad project must not block every other project's import.

| | A: leave as is | B: name the project, still abort | C: contain per project |
|---|---|---|---|
| Other projects still import | no | no | yes. Roll back that project's transaction, continue the loop |
| The log names the culprit | no | yes (key and dir in `extra=`) | yes (`log.exception` with key and dir) |
| Interactive output | raw traceback | raw traceback with project context | one line per failed project plus a count in the final summary; the run exits non-zero if any project failed |
| A systematic bug (e.g. a schema problem) | one failure | one failure | one log entry per project. Noisy, but the summary count makes it obvious |
| Size of change | none | small | small: try/except around `import_project` and `conn.commit()` in both `_run` branches (`:513-520`, `:531-545`) |
| Relation to #170's per-file containment | n/a | n/a | same pattern, one level up |
| Infrastructure failures (`sqlite3.OperationalError`: disk full, locked, corrupt) | abort | abort | still abort. The handler catches only non-`OperationalError` exceptions, matching `import_project`'s own split (`hooks/import_conversations.py:354-367`) |

**Handler contract (sketch-time challenge, Finding 2):**
- On a caught failure, including a failed `conn.commit()`, call `conn.rollback()` before continuing. `import_project` opens its explicit `BEGIN` only `if not conn.in_transaction` (`hooks/import_conversations.py:304-305`), so without the rollback the next project would join the failed project's transaction.
- The rollback discards the whole project's per-file work and its `import_log` rows together, so the project is retried on the next run.
- An `OperationalError` rolls back and propagates.
- The same handler applies in both `_run` branches: the single project and the loop.
- **Visibility (Finding 3):** the per-project line, the summary count, and the non-zero exit serve interactive `ccrecall import` only.
  - The routine import is spawned detached with output to DEVNULL, on most SessionStarts (`memory_setup.py:81-92`, `:154-157`). Nothing reads its exit code.
  - For a detached run, the only signal is `log.exception` in `ccrecall-import.log`, naming the key and dir.
  - Surfacing failed projects through a `health.py` sidecar belongs to the separate CLI-wide error-messaging issue.

**Recommendation:** C, because the #221 incident's real cost was that 3 days of imports skipped every project after personal-apps, and per-project containment turns that into one project missing plus a named error.
**Pick B instead if** you want any import failure to stop the run, so it can't go unnoticed in a detached process.
**Pick A instead if** you would rather do all error-surfacing work in the separate CLI-wide issue.
**Reversibility:** easy.
**Ratified:** Chose C, per-project containment of non-`OperationalError` exceptions with an explicit rollback, over naming-and-aborting or leaving it. One bad project can't block every other project's import, and infrastructure failures still stop the run. This accepts one log entry per project when a logic bug is systematic.

### D4: Which transcripts count as a source of an accurate path

This comes from the sketch-time challenge (Findings 1, 6, 7). Two writers feed the "accurate path replaces" rule:
- The Stop-hook sync passes the current session's first cwd (`session_ops.py:104-107`, `parsing.py:223-224`).
- The import probe passes the first cwd of a discovered file.

Discovery includes `subagents/*.jsonl` (`transcript_sources.py:260-283`). Ordering by mtime can put a subagent file first. That file's first cwd can be a `cd`'d subdirectory, which changes `name` and, through it, the `exclude_projects` match (`hooks/import_conversations.py:313-321`) and the `--project` filter (`search_query.py:77`).

**Deciding factor:** one source rule shared by both writers, instead of per-case patches to the write path.

| | A: top-level session transcripts only; last writer wins | B: A, plus rank the writers (import's newest beats sync) | C: keep subagent files as probe candidates |
|---|---|---|---|
| Subagent subdirectory paths stored | impossible | impossible | possible (rare) |
| `exclude_projects` bypass through the probe | closed | closed | open (rare) |
| Writers use the same kind of source | yes | yes | no |
| Residual drift | resuming a session launched from a pre-move location writes the old path until the next import | none | as A, plus subagent drift |
| Code added | a filter on probe candidates | a filter plus a stored source/timestamp column | none |

**Recommendation:** A, because a one-line candidate filter closes the privacy-relevant case and leaves only display-level drift that heals on its own.
**Pick B instead if** the resumed-pre-move drift shows up in practice. **Pick C instead if** the subagent case is shown not to occur.
**Reversibility:** easy.
**Probe rule details:**
- Order the top-level session transcripts by mtime, newest first, with filename as the tiebreak for determinism.
- Return the first cwd found.
- Each file is read by streaming lines and stops at the first entry that has a cwd. It is never fully materialized.
- A file that raises logs the existing WARNING, and the walk continues to the next-older file.
- The walk has no cap. It falls back to lossy only if no file yields a cwd.

The streaming and per-file failure rules come from the sketch-time challenge, Finding 4. Real data shows the newest-file rule fixes personal-apps and the 1 dir where `files[0]` lacked a cwd, with no regressions (research brief, "Probe strategies, simulated").

**Ratified:** Chose A over ranking writers or keeping subagent candidates. The probe considers only top-level session transcripts (newest by mtime, filename tiebreak, first cwd), and sync keeps using the current session's first cwd, so the path has one source rule and the exclusion bypass is closed. This accepts that resuming a session launched from a repo's pre-move location rewrites `path`/`name` (with an INFO line) until the next import restores the newest path.

## Assumed

- **Inherited ratifications.** The user ratified the following while answering the research brief's open questions, before this ledger existed. They are inherited here rather than redrafted as decisions:
  - "the stored path tracks the newest accurate cwd" (below)
  - "existing data is left alone" (below)
  - option C itself, which D1 restates with its table

  Evidence: `design/research/2026-10-20-issue-221-project-path-identity/research.md`, Options and Open questions.
- **Identity is the normalized key, which is unique and stable.** It is always derived from the transcript dir name. Evidence: `schema.py:22` (`key TEXT UNIQUE NOT NULL`), `hooks/import_conversations.py:307`, `session_ops.py:106`, `hooks/session_selection.py:259`.
- **Nothing reads `projects.path`.** `recent_chats.py:41` selects it and discards it (`:77`, `:92`). The `--path` search filter uses `sessions.cwd` (`search_query.py:78-80`). The path matters only through `name` (`formatting.py:109-111`).
- **The stored path tracks the newest accurate cwd (inherited).** An accurate path that differs from the stored one replaces it, even if the stored one is also accurate. This is how a moved repo heals. D4 defines where an accurate path may come from.
- **Existing data is left alone (inherited).** No orphan deletion, no key repair. Evidence: the research brief's Open questions.
- **A table rebuild must run inside the version gate after `prepare`.** Rebuilding reparses the schema, including the `chunks_vec_ad` trigger, which references vec0. Evidence: `db_base.py:323-333`, #121, #136.
- **The rebuild preserves `projects.id`.** `sessions.project_id` is the only FK to `projects` (`schema.py:32`). The ladder turns `foreign_keys` off during migrations (`db_base.py:317`, `:345`). The rebuild must copy `id`, `path`, `key`, `name`, `created_at` verbatim and recreate `idx_projects_key`.
- **Rebuild order (sketch-time challenge, Finding 8).** The rebuild follows the v2 shape (`db_base.py:143-201`):
  1. `CREATE TABLE projects_new`.
  2. `INSERT ... SELECT` with explicit columns.
  3. `DROP TABLE projects`.
  4. `ALTER TABLE projects_new RENAME TO projects`.
  5. Recreate `idx_projects_key`.

  Never rename `projects` away. Since SQLite 3.26, a rename rewrites the FK in `sessions` to follow the renamed table.

  Tests must pin two things across the matrix: `PRAGMA foreign_key_list(sessions)` still targets `projects`, and `PRAGMA foreign_key_check` returns no rows. No runtime FK check runs inside the migration. A pre-existing violation would otherwise make the DB unopenable under fail-loud.
- **The v0–v7 migration matrix must extend to v8.** `_seed_genuine_db` raises for any version above 7 (`tests/test_db.py:728-732`). With `SCHEMA_VERSION = 9`, `range(SCHEMA_VERSION)` includes 8, so a genuine v8 seed (the v7 shape plus `chunks.cap_tokens`) is required. The post-migration assertions must pin that `projects.path` is no longer unique and that project rows and ids survive.
- **The fresh-install path is pinned too (sketch-time challenge, Finding 9).** `SCHEMA_CORE` already creates the new `projects` shape at `user_version = 0`, and v9 then rebuilds it. A brand-new DB opened through `get_connection` must satisfy four checks:
  - `user_version` is 9.
  - `projects` has no UNIQUE index on `path`.
  - `idx_projects_key` exists.
  - Two rows with the same `path` and different keys both insert.
- **The lossy flag returned by `upsert_project` keeps its meaning:** "this call derived a lossy path." The exclusion check depends on that. Evidence: `hooks/import_conversations.py:313-321`, #135.
- **Logging per `rules/common/logging.md`:**
  - INFO when an existing row's path changes, with `extra=` key, old path, new path.
  - Nothing for a skipped lossy write (routine on every import).
  - The existing WARNING for probe parse failures stays.
- **CLAUDE.md invariant 4's `SCHEMA_VERSION` narrative must gain the v9 entry.** Evidence: CLAUDE.md "Four invariants to preserve", item 4.

## Build

- [x] Implementation and tests committed
- [x] Docs — CLAUDE.md invariant 4 gains the v9 entry
- [ ] Ship-time challenge

**Calls made during the build:**
- The probe also skips top-level `agent-*.jsonl` and dot-prefixed files: `agent-*` at the top level is the legacy subagent transcript layout, which D4 excludes for the same reason as `subagents/`. The import loop already skips dot-prefixed files.
- `_migrate_to_v9` isn't injectable through `_apply_migrations` the way v1/v2 are: no test needs to swap it, and the partial-failure test already pins the shared transaction.
- Containment lives in a new `import_project_contained` (import plus commit) shared by both `_run` branches. Because the commit moved inside it, the loop's debug timing line reports `import+commit` together rather than splitting them.
- `_run` returns repair failures plus project failures as one count, and `run` exits 1 when that count is non-zero. Only the exit code reads that count.
- A probe candidate that vanishes between discovery and its `stat` is dropped with a WARNING, the same as a file that fails to read. It doesn't fail the project.
- `sqlite3.DatabaseError` (e.g. SQLITE_CORRUPT) other than `OperationalError` is contained per project, not aborted, to match `import_project`'s existing per-file split. The run still exits non-zero.
- Verified on a copy of the real DB: v8→v9 kept all 1382 project rows (identical) and 6147 sessions, the FK still targets `projects`, and there are 0 FK violations. A full `ccrecall import` against the copy exited 0, and otf-api and personal-apps resolved to their real paths.
