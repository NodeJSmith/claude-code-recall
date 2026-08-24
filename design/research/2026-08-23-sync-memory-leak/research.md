---
proposal: "Investigate and fix unbounded memory growth in sync-current that consumed 7.5 GB and froze the machine"
date: 2026-08-23
status: Draft
flexibility: Exploring
motivation: "Preventive hardening after sync-current consumed 7.5 GB (3.3 GB RSS + 4.4 GB swap), triggered kernel memory throttling, and froze the WSL2 machine for 35+ minutes"
constraints: "ccrecall project (Python, sqlite3, fastembed for embeddings). sync-current runs as a SessionStop hook. DB was 577 MB at time of incident. Machine is WSL2 with 24 GB RAM."
non-goals: "Changing the embedding model, breaking vector compatibility (would force full re-embed)"
depth: deep
---

# Research Brief: sync-current Unbounded Memory Growth (Issue #162)

**Initiated by**: Issue #162 -- sync-current leaked memory unboundedly (7.5 GB observed, froze machine)

## Context

### What prompted this

The `sync-current` Stop-hook process consumed 7.5 GB (3.3 GB RSS + 4.4 GB swap), triggered kernel `__mem_cgroup_handle_over_high` throttling, and froze the entire WSL2 machine for 35+ minutes. The process also opened a SQLite temp file during operation. The DB was 577 MB at the time. The input transcript file was deleted before investigation, so the exact trigger is not reproducible -- but the code paths that could produce this spike are all identifiable from the source.

### Current state

`sync-current` (`hooks/sync_current.py:run()`) is a SessionStop hook that syncs a single session's transcript into the conversations DB. It calls `sync_session()` (`session_ops.py`), which:

1. Reads the entire JSONL transcript into memory: `all_entries = list(parse_all_with_uuids(filepath))` (line 83)
2. Filters to messages, extracts branches
3. Inserts new messages, updates tool_content
4. For each branch (typically one), calls `sync_branch()` which:
   - Recomputes aggregated content for FTS (full DB re-fetch)
   - Computes context summary (second full DB re-fetch)
   - Fetches all branch messages for embedding (third full DB re-fetch)
   - Builds exchange pairs and embeds up to 8 new/changed chunks

None of these intermediate structures are freed before the next step begins. The function holds the full parsed transcript, three independent DB re-fetches of the same branch content, exchange pair reconstructions, and then fires onnxruntime inference -- all within a single call stack with no memory reclamation.

### Key constraints

- The embedding model (jina-embeddings-v2-small-en) uses unfused ALiBi attention in its ONNX export, producing quadratic seq x seq attention matrices
- `MODEL_TOKEN_LIMIT = 8192` is the cap; lowering it would change vectors and force a full re-embed
- `enable_cpu_mem_arena=False` is already set, so onnxruntime returns transient workspace to the OS after each `model.embed()` call
- The backfill path already has `reclaim_memory()` (gc.collect + malloc_trim) between batches; the sync-current path does not
- `MAX_WRITE_PATH_EMBEDS_PER_SYNC = 8` caps the number of inference calls, not the per-call token length

## Feasibility Analysis

### Materialization inventory

Every point where data is pulled into memory during a sync-current run, ordered by call depth. "Session-cumulative" means it scales with total session history, not the incremental delta.

| # | Location | What | How | Scaling | Worst-case size |
|---|----------|------|-----|---------|----------------|
| 1 | `session_ops.py:83` | `all_entries` | `list(parse_all_with_uuids(filepath))` -- full JSONL materialized | File size x 4-8 (Python object overhead) | 400-800 MB for 100 MB transcript |
| 2 | `session_ops.py:91-95` | `messages` | List comprehension filter of `all_entries` | Reference-sharing, negligible extra | Same dicts as #1 |
| 3 | `session_ops.py:118-122` | `existing_uuids` | `cursor.fetchall()` -- UUID strings only | Linear in session message count | Small (36 bytes/UUID) |
| 4 | `session_ops.py:128-132` | `uuid_to_msg_id` | `cursor.fetchall()` -- int+UUID pairs | Linear in session message count | Small |
| 5 | `message_ops.py:124-149` | `update_missing_tool_content` | Iterates ALL messages, calls `extract_text_content` per message | O(full transcript) per sync | Transient per-message, but wasted CPU |
| 6 | `message_ops.py:91-121` | `insert_new_messages` | Iterates ALL messages, calls `extract_text_content` again | O(full transcript) per sync | Transient per-message, but wasted CPU |
| 7 | `parsing.py:334-353` | `aggregate_branch_content` | `SELECT content, tool_content ... fetchall()` + `"\n".join()` | Session-cumulative, full re-fetch | 0.3-0.5x raw file size |
| 8 | `parsing.py:356-377` | `build_aggregated_content` | `"".join(parts)` on top of #7 | Session-cumulative | 0.3-0.5x raw file size |
| 9 | `summarizer.py:418-430` | `compute_context_summary` | Second independent full SELECT + fetchall | Session-cumulative | 0.3-0.5x raw file size |
| 10 | `summarizer.py:115-171` | `build_exchange_pairs` (for summary) | Builds exchange dicts with joined assistant strings | Session-cumulative | 0.3-0.5x raw file size |
| 11 | `db_vec.py:102-119` | `fetch_branch_messages` | Third independent full SELECT + fetchall | Session-cumulative | 0.3-0.5x raw file size |
| 12 | `embed_ops.py:216` | `build_exchange_pairs` (for embedding) | Second call, rebuilds all exchanges | Session-cumulative | 0.3-0.5x raw file size |
| 13 | `embed_ops.py:72-99` | `_prepare_exchange_data` | Builds capped text + hash for ALL exchanges (before diffing) | Session-cumulative | Bounded per-exchange (32K chars) |
| 14 | `embeddings.py:239-265` | `embed_batch` -> `model.embed()` | onnxruntime inference, unfused ALiBi attention | Quadratic in token count | **~8.4 GB for one 8192-token text** |

Items #1, #7-12 are simultaneously alive during `sync_branch`. Items #5-6 are transient per-message but scan the entire transcript on every sync (not just new messages).

### Peak memory waterfall

At the point where `embed_batch` fires (item #14), the following are still live in the call stack:

- `all_entries` (#1): 400-800 MB for a 100 MB transcript
- `messages` (#2): reference-sharing with #1
- `agg_content` (#8): ~30-50 MB for 100 MB transcript (text-only, no dict overhead)
- `embed_msgs` (#11): ~30-50 MB
- `exchanges` (#12): ~30-50 MB
- `exchange_data` (#13): ~30-50 MB (all exchanges, not just the 8 being embedded)
- Then `model.embed()` (#14): up to ~8.4 GB transient

**Total peak for a 100 MB transcript with one near-8192-token exchange: ~9-10 GB.**

For a smaller transcript (say 20-30 MB) with one near-max-length exchange, the dominant term is still the ~8.4 GB onnxruntime spike, which matches the observed 7.5 GB closely.

### Root cause analysis

Two independent mechanisms contribute, with very different scaling:

**Primary: onnxruntime attention workspace (~8.4 GB peak)**

The code already documents this risk (`embeddings.py:64-84`). The ONNX export of jina-v2-small's ALiBi attention is unfused, so onnxruntime materializes the full seq x seq score matrix per head, plus softmax/workspace copies at ~5x the raw tensor. Measured peaks: ~1 GB at ~2.2k tokens, ~4 GB at ~4.5k tokens, ~8.4 GB at the 8192-token cap. This is the strongest single explanation for the observed 7.5 GB figure.

`MAX_WRITE_PATH_EMBEDS_PER_SYNC = 8` caps the number of inference calls but does nothing to prevent any single call from hitting the full ~8.4 GB peak. Even one new exchange with a long conversation turn (a large tool output, a full file read, a long assistant response) can trigger it.

Confidence: **Supported** -- measured peaks documented in the source code comments match the observed incident size. No direct runtime profiling of the incident exists (the transcript was deleted), but the mechanism is concrete and the numbers align.

**Secondary: transcript data amplification (~5-9.5x file size)**

The full JSONL transcript is materialized into Python dicts (`all_entries`), never freed, and then three independent full-branch DB re-fetches (`aggregate_branch_content`, `compute_context_summary`, `fetch_branch_messages`) each re-materialize essentially the same content. `build_exchange_pairs` is called twice (once for summary, once for embedding), each time rebuilding exchange dicts for the entire branch. `_prepare_exchange_data` processes all exchanges before diffing against existing chunks.

For a 100 MB transcript, this chain produces ~500 MB - 1 GB of resident Python objects before embedding even starts. This is not large enough alone to explain 7.5 GB, but it compounds with the onnxruntime spike.

Confidence: **Inferred** -- the amplification factor (4-8x for parsed JSON, plus redundant copies) is a structural estimate from Python's known per-object overhead, not a `tracemalloc` measurement. The 4-8x range is consistent with published measurements of JSON-to-Python-dict expansion for nested structures with many small string keys.

**Not the cause: SQLite itself**

No `cache_size`, `mmap_size`, or `temp_store` overrides are set. SQLite uses its compiled-in defaults (a few MB of page cache). The temp file observed during the incident is consistent with bounded FTS5 write-path spill (the `branches_au` trigger re-tokenizing a large `aggregated_content` string) or an index sort -- both mechanisms where SQLite spills to disk specifically to keep its own memory bounded.

Confidence: **Direct** -- verified by grep across all source files; no memory-expanding PRAGMAs are set.

### What already supports fixing this

- `reclaim_memory()` in `subprocess_utils.py` already implements the gc.collect + malloc_trim pattern, used by both `backfill_embeddings` and `import_conversations`. The sync-current path just doesn't call it.
- `enable_cpu_mem_arena=False` is already set on the model, so onnxruntime returns transient workspace to the OS after each call. The problem is that the ~8.4 GB peak of a single call is itself too large for the machine.
- `cap_for_embedding()` already caps per-exchange text to 8192 tokens. The issue is that 8192 tokens is still enough to trigger the quadratic attention spike.
- `MAX_WRITE_PATH_EMBEDS_PER_SYNC = 8` already bounds inference call count.
- The backfill path already has a PID-file concurrency guard (only one backfill at a time), and sync-current has its own (`PID_KEY = "ccrecall-sync-current"`).

### What works against fixing this

- Lowering `MODEL_TOKEN_LIMIT` below 8192 would change vector outputs and force a full re-embed of all existing chunks -- a breaking change to the embedding contract.
- The three redundant full-branch DB re-fetches in `sync_branch` serve three different consumers (`aggregate_branch_content`, `compute_context_summary`, `fetch_branch_messages`) that each expect slightly different row shapes. Unifying them requires interface changes.
- `_prepare_exchange_data` processes all exchanges (not just new ones) because it needs to compute content hashes to diff against existing chunks. Moving the diff earlier requires restructuring the exchange-to-chunk mapping.
- The `all_entries` list is held for the entire `sync_session` call because `messages` (a filtered view) is passed to `sync_branch`, which needs the content for metadata computation. Streaming would require rethinking the pipeline's data flow.

## Options Evaluated

### Option A: Sync-path token cap (reduce single-call peak)

**How it works**: Introduce a `SYNC_PATH_TOKEN_LIMIT` lower than `MODEL_TOKEN_LIMIT` (e.g., 4096 or 2048 tokens) that applies only to the write-path embedding in `embed_branch_chunks`. The backfill path continues to use the full 8192-token cap. Exchange texts embedded on the sync path are capped tighter, producing slightly different (shorter-context) vectors that the backfill later replaces with full-length vectors when it runs.

This is the most direct fix for the dominant ~8.4 GB spike. At 4096 tokens, the measured peak drops to ~4 GB; at 2048 tokens, ~1 GB.

**Pros**:
- Directly addresses the dominant contributor (onnxruntime attention peak)
- No re-embed needed -- backfill naturally upgrades the shorter vectors
- Small, localized change (one constant + one cap call in `embed_branch_chunks`)
- The existing `_diff_exchanges` mechanism already detects content-hash changes, so the backfill will pick up the re-embed

**Cons**:
- Sync-path vectors are lower quality until backfill runs (shorter context window means less semantic information)
- Adds a second token cap constant to reason about
- The backfill may never run on some installations (it's opt-in), leaving permanently shorter vectors

**Effort estimate**: Small -- one new constant, one conditional cap in `embed_branch_chunks`

**Dependencies**: None

### Option B: Skip sync-path embedding entirely (defer all embedding to backfill)

**How it works**: Set `embed=False` (or equivalent) in the sync-current path, so `sync_branch` skips `embed_branch_chunks` entirely. All embedding is deferred to the background backfill. The sync path still does message insertion, branch metadata, aggregated content, and summary computation.

**Pros**:
- Eliminates the entire ~8.4 GB onnxruntime spike from the sync-current process
- Simplifies the sync path (no embedding model loaded, no fastembed/onnxruntime imported)
- The sync-current process description already says "fast and lightweight" (line 1)
- Fastest possible sync hook

**Cons**:
- New sessions have no vector embeddings until backfill runs, degrading semantic search for recent conversations
- The backfill is opt-in (`ccrecall backfill embeddings`) -- users who don't run it get no vectors at all
- This is a regression in the "embed-on-write" design that currently provides immediate searchability

**Effort estimate**: Small -- pass `embed=False` or skip the `fetch_branch_messages` + `embed_branch_chunks` call in `sync_branch` when called from the sync path

**Dependencies**: Would need to make backfill auto-scheduled (not opt-in) to avoid a searchability regression

### Option C: Memory circuit breaker + reclaim + de-duplicate fetches

**How it works**: A multi-pronged approach:

1. **Add `reclaim_memory()` calls** between the major phases of `sync_branch` (after `build_aggregated_content`, after `write_branch_summary`, before `embed_branch_chunks`). The pattern already exists in `backfill_embeddings` and `import_conversations`.

2. **De-duplicate the three full-branch DB re-fetches**: `aggregate_branch_content`, `compute_context_summary`, and `fetch_branch_messages` each independently SELECT the entire branch's message content. Fetch once and pass the result to all three consumers.

3. **Free `all_entries` before `sync_branch`**: After message insertion and uuid_to_msg_id construction, `all_entries` is no longer needed. Explicitly `del all_entries` (or restructure so it goes out of scope) before the branch loop.

4. **Limit `_prepare_exchange_data` to candidates only**: Currently it processes all exchanges before the diff. Restructure to compute content hashes first (cheap -- just the text + SHA-256), diff against existing chunks, then only run `cap_for_embedding` on the exchanges that actually need embedding.

5. **Memory circuit breaker**: Check `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` before calling `embed_batch`. If RSS already exceeds a threshold (e.g., 2 GB), skip embedding for this sync and leave it for backfill.

**Pros**:
- Addresses both the data amplification (items 1-4) and the onnxruntime spike (item 5)
- Preserves embed-on-write for normal-sized sessions
- Makes the sync path genuinely lighter, not just capped
- The de-duplication also reduces CPU waste (three full SELECT queries become one)

**Cons**:
- Largest scope of change across multiple files
- The circuit breaker (item 5) introduces a new behavioral mode (conditional embedding skip) that needs testing
- De-duplicating the fetches (item 2) requires interface changes to `aggregate_branch_content`, `compute_context_summary`, and `embed_branch_chunks`
- The circuit breaker threshold needs tuning per-machine (24 GB WSL vs. 15 GB VPS vs. 32 GB RHYME)

**Effort estimate**: Medium -- touches `session_ops.py`, `branch_ops.py`, `parsing.py`, `embed_ops.py`, `summarizer.py`

**Dependencies**: None new; uses existing `reclaim_memory` from `subprocess_utils`

### Option D: Simplest possible fix (sync-path token cap only)

**How it works**: The absolute minimum change. Add one line in `embed_branch_chunks` that re-caps exchange texts to a lower token limit (e.g., 4096) when called from the sync path. Pass a `max_tokens` parameter from `sync_branch` to `embed_branch_chunks`.

This is Option A stripped to its essence -- no de-duplication, no reclaim_memory, no circuit breaker. Just cap the single dominant term.

**Pros**:
- One parameter, one cap call, done
- Directly targets the ~8.4 GB peak (the only component large enough to explain 7.5 GB)
- Minimal risk of regressions
- The backfill naturally upgrades vectors later

**Cons**:
- Does not address the secondary amplification (redundant fetches, held `all_entries`)
- The secondary amplification could still cause issues for truly enormous transcripts (hundreds of MB)

**Effort estimate**: Small -- ~10 lines changed

**Dependencies**: None

## Concerns

### Technical risks

- **Token cap changes vector quality**: A 4096-token cap drops the bottom half of context for long exchanges. For typical coding conversations, the most recent content (kept by the head+tail cap strategy in `cap_for_embedding`) is the most semantically relevant, so quality loss may be minimal in practice -- but this is an assertion, not a measurement. Verifying it requires comparing search recall before/after on real queries.
- **Circuit breaker threshold portability**: If Option C's RSS-based circuit breaker is used, the threshold must work across machines with 15-32 GB RAM. `resource.getrusage` returns RSS in KB on Linux; the threshold should be a fraction of available memory, not an absolute number.

### Complexity risks

- **Two token caps**: Options A/D introduce a second token limit that interacts with the existing `MODEL_TOKEN_LIMIT`. The relationship (sync-path vectors are "draft quality," backfill produces "final quality") needs to be documented and tested.
- **Conditional embedding skip**: Option C's circuit breaker adds a new behavioral branch (skip embedding when memory is high) that could mask bugs if the threshold is set too aggressively.

### Maintenance risks

- **Redundant fetch debt remains**: Options A/D leave the three redundant full-branch SELECT queries in place. Each grows with session size and produces wasted I/O + CPU on every sync. This is technical debt that compounds as usage grows.
- **`_prepare_exchange_data` waste**: Processing all exchanges (not just new ones) before diffing is wasted work that grows linearly with session history. This affects sync latency even when no embedding fires.

## Open Questions

- [ ] What was the actual transcript file size that triggered the incident? (Deleted, not recoverable -- but knowing whether it was 10 MB or 200 MB would distinguish between "onnxruntime spike alone" and "amplification + spike.")
- [ ] Has the backfill ever been run on this installation? If not, every sync-current call embeds new exchanges, making the onnxruntime spike a regular event, not a one-off.
- [ ] Should the sync-path token cap be configurable via settings, or is a hardcoded 4096 sufficient? The "right" cap depends on the machine's RAM.
- [ ] Is there an appetite to make backfill auto-scheduled (e.g., a periodic systemd timer) rather than opt-in? This would make Option B viable without a searchability regression.
- [ ] Should `_prepare_exchange_data` be restructured to compute hashes first and only cap/tokenize the exchanges that actually need embedding? This is independent of the memory fix but would reduce per-sync CPU waste.

## Recommendation

**Option A (sync-path token cap) is the right first step.** It directly addresses the dominant contributor (~8.4 GB onnxruntime spike) with a small, low-risk change. The numbers are clear: the observed 7.5 GB aligns closely with the documented ~8.4 GB peak at 8192 tokens, and a 4096-token cap drops this to ~4 GB -- well within the machine's 24 GB budget even with the secondary amplification on top.

Option C's de-duplication and reclaim_memory additions are valuable follow-up work (the three redundant full-branch SELECT queries are genuine waste that grows with session size), but they are not urgent for preventing the freeze -- the secondary amplification alone cannot reach 7.5 GB without a very large transcript (200+ MB).

Confidence in this recommendation: **Supported**. The dominant mechanism (onnxruntime attention quadratic in token count) is explicitly documented in the source with measured peaks that match the incident. The secondary mechanism (data amplification) is structurally traced but not profiled, and its contribution is estimated at 500 MB - 1 GB for a 100 MB transcript -- significant but not the 7.5 GB driver.

### Suggested next steps

1. Implement Option A: add a `SYNC_PATH_TOKEN_LIMIT = 4096` constant and apply it in `embed_branch_chunks` when called from the sync path. This is small enough to go straight to implementation without a design doc.
2. Add `reclaim_memory()` calls between phases of `sync_branch` (low-effort, high-value hardening borrowed from the backfill path).
3. File a follow-up issue for Option C items 2-4 (de-duplicate fetches, free `all_entries` early, limit `_prepare_exchange_data` scope) as technical debt reduction. These improve sync latency and reduce memory baseline but are not urgent for preventing the freeze.
4. Consider adding a `tracemalloc`-based smoke test that runs `sync_session` against a synthetic large transcript and asserts peak RSS stays below a threshold -- this would catch regressions.

## Sources

- [ONNX Runtime Memory Optimization](https://www.mintlify.com/microsoft/onnxruntime/performance/memory-optimization)
- [Processing large JSON files in Python without running out of memory](https://pythonspeed.com/articles/json-memory-streaming/)
- [Python resource limitation (RLIMIT_AS)](https://luminousmen.com/post/python-resource-limitation/)
- [jina-embeddings-v2-small-en model card](https://huggingface.co/jinaai/jina-embeddings-v2-small-en)
- [Jina Embeddings 2: 8192-Token General-Purpose Text Embeddings](https://arxiv.org/html/2310.19923v3)
