---
topic: "Reprocessing unchanged transcripts after a parser change"
date: 2026-11-01
status: Draft
---

# Prior Art: Reprocessing Unchanged Transcripts After a Parser Change

## The Problem

An indexer that skips unchanged files has a blind spot when its own parsing logic changes: the file is the same, so the skip still fires, and the old result stands. ccrecall hits this with spec 018, whose new walk finds history in transcripts that haven't changed on disk. The question is how comparable projects get already-indexed, unchanged files reprocessed. This brief comes from reading their source, not their changelogs.

## How We Do It Today

`ingestion_check_cache` stores a source fingerprint (file size and mtime) and a DB-coverage fingerprint per session, and a match returns "ok" without parsing (`src/ccrecall/ingestion_status.py:61-75, 190`). Neither includes a parser version, and nothing invalidates the cache when the walk changes.

## Patterns Found

### Pattern 1: A content version in the cache key

**Used by**: daaain/claude-code-log.
**How it works**: The skip check compares mtime, size, a sidecar fingerprint and a `content_version` (`cache.py:620-667`). `content_schema_version()` hashes the cached models' field names plus a manual `_CONTENT_SCHEMA_SALT` (`cache.py:542, 585-617`), and a NULL stored version counts as stale. Commit 120def2 (#320) added it after unchanged transcripts kept reading a new field as NULL, which is the same class of bug as ccrecall's.
**Strengths**: Automatic, and an older build's rows self-correct.
**Weaknesses**: The digest can't see a logic change that keeps the shape, so the salt is bumped by hand. Spec 018's walk change is that kind of change.

### Pattern 2: Don't handle it; offer a manual rebuild

**Used by**: obra/episodic-memory, basic-memory, beaugunderson/obliscence (text re-parse), valpere/session-indexer.
**How it works**: The skip key has no parser version. episodic-memory uses a per-file line high-water mark and re-parses nothing below it (`src/indexer.ts:359-378`), so a parser change needs `index-conversations rebuild`, which deletes the database (`src/index-cli.ts:102-131`). basic-memory needs `bm reindex --full` (`cli/commands/db.py:330-355`). obliscence needs `--force`. session-indexer's answer is to delete the database and re-mine.
**Strengths**: No machinery.
**Weaknesses**: Old data stays wrong until someone acts. session-indexer's migration guide warns that re-mining loses history once Claude Code prunes old transcripts.

### Pattern 3: A schema migration clears or recomputes data

**Used by**: basic-memory, obliscence, claude-mem.
**How it works**: basic-memory's `0042_nfc_file_paths.py` sets checksums to NULL, but only on the rows it merged. obliscence rebuilds embeddings on a `PRAGMA user_version` change (`db.go:67-96`), and not parsed text. claude-mem's migrations recompute derived values from rows already in the database (`SessionStore.ts`), never from transcripts.
**Strengths**: Runs once, automatically, and can tell older installs through the version.
**Weaknesses**: None of these clears a whole skip cache to force a re-parse, so that use has no direct precedent. Each later logic change needs another migration.

### Pattern 4: Keep raw entries and re-derive

**Used by**: marcelocantos/mnemo (commit 671e5f8).
**How it works**: Raw entries are stored, and an idempotent startup backfill re-derives from them.
**Strengths**: Survives pruned transcripts.
**Weaknesses**: A much larger design than this change needs.

### Older install opening a newer store

Separate from reprocessing, and the code differs from what the changelogs suggest:

- **Refuses:** claude-mem throws before touching the database (`src/services/sqlite/connection.ts:35-58`), added after a data-loss incident (#3609). session-indexer refuses on any mismatch.
- **Warns and degrades:** mnemo allows reads and fails writes. ccrecall warns (#228).
- **No guard:** episodic-memory, basic-memory (Alembic's raw error, inferred), obliscence, claude-code-log.

## Relevance to Us

Most peers don't solve this: they leave old data wrong until a manual rebuild. claude-code-log is the one transcript indexer that solves it automatically, and its shape is a version in the cache key, which is ccrecall's option A. Its manual salt is the same discipline A needs, since a walk change must bump the constant by hand.

A clearing migration has a flaw specific to ccrecall. After it runs, an older install sharing the database can write rows with the same fingerprint format, and the newer install trusts them, which re-hides sessions. A version in the key survives that.

## Recommendation

A version constant in the fingerprint (Pattern 1). The older-install warning is a separate concern that a clearing migration would serve only as a side effect.

Coverage caveat: six candidates were read in source, and some claims about behaviour at runtime are inferred, not tested. ccusage, ccglass, csift, cli-continues and cross-code-organizer were not read.

## Sources

These URLs were not live-verified, and file references are from shallow clones on this date.

- https://github.com/daaain/claude-code-log — SQLite cache with `content_version`
- https://github.com/beaugunderson/obliscence — FTS5 plus sqlite-vec with session hooks
- https://github.com/valpere/session-indexer — hooks and semantic search, no migrations
- https://github.com/marcelocantos/mnemo — raw entries with startup backfill
- https://github.com/obra/episodic-memory — line high-water mark and manual rebuild
- https://github.com/basicmachines-co/basic-memory — checksum key and manual full reindex
- https://github.com/thedotmack/claude-mem — hook-driven observations, refuses newer databases
