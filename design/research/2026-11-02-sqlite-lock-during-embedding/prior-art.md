---
topic: "Avoiding a SQLite write transaction during local embedding inference"
date: 2026-11-02
status: Draft
---

# Prior Art: Avoiding a SQLite Write Transaction During Embedding Inference

## The Problem

A local indexer that embeds text with an on-device model can spend seconds to minutes in inference. If a SQLite write transaction is open during that time, every other writer waits out its busy timeout and then fails with `database is locked`. Moving inference out of the transaction opens a gap: the source can change between reading the text and writing the vector, and a crash can land between the two.

## How We Do It Today

`sync-current` runs `sync_session` in one connection context. `sync_branch` writes messages and links, then calls `embed_branch_chunks` inline, so inference runs inside the open transaction, which commits when the context exits. Per-chunk `content_hash` already exists. A branch-level watermark (`branches.embedding_version`) follows a clear-first/set-last protocol. `backfill_embeddings` does the same at a larger scale, committing every 20 branches.

## Patterns Found

### Pattern 1: Infer outside the transaction, short atomic write of vector + marker

**Used by**: episodic-memory (migration path), simonw/llm, mnemosyne (after PR #432), Khoj (Postgres analogue), valpere/session-indexer (per-row form).
**How it works**: Read the text and its identity in a read-only step. Run inference with no write lock. Open a short transaction that writes the vectors and the "embedded" marker together. Batch size sets how many rows share a commit.
**Strengths**: The lock is held for milliseconds. Marker and vector commit atomically, so a crash leaves both or neither and the work repeats.
**Weaknesses**: Nothing in episodic-memory or llm re-verifies the source before writing. They tolerate the gap because the write is an idempotent replace keyed by id or hash. Inference results are lost on a crash.
**Example**: https://github.com/obra/episodic-memory/blob/7e06519357777badd7a115d2014a7ef845904310/src/embedding-migration.ts#L121-L131
**Read**: episodic-memory `src/embedding-migration.ts`, `src/db.ts`, `src/indexer.ts`; llm `llm/embeddings.py`; Khoj `text_to_entries.py`; mnemosyne PR #432 body; session-indexer `main.go`, `db.go`.

### Pattern 2: Content identity instead of change detection

**Used by**: simonw/llm (`content_hash` written with the vector), Khoj (md5 `hashed_value`), obliscence (append-only messages, `INSERT OR IGNORE` by UUID).
**How it works**: The vector carries or is keyed by a hash of its source text. Changed text gets a new hash, so a stale vector can't pass as current. No surveyed project uses a generation counter. episodic-memory and session-indexer only tag the model or version, which detects a model change, not a text change.
**Strengths**: A read/write race becomes harmless. Unchanged chunks are skipped cheaply.
**Weaknesses**: Needs a prune step, because orphan vectors consume KNN top-k slots (obliscence's `pruneOrphanVectors`). A hash alone doesn't say a document is fully embedded.
**Example**: https://github.com/simonw/llm/blob/aebaca27f9acb6cc56ba4b33ab81e08d6dcb3bc3/llm/embeddings.py#L245-L290
**Read**: llm `embeddings.py`; Khoj `text_to_entries.py`; obliscence `index.go`, CLAUDE.md.

### Pattern 3: "Fully embedded" is the absence of pending rows, with no document-level watermark

**Used by**: obliscence (`embedded_messages` anti-join), session-indexer (`LEFT JOIN embeddings`), episodic-memory migration (`embedding_version < N`), llm (hash present).
**How it works**: Completeness is per row, and pending is derived at the start of each run, so crash recovery needs no extra code.
**Strengths**: No clear-first/set-last protocol and no half-state to get stuck in.
**Weaknesses**: The pending query grows with the archive. None handles a partially embedded document whose chunk set later changes; obliscence avoids that because messages are immutable.
**Example**: https://github.com/beaugunderson/obliscence/blob/95c6097eca57967afc841748fd089ec7e19037d6/index.go#L298-L335
**Read**: obliscence `index.go`, `db.go`; session-indexer `db.go`; episodic-memory `embedding-migration.ts`.

### Pattern 4: Concurrent embedders skip on a lock, or tolerate duplicate work

**Used by**: episodic-memory (`proper-lockfile`, loser skips), obliscence, session-indexer, and llm (no lock, idempotent writes).
**How it works**: Either a cross-process lock around the embed run, where losers exit, or no lock and a write that converges. No surveyed project coordinates at row level.
**Strengths**: The lock bounds CPU and memory. The idempotent form needs no lifecycle code.
**Weaknesses**: Idempotent-only doubles inference under contention.
**Example**: https://github.com/obra/episodic-memory/blob/7e06519357777badd7a115d2014a7ef845904310/src/embedding-migration.ts#L88-L100
**Read**: episodic-memory `file-lock.ts`, `embedding-migration.ts`, `sync-cli.ts`; obliscence `index.go`; llm `embeddings.py`; session-indexer `main.go`.

### Pattern 5: Accepted limitation, hold the transaction and bound it by batch size

**Used by**: obliscence, the closest peer.
**How it works**: Inference runs inside a transaction per 100 messages, with WAL and a 5 s busy timeout, the same settings as ccrecall. Nothing in its docs discusses contention. This looks unremarked rather than decided [no source found for an explicit rationale].
**Strengths**: Simple, and vectors and marker are atomic by construction.
**Weaknesses**: Other writers wait out the whole batch.
**Read**: obliscence `index.go` lines 298-440, CLAUDE.md.

## Anti-Patterns

- **Implicit transaction left open across slow calls.** A mnemosyne deployment was wedged for 7+ hours at `busy_timeout=5000`, and the WAL could not checkpoint. Its regression test asserts `conn.in_transaction` is False at each inference call. https://github.com/mnemosyne-oss/mnemosyne/pull/432
- **Orphan vectors left in a vec0 table.** They take KNN top-k slots and silently shrink results. https://github.com/beaugunderson/obliscence/blob/95c6097eca57967afc841748fd089ec7e19037d6/CLAUDE.md
- **Multi-statement insert with no transaction.** episodic-memory's `insertExchange` can leave an exchange with no vector on a crash; no repair for it was found.

## Gaps

- datasette-embeddings could not be cloned; simonw/llm stands in for it.
- LlamaIndex and LangChain SQLite stores were not read. Khoj is the hash-diff example, but it runs on Postgres, so it gives no SQLite locking evidence.
- episodic-memory's crash recovery for an exchange with no vector was not traced.
- "No project re-reads the source or uses a generation counter" rests on the files listed above, not on full histories or issue trackers.

## Relevance to Us

Pattern 1 is the mainstream answer and matches issue #232. ccrecall differs from peers in two ways that matter:

- **It has a document-level watermark.** Peers can tolerate a stale write because nothing claims the whole document is complete. A ccrecall stamp is exactly that claim, so a stamp written from read-time data can be false. Only the stamp needs fresh data; a stale vector is already harmless, because its `content_hash` mismatches on the next diff.
- **Exchanges aren't append-only.** They are indexed by position, and a rewind or a tool-content repair can change their text with or without a link change.

A generation counter would be more machinery than any peer carries. Re-reading the source inside the write transaction is also something no peer does, but only because no peer has a stamp to protect.

## Recommendation

Adopt Pattern 1. Reuse the existing `content_hash` (Pattern 2) as the only identity check, and derive the stamp from data read inside the write transaction. Don't add a cross-process lock; Pattern 4's idempotent form fits, with the existing PID guards bounding same-type concurrency. Take mnemosyne's `conn.in_transaction` assertion as the regression pin.

## Sources

### Reference implementations
- https://github.com/beaugunderson/obliscence/blob/95c6097eca57967afc841748fd089ec7e19037d6/index.go — inference inside a per-batch transaction
- https://github.com/obra/episodic-memory/blob/7e06519357777badd7a115d2014a7ef845904310/src/embedding-migration.ts — embed first, then atomic write
- https://github.com/obra/episodic-memory/blob/7e06519357777badd7a115d2014a7ef845904310/src/file-lock.ts — skip-not-queue process lock
- https://github.com/valpere/session-indexer/blob/374145c728a2d675fceb0a58f2ee01798595d1cf/cmd/session-indexer/main.go — per-chunk autocommit
- https://github.com/simonw/llm/blob/aebaca27f9acb6cc56ba4b33ab81e08d6dcb3bc3/llm/embeddings.py — hash stored with the vector, atomic upsert
- https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/processor/content/text_to_entries.py — hash-keyed chunks (Postgres)
- https://github.com/daaain/claude-code-log — no embeddings; not applicable

### Blog posts & writeups
- https://github.com/mnemosyne-oss/mnemosyne/pull/432 — incident report and fix for the same failure

### Documentation & standards
- https://github.com/llamastack/llama-stack/issues/5344 — sqlite-vec writers without WAL/busy_timeout; background only (body partly read)
- https://www.sqlite.org/lang_transaction.html (not opened)
- https://github.com/asg017/sqlite-vec/issues/40 (not opened)
