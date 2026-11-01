---
topic: "Handling permanently failing items in incremental indexers"
date: 2026-11-01
status: Draft
---

# Prior Art: Handling Permanently Failing Items in Incremental Indexers

## The Problem

An unattended repair pass will meet sessions it can't fix, and without a record of them it attempts the same sessions on every run. A persisted negative cache stops the repeat work, but it brings its own failure modes: verdicts that describe items nobody tried, outcomes cached as facts when they depend on the clock or environment, diagnostics that under-report, and fixes that never reach suppressed items.

The question is whether comparable projects guard against repeat attempts at all, and how much machinery they use.

## How We Do It Today

The repair path remembers nothing between runs. A session still non-ok after a forced reimport counts as `sessions_unrepairable` (`src/ccrecall/hooks/import_repair.py:253-254`) and is retried on the next `--repair-gaps`. Spec 016 chose this deliberately, and left a persisted marker out of scope "unless `--repair-gaps` moves to automated/repeated invocation" (`design/specs/016-stale-tail-import-repair/design.md:43`), which spec 018's D3 does.

`ingestion_check_cache` holds ok results only. The one "don't retry this" precedent is the embedding backfill's `embedding_version = -1` sentinel, set per branch after a content error (`src/ccrecall/hooks/backfill_embeddings.py:439-443`) and excluded in `src/ccrecall/hooks/backfill_query.py:60`. It guards inference, which is far more expensive than a parse.

## Patterns Found

### Pattern 1: Retry every run, log it, accept the cost

**Used by**: claude-mem's transcript path, rclone, restic, Syncthing (folder-level pause only), Paperless-ngx (manual requeue), basic-memory #1188 for malformed frontmatter.
**How it works**: The source is compared to the destination each run, and an item that still fails is attempted again. Failures surface in the log or exit code, and no per-item failure state exists.
**Strengths**: No schema or invalidation logic, and a fixed bug is picked up on the next run.
**Weaknesses**: Log noise and repeated cost, which matters when a retry is expensive and barely matters for a parse of tens of milliseconds.
**Example**: https://github.com/restic/restic/issues/3098 ; https://forum.syncthing.net/t/retries-after-fail/20785

### Pattern 2: Persisted skip-list with an explicit re-arm

**Used by**: Recoll, basic-memory #364 (current status on `main` not verified).
**How it works**: A failure is recorded after an attempt, and the item is skipped until its identity changes or someone asks for a retry. Recoll justifies this as a performance optimization because some failures are costly.
**Strengths**: Stops repeated expensive failures.
**Weaknesses**: The re-arm is only as good as its signals. Recoll's manual calls its own retry heuristic "a rather lame attempt", and an importer fix doesn't change the source file.
**Example**: https://www.recoll.org/usermanual/webhelp/docs/ch02s01s04.html ; https://github.com/basicmachines-co/basic-memory/pull/364

### Pattern 3: Permanent verdict for deterministic outcomes, expiry for the rest

**Used by**: obra/episodic-memory (#91 permanent empty sentinel, #96 errored sentinel retried after an hour).
**How it works**: Outcomes that follow from file content are cached permanently, and others get a timestamped marker. All readers go through one helper.
**Strengths**: Handles environment-dependent failures with no manual step.
**Weaknesses**: Needs reliable failure classification, and `verify`, `stats`, `search` and the indexer all had to learn the new state. The retry it protects is an LLM call.
**Example**: https://github.com/obra/episodic-memory/commit/de11f9098042fdea8c38a53fc85149f700d957aa

### Pattern 4: Cache key includes the processor version

**Used by**: ripgrep-all, PMD.
**How it works**: The tool's version is part of the key, so upgrading it invalidates results.
**Strengths**: A later fix reaches suppressed items with no manual step.
**Weaknesses**: A coarse token invalidates everything on any bump.
**Example**: https://raw.githubusercontent.com/phiresky/ripgrep-all/v0.10.10/src/preproc_cache.rs

### Pattern 5: Attempt counter or dead-letter table

**Used by**: Broker-based systems, and claude-mem before v12.5.0.
**How it works**: A bounded number of attempts, then the item moves aside for inspection.
**Strengths**: Fits ordered queues where one poison item blocks the rest.
**Weaknesses**: claude-mem removed its counter because it "silently dropped data after 3 attempts", and later found 277 rows stuck in a state no cleanup path owned.
**Example**: https://github.com/thedotmack/claude-mem/releases/tag/v12.5.0 ; https://github.com/thedotmack/claude-mem/issues/2373

## Anti-Patterns

- **Silent sentinel for failures.** episodic-memory rejected a plain empty sentinel because it "permanently buries transient failures".
- **Retry counter with a silent give-up.** claude-mem's removal is the documented case.
- **A status column only one reader understands.** Any diagnostic that ignores the new state under-reports, as episodic-memory's consumer updates show.

## Relevance to Us

The projects with this machinery added it after a reported stall or loop, or to avoid costly retries: LLM calls in episodic-memory, large decompressions in Recoll. ccrecall's retry is about 3 parses plus a force-reimport per session, and its repair loop isn't a bounded queue that a failing head can block, because each candidate runs and commits independently. The measured set is 0 under the current walk, with a few possible under the new one.

The negative cache in spec 018 sits at the heavy end of what these projects do, for the cheapest retry in the survey. Recoll's cost justification weakens by its own logic when the retry is this cheap.

## Recommendation

Retry on every import and log the unrepairable count (Pattern 1), which is what spec 016 already does. Revisit if a repair log shows a large unrepairable count. If a cache is later needed, Patterns 2 and 4 together are the shape to build: write after the attempt, and include a processor version in the key.

Coverage caveat: no source was found that defends a negative cache for retries this cheap, which is an absence in these searches and not proof. basic-memory #364's survival on current `main` couldn't be confirmed, and the dead-letter literature assumes ordered queues.

## Sources

These URLs were not live-verified.

### Reference implementations
- https://github.com/obra/episodic-memory/commit/de11f9098042fdea8c38a53fc85149f700d957aa — errored sentinel with timed retry
- https://github.com/obra/episodic-memory/issues/91 — permanent sentinel for zero-exchange files
- https://github.com/thedotmack/claude-mem/releases/tag/v12.5.0 — removal of the retry counter
- https://github.com/thedotmack/claude-mem/issues/2373 — stuck pending rows
- https://github.com/thedotmack/claude-mem/blob/e0249485/src/services/transcripts/watcher.ts — offset-only watcher state
- https://github.com/basicmachines-co/basic-memory/pull/364 — sync circuit breaker
- https://github.com/basicmachines-co/basic-memory/pull/1188 — malformed files stay visible errors
- https://raw.githubusercontent.com/phiresky/ripgrep-all/v0.10.10/src/preproc_cache.rs — adapter version in the cache key

### Issues and forums
- https://github.com/restic/restic/issues/3098 — unreadable files reported each run
- https://github.com/kmwoley/restic-windows-backup/issues/19 — wrapper looping on a permanent error
- https://github.com/ncw/rclone/issues/2291 — retry behaviour
- https://forum.syncthing.net/t/retries-after-fail/20785 — folder-level backoff and log noise
- https://github.com/paperless-ngx/paperless-ngx/discussions/6465 — manual requeue of failed documents

### Documentation
- https://www.recoll.org/usermanual/webhelp/docs/ch02s01s04.html — skipping previously failed files
- https://pmd.github.io/pmd/pmd_userdocs_incremental_analysis.html — cache keyed on tool version
