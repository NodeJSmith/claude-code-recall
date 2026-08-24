# Design: Sync-Current Memory Fix

**Date:** 2026-08-23
**Status:** archived
**Scope-mode:** hold
**Research:** design/research/2026-08-23-sync-memory-leak/research.md

## Problem

The `sync-current` SessionStop hook can consume ~8.4 GB of memory from a single onnxruntime inference call, freezing the machine for 35+ minutes. The root cause is jina-v2-small's unfused ALiBi attention in its ONNX export, which materializes a quadratic seq x seq score matrix peaking at ~8.4 GB for one 8192-token text. A 7.5 GB incident (3.3 GB RSS + 4.4 GB swap) triggered kernel `__mem_cgroup_handle_over_high` throttling on a 24 GB WSL2 machine.

Users have no visibility into this risk: no logging on the embedding hot path, no alert when embeddings are degraded, and no guidance on how to avoid or mitigate the spike.

## Goals

- Drop the sync-current worst-case memory peak from ~8.4 GB to ~4 GB by capping sync-path embedding at 4096 tokens and scaling the batch attention budget to the sync-path cap.
- Preserve semantic search quality — embeddings still work at 4096 tokens; backfill is optional, not required.
- Surface draft-quality embedding state to users via the existing SessionStart alert framework, with guided backfill setup and schedule-marker suppression.
- Add observability to the embedding hot path so a recurrence is diagnosable from logs alone.
- Reduce memory baseline marginally by freeing the `all_entries` list container and non-message entries before the branch loop, and adding `reclaim_memory()` between sync_branch phases. (Note: `messages` shares dict references with `all_entries`, so the bulk of the transcript data stays resident via `messages` — `del all_entries` frees only the list container and entries not in `messages`, such as notifications.)

## Non-Goals

- De-duplicating the three redundant full-branch DB SELECT queries (deferred as tech-debt follow-up).
- RSS-based circuit breaker (needs live-RSS API, not `ru_maxrss` — separate issue).
- `RLIMIT_AS` hard ceiling on sync-current (defense-in-depth hardening, separate issue).
- Memoizing `build_exchange_pairs` (optimization, not memory fix).
- Changing the embedding model or breaking vector compatibility.

## User Scenarios

### User: ccrecall user upgrading to this version
- **Goal:** Continue using semantic search without machine freezes
- **Context:** User upgrades ccrecall via `pip install --upgrade ccrecall` or plugin update

#### First session after upgrade

1. **Starts a Claude Code session**
   - Sees: SessionStart context includes a new alert if any branches have draft-quality (4096-capped) embeddings
   - Decides: Whether to set up scheduled backfill now or skip
   - Then: If they ask Claude to help set up a timer, Claude guides them through cron/systemd/launchd setup (platform-appropriate); a schedule marker is written to suppress the alert

#### Skips backfill setup

1. **Continues using ccrecall normally**
   - Sees: Semantic search works; long exchanges have slightly less context in their vectors
   - Decides: Nothing — search quality for typical queries is unaffected
   - Then: Alert stays suppressed (snoozed for 24h per existing mechanism)

#### Sets up scheduled backfill

1. **Asks Claude to help set up a backfill timer**
   - Sees: Step-by-step guidance for their platform (cron, systemd, or launchd on macOS)
   - Decides: Timer interval (e.g., daily)
   - Then: Schedule marker written to `~/.ccrecall/backfill-schedule.json`; alert permanently suppressed while marker exists
2. **Backfill runs on schedule**
   - Sees: `ccrecall backfill embeddings --status` shows progress
   - Decides: Nothing — automatic
   - Then: Draft-quality chunks are upgraded to 8192-token vectors over time; converges to no-op

## Functional Requirements

- **FR#1** `content_hash` in `_prepare_exchange_data` is derived from the raw, uncapped combined exchange text, not from the post-`cap_for_embedding` output.
- **FR#2** `cap_for_embedding` accepts an optional `max_tokens` parameter; when provided, it caps to that limit instead of `MODEL_TOKEN_LIMIT`.
- **FR#3** When called from the sync path, `embed_branch_chunks` caps exchange texts to `SYNC_PATH_TOKEN_LIMIT` (default 4096, configurable via `sync_path_token_limit` in settings).
- **FR#4** When called from the backfill path, `embed_branch_chunks` caps exchange texts to `MODEL_TOKEN_LIMIT` (8192), unchanged from today.
- **FR#5** Each embedded chunk stores the token limit that was used in a `cap_tokens` column in the `chunks` table — but only when `cap_for_embedding` actually truncated the text (`was_capped = True`). When the text fit within the cap without truncation, `cap_tokens` is `NULL` (indistinguishable from pre-migration rows, both meaning "full quality at this cap").
- **FR#6** `_diff_exchanges` flags a chunk for re-embedding when `cap_tokens < MODEL_TOKEN_LIMIT`, even if `content_hash` matches — this is how backfill detects draft-quality chunks needing upgrade.
- **FR#7** A new alert class `ALERT_DRAFT_QUALITY_VECTORS` fires at SessionStart when the DB contains chunks with `cap_tokens IS NOT NULL AND cap_tokens < MODEL_TOKEN_LIMIT` and the schedule marker either does not exist or does not contain a `configured_at` or `dismissed_at` field.
- **FR#8** The alert is suppressed when a schedule marker file exists at `~/.ccrecall/backfill-schedule.json` and contains either a `configured_at` or `dismissed_at` field.
- **FR#9** `session_ops.sync_session` frees the `all_entries` list before entering the branch loop. (`messages` is retained — it is passed to `sync_branch` for branch metadata computation.)
- **FR#10** `branch_ops.sync_branch` calls `reclaim_memory()` between its major phases (after aggregated content, after summary, before embedding).
- **FR#11** `embed_batch` logs the batch size and longest token count at DEBUG before each `model.embed()` call. `embed_branch_chunks` logs a WARNING when an exchange exceeds the caller's cap limit before capping (this is path-aware — the sync path warns above 4096, backfill warns above 8192).
- **FR#12** `sync_current.run()` logs the transcript `file_size` at INFO on every sync.
- **FR#13** `embed_batch` accepts a `max_token_cap` parameter and computes the batch attention budget as `max_token_cap²` instead of the module constant `EMBED_BATCH_ATTENTION_BUDGET`. On the sync path this limits batches to 1 text at 4096 tokens; on backfill, the existing `MODEL_TOKEN_LIMIT²` budget is preserved.
- **FR#14** `_should_stamp_watermark` does not stamp the branch's `embedding_version` watermark if any chunk has `cap_tokens IS NOT NULL AND cap_tokens < MODEL_TOKEN_LIMIT`. (Chunks with `cap_tokens IS NULL` are full-quality — either pre-migration or not truncated — and do not block the watermark.)
- **FR#15** `build_selection` in `backfill_query.py` selects branches that have any chunk with `cap_tokens < MODEL_TOKEN_LIMIT`, in addition to the existing version/model criteria.
- **FR#16** `branch_embedding_coverage` and `compute_caveat` distinguish three states: "not embedded" (no chunks at all), "embedded, draft quality" (chunks exist but `cap_tokens IS NOT NULL AND cap_tokens < MODEL_TOKEN_LIMIT`), and "embedded, full quality" (chunks exist with `cap_tokens IS NULL` or `cap_tokens >= MODEL_TOKEN_LIMIT`). The caveat prose reflects the actual state rather than treating draft-quality as "not embedded."
- **FR#17** `ccrecall backfill embeddings --dismiss` writes a `dismissed_at` field to the schedule marker sidecar, permanently silencing the alert without requiring a scheduled job.
- **FR#18** A new `ccrecall backfill schedule` subcommand with three commands: `write` (writes the schedule marker with `configured_at` timestamp), `clear` (removes the schedule marker, re-enabling the alert), and `status` (shows whether a marker exists, when it was written, whether it's a schedule or dismiss, and how many draft-quality chunks remain).

## Edge Cases

- **Exchange shorter than `SYNC_PATH_TOKEN_LIMIT`**: Embedded at its actual token count; no capping occurs. `cap_tokens` is `NULL` because `cap_for_embedding` returned `was_capped = False`. Only exchanges that were actually truncated get a non-NULL `cap_tokens`. This prevents short exchanges from being flagged as draft-quality or queued for backfill.
- **Backfill re-embeds a draft chunk (4096-8192 token exchange)**: Backfill sees `cap_tokens = 4096 < MODEL_TOKEN_LIMIT`, re-embeds at 8192. Since the text fits within 8192 without truncation, `was_capped = False` → `cap_tokens = NULL`. Next sync: hash matches, `cap_tokens IS NULL` (full quality) — skips. No ping-pong.
- **Backfill re-embeds a draft chunk (>8192 token exchange)**: Same detection, but re-embedding at 8192 still truncates → `was_capped = True`, `cap_tokens = 8192`. Next sync: hash matches, `cap_tokens = 8192 >= SYNC_PATH_TOKEN_LIMIT` — skips. No ping-pong.
- **Existing chunks with no `cap_tokens` column (pre-migration)**: `cap_tokens` is `NULL` for old rows. Backfill treats `NULL` as `MODEL_TOKEN_LIMIT` for backward compatibility — old chunks are assumed full-quality and not re-embedded unless their `content_hash` changes.
- **Schedule marker exists but backfill never runs**: Alert stays suppressed. The user explicitly chose this setup and owns the outcome. No staleness check in v1.
- **Schedule marker file is corrupted or unreadable**: Treat as absent — alert fires. Same defensive pattern as `embedding-status.json` reads.
- **Schedule marker file exists but contains neither `configured_at` nor `dismissed_at`**: Treat as absent — alert fires. An empty `{}` or unrelated JSON does not suppress. Per FR#7/FR#8, suppression requires a recognized field.
- **`sync_path_token_limit` set above `MODEL_TOKEN_LIMIT` in config**: Clamp to `MODEL_TOKEN_LIMIT` — the sync path should never cap higher than the model supports.
- **Multiple branches in one sync**: Each branch independently gets `reclaim_memory()` between phases. The `del all_entries` happens once before the loop, not per-branch.
- **User dismisses the alert**: `ccrecall backfill embeddings --dismiss` writes a `dismissed_at` field to the marker sidecar. Alert stays silent. User can undo with `ccrecall backfill schedule clear`.
- **Exchange exceeds 32K char budget but is under 4096 tokens**: `cap_for_embedding` truncates on char budget → `was_capped = True` → `cap_tokens = 4096` even though the token cap wasn't the limiting factor. This makes the alert/backfill population a superset of "actually token-cap-limited" exchanges. Low real-world impact — text over 32K chars but under 4096 tokens is unusual (mostly whitespace or repetitive formatting).
- **Sync and backfill race on the same exchange**: Before this design both wrote 8192-cap vectors, so the race was harmless. After this design, sync writes 4096 and backfill writes 8192 — last writer wins. If sync overwrites a backfill result, the next backfill pass re-upgrades it. Self-healing with one wasted inference call at worst. Accepted as consistent with how the hash-transition case is handled.

## Operational Lifecycle

The backfill lifecycle is unchanged from today:

- **Run completion**: A backfill run is complete when zero chunks have `cap_tokens < MODEL_TOKEN_LIMIT` (or `embedding_version < EMBEDDING_VERSION`). The scheduled job becomes a no-op.
- **Failure eligibility**: A chunk that fails embedding is eligible again on the next run (existing behavior).
- **Retry bounds**: PID-file guard prevents concurrent runs (existing `try_acquire_pid_file`). No max-attempts cap — a chunk stays eligible until it succeeds.
- **User-visible accounting**: `ccrecall backfill embeddings --status` reports progress (existing).
- **Convergence**: After all draft-quality chunks are upgraded, the scheduled job runs but embeds nothing.
- **Schedule marker**: Written to `~/.ccrecall/backfill-schedule.json` when the user sets up a timer. Read by the alert to suppress nagging. Contains `{"configured_at": "<ISO timestamp>"}`.

These lifecycle behaviors are expressed in FR#6 (upgrade detection), FR#7 (alert), and FR#8 (suppression).

## Acceptance Criteria

- **AC#1** (FR#1) A test constructs two exchange texts that differ only past the 4096-token mark — `_prepare_exchange_data` produces *different* `content_hash` values for each, because the hash is derived from the full raw text (not the capped output). A second pair with identical raw text but different cap limits produces the *same* hash.
- **AC#2** (FR#3, FR#4) A test calls `embed_branch_chunks` with `sync_path_token_limit=4096` and verifies the embedded text is capped at 4096 tokens. A second call with default (`MODEL_TOKEN_LIMIT`) verifies 8192-token cap.
- **AC#3** (FR#5, FR#6) A test embeds a chunk at `cap_tokens = 4096`, then calls `_diff_exchanges` with `target_cap = MODEL_TOKEN_LIMIT` (backfill context) — the chunk is flagged for re-embedding despite matching `content_hash`. After re-embedding at 8192, calling `_diff_exchanges` with `target_cap = SYNC_PATH_TOKEN_LIMIT` (sync context) does NOT flag it — hash matches and `cap_tokens >= SYNC_PATH_TOKEN_LIMIT`, so the sync path never downgrades a backfill-quality vector.
- **AC#4** (FR#1, FR#5, FR#6) **No-ping-pong integration test**: embed an exchange at sync-path cap (4096), run backfill to upgrade it (8192), then run sync-path embedding again — the chunk is NOT re-embedded (hash matches, `cap_tokens >= SYNC_PATH_TOKEN_LIMIT`).
- **AC#5** (FR#7, FR#8) A test inserts chunks with `cap_tokens = 4096`, verifies the alert fires. Then writes a schedule marker file, verifies the alert does not fire. Then removes the marker, verifies the alert fires again.
- **AC#6** (FR#9) `session_ops.sync_session` does not hold a reference to the parsed JSONL list during the `sync_branch` call — verifiable by inspection or by patching and checking the reference count.
- **AC#7** (FR#11, FR#12) After a sync, the log file contains an INFO line with `file_size` and DEBUG lines with batch size / token counts (when embedding fires).
- **AC#8** (FR#13) A test verifies that `embed_batch` with `max_token_cap=4096` produces batches of at most 1 text when texts are near 4096 tokens (attention budget = `4096²`), compared to the default `MODEL_TOKEN_LIMIT` budget which allows larger batches.
- **AC#9** (FR#14, FR#15) A test embeds a branch's chunks at `cap_tokens = 4096`, confirms `_should_stamp_watermark` does NOT stamp the watermark. Confirms `build_selection` includes this branch as a backfill candidate. After backfill re-embeds at 8192, confirms watermark is stamped and `build_selection` no longer selects it.
- **AC#10** (FR#14) A branch whose freshly-embedded chunks all have `was_capped=False` (i.e., `cap_tokens=NULL`) still gets its watermark stamped — the positive case for FR#14, ensuring untruncated exchanges don't block watermark stamping.
- **AC#11** (FR#6, FR#14) A chunk with `cap_tokens IS NULL` does not raise `TypeError` and is treated as full-quality at each comparison site (`_diff_exchanges`, `_should_stamp_watermark` existing-chunks loop, `_should_stamp_watermark` freshly-embedded shortcut).
- **AC#12** (FR#16) A branch with only draft-quality chunks (`cap_tokens=4096`) shows "draft quality" in `compute_caveat`, not "not embedded."

## Key Constraints

- **Do not lower `MODEL_TOKEN_LIMIT`**: The 8192-token constant is the embedding model's contract. Changing it would alter all vectors and force a full re-embed. `SYNC_PATH_TOKEN_LIMIT` is a separate, sync-path-only constant.
- **Do not import fastembed/onnxruntime on the hook hot path**: The alert in `health.py` and `context_alerts.py` must detect draft-quality vectors via a DB query, not by loading the embedding model. This preserves architecture invariant 3 (embedding health is read, never probed, on the hook path).
- **Do not break the `_diff_exchanges` contract for version-stale chunks**: Version-stale but content-unchanged chunks remain backfill's job (existing design decision H6). The new `cap_tokens` check is additive — a chunk can be flagged for re-embedding by either a hash mismatch OR a cap-tokens upgrade, but version-stale handling is unchanged.

## Dependencies and Assumptions

- The `cap_for_embedding` head+tail strategy preserves the most semantically relevant content (beginning and end of exchanges). Quality loss from a 4096-token cap is mitigated by this design but not measured — this is an accepted tradeoff, not a verified claim.
- The backfill is opt-in. Users who never run it get permanent 4096-token embeddings for long exchanges. The alert + schedule marker provide visibility but not enforcement.
- The `~4 GB` peak estimate for 4096-token exchanges is based on documented measured peaks in `embeddings.py:64-84`. The quadratic relationship (tokens² ∝ memory) is structural to the model architecture.

## Architecture

### Content hash fix

`_prepare_exchange_data` (`embed_ops.py:72-99`) currently computes `content_hash = hashlib.sha256(text.encode()).hexdigest()` where `text` is the post-`cap_for_embedding` output (line 83-84). Change this to hash the raw `combined` text before capping:

```
combined = f"{user}\n\n{assistant}"
content_hash = hashlib.sha256(combined.encode()).hexdigest()
text, was_capped = cap_for_embedding(combined, max_tokens=cap_limit)
```

This decouples content identity (did the exchange change?) from embedding quality (which cap was used?), preventing the ping-pong bug identified in the challenge review.

### Sync-path token cap

Add `SYNC_PATH_TOKEN_LIMIT = 4096` to `embeddings.py` alongside `MODEL_TOKEN_LIMIT = 8192`. Add an optional `max_tokens` parameter to `cap_for_embedding` that overrides `MODEL_TOKEN_LIMIT` when provided. Thread `sync_path_token_limit` from settings through `sync_branch` → `embed_branch_chunks` → `_prepare_exchange_data` → `cap_for_embedding`.

The backfill path continues to call `cap_for_embedding` without `max_tokens`, defaulting to `MODEL_TOKEN_LIMIT`.

### Sync-path attention budget

`EMBED_BATCH_ATTENTION_BUDGET = MODEL_TOKEN_LIMIT²` is a path-agnostic constant in `embeddings.py`. At 4096-token cap, `_plan_embed_batches` can pack 4 texts per batch (since `4 × 4096² = 8192²`), producing the same ~8.4 GB peak as a single 8192-token text — defeating the cap's purpose. Fix: `embed_branch_chunks` passes the caller's cap limit to `embed_batch`, which computes the batch budget as `cap_limit²` instead of using the module constant. On the sync path this gives `4096² = 16.7M` (vs 67M today), limiting batches to 1 text at 4096 tokens — the worst-case peak is genuinely ~4 GB. The backfill path passes `MODEL_TOKEN_LIMIT`, preserving the existing `8192²` budget.

### Cap-tokens tracking

Add a `cap_tokens INTEGER` column to the `chunks` table via a new migration (`_migrate_to_v8`). This records which token limit was used for each embedding, but only when the text was actually truncated (`was_capped = True` from `cap_for_embedding`). When the text fit within the cap without truncation, `cap_tokens` is `NULL` — the embedding is full-quality at whatever cap was applied, so no upgrade is needed. `_prepare_exchange_data` populates `cap_tokens` conditionally: `cap_limit if was_capped else None`. `_diff_exchanges` adds a condition: re-embed when `cap_tokens < target_cap` even if hash matches, where `target_cap` is the caller's token limit (`SYNC_PATH_TOKEN_LIMIT` on sync, `MODEL_TOKEN_LIMIT` on backfill).

The sync path's `_diff_exchanges` uses `target_cap = SYNC_PATH_TOKEN_LIMIT` — it does not flag chunks that were embedded at a higher cap (8192) for downgrade. Only backfill flags chunks where `cap_tokens IS NOT NULL AND cap_tokens < MODEL_TOKEN_LIMIT`.

**One-time hash-derivation transition for >8192-token exchanges:** The hash change (`hash(raw)` vs old `hash(capped)`) causes a one-time mismatch for exchanges >8192 tokens on the first sync after upgrade. The sync path re-embeds these at 4096, which is a temporary quality reduction from the prior 8192-cap embedding. This is accepted because: (a) exchanges >8192 tokens are extremely rare, (b) FR#14 withholds the watermark (sees `cap_tokens=4096 < MODEL_TOKEN_LIMIT`), (c) FR#15 selects the branch for backfill, and (d) backfill upgrades to 8192 with the new raw-text hash. The self-healing takes one backfill cycle. For users who never run backfill, the downgrade is permanent for these rare exchanges — the alert (FR#7) provides visibility.

Existing rows have `cap_tokens = NULL`. Backfill treats `NULL` as `MODEL_TOKEN_LIMIT` (backward compat — old chunks are assumed full-quality). In Python, substitute `MODEL_TOKEN_LIMIT` for `None` before any `<` comparison to avoid `TypeError`. Extract this substitution into a shared helper (e.g., `effective_cap_tokens(cap_tokens: int | None) -> int`) that returns `MODEL_TOKEN_LIMIT` for `None` — every comparison site calls it, consistent with the "one parse boundary" convention.

### Backfill branch selection and watermark

Two additional changes are required to make backfill actually reach draft-quality chunks:

1. **`_should_stamp_watermark`** (`embed_ops.py`): Currently stamps a branch's `embedding_version` once every chunk has a current `embedding_version` and matching `content_hash`. Two changes: (a) the `existing_chunks` SELECT (`embed_ops.py:226-233`) must include `cap_tokens` in its fetched columns so the data is available; (b) the function's loop currently does `if idx in embedded_indices: continue` for freshly-embedded chunks, bypassing all checks — this shortcut must also check the just-written `cap_tokens` value (available from `_prepare_exchange_data`'s per-exchange return value — `cap_limit if was_capped else None`). The raw caller cap limit must NOT be used — it would treat every untruncated exchange as draft-quality and permanently prevent watermark stamping. Only skip if the per-exchange `cap_tokens` is non-NULL and `< MODEL_TOKEN_LIMIT`. The net effect: do not stamp the watermark if any chunk (existing or freshly embedded) has `cap_tokens < MODEL_TOKEN_LIMIT`. This keeps the branch eligible for backfill re-selection until all chunks are at full quality.

2. **`build_selection`** (`hooks/backfill_query.py`): Currently selects candidate branches purely on `branches.embedding_version`/`embedding_model` plus a chunk_vec heal clause. Add a clause: also select branches that have any chunk with `cap_tokens IS NOT NULL AND cap_tokens < MODEL_TOKEN_LIMIT`, even if the branch's `embedding_version` watermark is current. This ensures backfill revisits branches whose watermark was stamped before the `cap_tokens` check was added (or was never stamped because the sync path correctly withheld it).

Together, these ensure that `embed_branch_chunks` → `_diff_exchanges` (where FR#6's `cap_tokens < target_cap` check lives) is actually invoked for branches with draft-quality chunks during backfill.

### Draft-quality alert

Add `ALERT_DRAFT_QUALITY_VECTORS` to `health.py:_ALERT_PROSE`:

```python
ALERT_DRAFT_QUALITY_VECTORS: (
    "Some conversations have draft-quality embeddings — semantic search works but may miss context from long exchanges.",
    "the sync path caps embeddings at 4096 tokens for memory safety; full 8192-token quality requires a scheduled backfill",
    "run `ccrecall backfill embeddings` to upgrade existing drafts; to prevent recurrence, set up a scheduled job (`ccrecall backfill schedule write`) or dismiss permanently (`ccrecall backfill embeddings --dismiss`)",
),
```

Wire into `context_alerts.py:proactive_alert_block()`:
1. Query `SELECT COUNT(*) FROM chunks WHERE cap_tokens IS NOT NULL AND cap_tokens < ?` with the model token limit. The limit value (8192) must NOT be imported from `embeddings.py` — that module pulls in numpy/fastembed. Instead, define `FULL_QUALITY_TOKEN_LIMIT = 8192` in `health.py` (which is already guarded to import none of vec/fastembed/onnxruntime per invariant 3) and use it here. This is a deliberate duplication of the value — the alternative (importing from `embeddings.py`) would violate the hot-path import constraint.
2. If count > 0, check for schedule marker at `~/.ccrecall/backfill-schedule.json`.
3. If marker exists and is valid JSON, suppress the alert.
4. Otherwise, add `ALERT_DRAFT_QUALITY_VECTORS` to active keys.

This preserves invariant 3: the check is a DB query + file read, not a model load.

### Schedule marker

A JSON sidecar at `~/.ccrecall/backfill-schedule.json` following the existing `embedding-status.json` and `alert-snooze.json` pattern. Written by the user (or by Claude guiding the user through setup). Minimal schema: `{"configured_at": "<ISO timestamp>"}`. The alert checks for file existence and valid JSON; any parse failure is treated as absent.

Written via `ccrecall backfill schedule write` (FR#18) after the user configures their timer, or by `ccrecall backfill embeddings --dismiss` (FR#17) to permanently decline. Claude calls the CLI command after guiding the user through timer setup, giving both scripted and manual users a supported path. `ccrecall backfill schedule clear` removes the marker, re-enabling the alert. `ccrecall backfill schedule status` shows the current state.

### Config setting

Add `sync_path_token_limit` to `DEFAULT_SETTINGS` in `config.py` with default `4096`. Loaded from `~/.ccrecall/config.json` via the existing `load_settings()` merge. Clamped to `[1, MODEL_TOKEN_LIMIT]` at the consumption site (`branch_ops.py` or `embed_ops.py`), not in `config.py` — `config.py` must remain free of fastembed/onnxruntime imports to preserve hook hot-path performance (architecture invariant 2).

### Memory cleanup

- `session_ops.sync_session`: After `insert_new_messages` and `update_missing_tool_content` complete, `del all_entries` before the branch loop. `all_entries` is the raw parsed JSONL and is no longer needed after message insertion. `messages` (the filtered view) is retained — `sync_branch` uses it for branch metadata computation (`branch_msgs` filtering at `branch_ops.py:204`).
- `branch_ops.sync_branch`: Import and call `reclaim_memory(libc)` between the three major phases (after `build_aggregated_content`, after `write_branch_summary`, before `embed_branch_chunks`). Load `libc` once via `try_load_libc()` at function entry, same pattern as `backfill_embeddings`.

### Hot-path logging

Add `log = logging.getLogger(__name__)` to `embed_ops.py` and `embeddings.py` (if not already present). Specific log points:

- `embeddings.py:embed_batch` — `log.debug("embedding batch", extra={"batch_size": N, "longest_tokens": M})` before each `model.embed()` call.
- `embed_ops.py:embed_branch_chunks` — `log.warning("exchange exceeds cap", extra={"tokens": M, "cap": cap_limit})` when an exchange exceeds the caller's cap limit before capping. This is path-aware: sync path warns above `SYNC_PATH_TOKEN_LIMIT`, backfill warns above `MODEL_TOKEN_LIMIT`.
- `sync_current.py:run()` — `log.info("sync complete", extra={"file_size": file_size, "duration_s": elapsed})` at the end of each sync.

## Implementation Preferences

- Follow the existing 3-tuple pattern in `_ALERT_PROSE` for the new alert.
- Follow the existing `DEFAULT_SETTINGS` dict pattern for the new config key.
- Follow the existing JSON sidecar pattern (`embedding-status.json`) for the schedule marker.
- Use `getLogger(__name__)` for all new logging, per the project's logging conventions.
- Migration follows the `_migrate_to_v4` pattern (additive `ALTER TABLE ADD COLUMN` with duplicate-column guard).

## Replacement Targets

- **`content_hash` derivation in `_prepare_exchange_data`** (`embed_ops.py:84`): The current `hashlib.sha256(text.encode())` (where `text` is post-cap) is replaced with `hashlib.sha256(combined.encode())` (where `combined` is pre-cap). The old derivation is removed outright — there is no incremental migration.
- **Hardcoded `MODEL_TOKEN_LIMIT` in `cap_for_embedding`** (`embeddings.py:292, 309`): The two reference sites gain an optional `max_tokens` parameter. `MODEL_TOKEN_LIMIT` becomes the default, not the only value. The constant itself is unchanged.

## Migration

### Schema migration (v7 → v8)

`_migrate_to_v8` adds `cap_tokens INTEGER` to the `chunks` table:

```python
def _migrate_to_v8(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE chunks ADD COLUMN cap_tokens INTEGER")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunks_cap_tokens "
        "ON chunks(cap_tokens) WHERE cap_tokens IS NOT NULL"
    )
```

Follows the v3/v4 pattern: additive, unconditional (runs outside the version gate with duplicate-column error handling), because the column must exist even when `user_version` is ahead of this code's `SCHEMA_VERSION`.

`SCHEMA_CORE` is updated to include `cap_tokens INTEGER` in the `chunks` table definition and a partial index `CREATE INDEX idx_chunks_cap_tokens ON chunks(cap_tokens) WHERE cap_tokens IS NOT NULL` so fresh installs have both from the start. The migration also creates this index.

`SCHEMA_VERSION` bumps to `8`.

### Existing data

- Existing chunks have `cap_tokens = NULL`. Backfill treats `NULL` as `MODEL_TOKEN_LIMIT` — old chunks are assumed full-quality and not flagged for upgrade unless their `content_hash` changes.
- The `content_hash` change (raw text vs capped text) means existing hashes were computed from capped text. On the next sync for each branch, `_prepare_exchange_data` computes new hashes from raw text. For exchanges shorter than 8192 tokens, `raw == capped`, so hashes match — no re-embed. For exchanges longer than 8192 tokens, hashes differ — the sync path re-embeds at 4096 (a temporary quality reduction from the prior 8192-cap embedding). This is self-healing: FR#14 withholds the watermark, FR#15 selects the branch for backfill, and backfill upgrades to 8192 with the new raw-text hash. See Architecture § Cap-tokens tracking for the full transition flow.
- The `was_capped` boolean column is retained in the schema but is no longer written or read — it is a dead column. `cap_tokens IS NOT NULL` is the live equivalent for detecting truncation.

## Convention Examples

### Alert prose definition

**Source:** `src/ccrecall/health.py:301-316`

```python
_ALERT_PROSE: dict[str, tuple[str, str, str]] = {
    ALERT_TOOL_CONTENT_INCOMPLETE: (
        "ccrecall's tool-content index is incomplete — tool_use content from older sessions is not yet searchable.",
        "sessions synced before tool-content extraction was added have not been backfilled",
        "run `ccrecall backfill tool-content` to index historical tool_use content (one-time, opt-in)",
    ),
}
```

### Additive column migration

**Source:** `src/ccrecall/db_base.py:214-223`

```python
def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """Version-4 migration: add tool_content column and eligibility index to messages."""
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN tool_content TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise
```

### Settings default

**Source:** `src/ccrecall/config.py:42-49`

```python
DEFAULT_SETTINGS = {
    "auto_inject_context": True,
    "max_context_sessions": 2,
    "alert_snooze_hours": 24,
}
```

### Content hash derivation (BEFORE — being replaced)

**Source:** `src/ccrecall/embed_ops.py:82-84`

```python
combined = f"{user}\n\n{assistant}"
text, was_capped = cap_for_embedding(combined)
content_hash = hashlib.sha256(text.encode()).hexdigest()  # hash of CAPPED text — the bug
```

**DON'T:** Hash the post-cap text. This couples content identity to the cap tier, causing sync/backfill ping-pong when different callers use different caps.

**DO:** Hash `combined` (pre-cap) so content identity is stable across cap tiers.

## Alternatives Considered

### Option B: Skip sync-path embedding entirely, defer to backfill

Eliminates the onnxruntime spike entirely from sync-current. Rejected because it requires backfill scheduling for any embeddings to exist — a PyPI package cannot assume users will set up cron/systemd, and users who don't would have zero semantic search.

### No cap, just `reclaim_memory()` and `del all_entries`

The secondary amplification (redundant fetches, held transcript) contributes ~500 MB - 1 GB for a 100 MB transcript. This alone cannot explain the 7.5 GB incident. The dominant term is the ~8.4 GB onnxruntime attention workspace, which `reclaim_memory()` cannot prevent — it's a single-call transient, not an accumulation across calls.

### Lower cap (2048 tokens)

Would drop the peak to ~1 GB — very safe. Rejected as overly aggressive: 4096 tokens is already 4-10x larger than industry standard chunk sizes (400-1024 per LangChain, LlamaIndex, and Weaviate conventions). The head+tail strategy in `cap_for_embedding` preserves both ends of long exchanges, so quality loss at 4096 is modest. Going to 2048 would visibly degrade search quality for exchanges in the 2048-4096 range with no proportional safety benefit.

## Test Strategy

### Required Test Types

Unit tests (pytest) for the hash fix, cap parameterization, `_diff_exchanges` upgrade detection, alert logic, config setting, and migration. Integration test for the no-ping-pong invariant across sync + backfill. All tests use the existing pytest infrastructure with `conftest.py` fixtures.

### Existing Tests to Adapt

- `tests/test_embeddings.py` — tests for `cap_for_embedding` need updating to cover the new `max_tokens` parameter.
- `tests/test_backfill_embeddings.py` — backfill tests need to verify `cap_tokens`-based upgrade detection.
- `tests/test_session_ops.py` — tests for `sync_session` may need updating if the `del all_entries` changes the function's signature or internal flow.
- `tests/test_db.py` — schema tests need to include the new `cap_tokens` column and `SCHEMA_VERSION = 8`.

### New Test Coverage

- **FR#1** — `content_hash` from raw text: unit test in `test_embeddings.py` or a new `test_embed_ops.py`.
- **FR#2** — `cap_for_embedding(max_tokens=)`: unit test in `test_embeddings.py`.
- **FR#3, FR#4** — sync vs backfill cap behavior: unit test in `test_embed_ops.py`.
- **FR#5, FR#6** — `cap_tokens` storage and `_diff_exchanges` upgrade detection: unit test in `test_embed_ops.py`.
- **FR#7, FR#8** — alert + schedule marker: unit test in `test_health.py` and `test_context_alerts.py`.
- **AC#4** — no-ping-pong integration test: new test in `test_integration.py`.
- **FR#10** — `reclaim_memory()` between phases: unit test in `test_branch_ops.py` (verify calls are made between phases).
- **FR#13** — dynamic attention budget: unit test in `test_embeddings.py`.
- **FR#14, FR#15** — watermark and branch selection: unit/integration test in `test_backfill_embeddings.py`.
- **FR#16** — three-state `compute_caveat`/`branch_embedding_coverage`: unit test in `test_db_vec.py` and `test_search_conversations.py`.
- **FR#17** — `--dismiss` flag writes `dismissed_at` and suppresses alert: unit test in `test_health.py` and CLI test in `test_cli.py`.
- **FR#18** — `ccrecall backfill schedule write/clear/status`: CLI tests in `test_cli.py`.
- **FR#7/health** — a test-only cross-check that `health.FULL_QUALITY_TOKEN_LIMIT == embeddings.MODEL_TOKEN_LIMIT` to catch drift between the deliberately duplicated constants.

### Tests to Remove

No tests to remove.

## Smoke Test

**Surface:** CLI + logs.

**Scenario:** Run `ccrecall backfill embeddings --status` after syncing a session with a long exchange (>4096 tokens). The status output should show chunks with `cap_tokens = 4096` eligible for upgrade. After running `ccrecall backfill embeddings`, those chunks should show `cap_tokens = 8192`. The sync-current log file (`~/.ccrecall/ccrecall-sync.log`) should contain an INFO line with `file_size` and, if embedding fired, DEBUG lines with batch size and token counts.

**Success:** Status shows draft-quality chunks before backfill, full-quality after. Log entries exist with the expected fields. No machine freeze or memory pressure during sync.

## Documentation Updates

- **CLAUDE.md**: Update the `embed_ops.py` architecture notes to document `SYNC_PATH_TOKEN_LIMIT`, `cap_tokens` column, and the raw-text hash change. Update `SCHEMA_VERSION` references to 8. Add `cap_tokens` to the chunks table description.
- **CHANGELOG**: Entry for the memory fix, new alert, and configurable sync-path token limit.

## Impact

### Changed Files

- **modify** `src/ccrecall/schema.py` — add `cap_tokens INTEGER` to `SCHEMA_CORE` chunks table, add partial index `idx_chunks_cap_tokens`
- **modify** `src/ccrecall/db_base.py` — bump `SCHEMA_VERSION` to 8, add `_migrate_to_v8`
- **modify** `src/ccrecall/embeddings.py` — add `SYNC_PATH_TOKEN_LIMIT` constant, add `max_tokens` param to `cap_for_embedding`, add `max_token_cap` param to `embed_batch` and `_plan_embed_batches` for dynamic attention budget, add logging to `embed_batch`
- **modify** `src/ccrecall/embed_ops.py` — change `content_hash` to use raw text, store `cap_tokens`, update `_diff_exchanges` for cap-tokens upgrade detection, add logging
- **modify** `src/ccrecall/health.py` — add `ALERT_DRAFT_QUALITY_VECTORS` constant and prose, add schedule-marker check
- **modify** `src/ccrecall/hooks/context_alerts.py` — wire draft-quality alert with DB query and schedule-marker read
- **modify** `src/ccrecall/config.py` — add `sync_path_token_limit` to `DEFAULT_SETTINGS`
- **modify** `src/ccrecall/session_ops.py` — `del all_entries` before branch loop (`messages` retained for `sync_branch`), thread `sync_path_token_limit` from settings to `sync_branch`
- **modify** `src/ccrecall/branch_ops.py` — add `reclaim_memory()` between phases, thread `sync_path_token_limit` to `embed_branch_chunks`
- **modify** `src/ccrecall/hooks/sync_current.py` — log `file_size`, pass settings for `sync_path_token_limit`
- **modify** `src/ccrecall/hooks/backfill_embeddings.py` — pass `MODEL_TOKEN_LIMIT` as the cap limit to `embed_branch_chunks` (which threads it to `embed_batch` as `max_token_cap`)
- **modify** `src/ccrecall/hooks/backfill_query.py` — add `cap_tokens < MODEL_TOKEN_LIMIT` clause to `build_selection` for draft-quality upgrade candidates
- **modify** `src/ccrecall/db_vec.py` — update `branch_embedding_coverage` for three-state distinction (not embedded / draft quality / full quality)
- **modify** `src/ccrecall/search_conversations.py` — update `compute_caveat` prose to reflect draft-quality vs not-embedded
- **modify** `src/ccrecall/status.py` — adapt `branch_embedding_coverage` call site (line 115) for three-state return type
- **modify** `src/ccrecall/search_cli.py` — adapt `branch_embedding_coverage` call site (line 150) for three-state return type
- **modify** `src/ccrecall/cli/commands.py` — add `schedule` subcommand group (`write`/`clear`/`status`) under `backfill_app`, add `--dismiss` flag to `backfill embeddings`
- **modify** `tests/test_embeddings.py` — adapt `cap_for_embedding` tests for `max_tokens` param
- **modify** `tests/test_backfill_embeddings.py` — adapt for `cap_tokens` upgrade detection
- **modify** `tests/test_session_ops.py` — adapt for `del all_entries` change
- **modify** `tests/test_db.py` — add schema v8 and `cap_tokens` column tests
- **create** or modify `tests/test_embed_ops.py` — new tests for hash fix, cap-tokens tracking, no-ping-pong
- **modify** `tests/test_health.py` — add tests for draft-quality alert and schedule-marker suppression
- **modify** `tests/test_context_alerts.py` — add tests for draft-quality alert wiring
- **create** or modify `tests/test_cli.py` — tests for `backfill schedule write/clear/status` and `backfill embeddings --dismiss`
- **modify** `tests/test_db_vec.py` — add tests for three-state `branch_embedding_coverage`
- **modify** `tests/test_search_conversations.py` — add tests for three-state `compute_caveat`

<!-- Gap check 2026-08-23: 2 gaps included — status.py:115 (branch_embedding_coverage caller) → T04 Focus, search_cli.py:150 (branch_embedding_coverage caller) → T04 Focus -->

### Behavioral Invariants

- `MODEL_TOKEN_LIMIT = 8192` is unchanged — backfill continues to produce 8192-token vectors.
- Existing chunks with `cap_tokens = NULL` are treated as full-quality — no unnecessary re-embedding.
- `EMBEDDING_VERSION` is unchanged — this is not a model change, just a cap-tier change.
- The `was_capped` boolean column is retained in the schema but is no longer written or read — `cap_tokens IS NOT NULL` is the live equivalent.
- Hook hot-path performance: the alert check is a DB query + file read, not a model load (invariant 3).
- `MAX_WRITE_PATH_EMBEDS_PER_SYNC = 8` continues to bound the number of inference calls per sync.

### Blast Radius

- **Search quality**: Slightly reduced for exchanges >4096 tokens that are actually truncated on the sync path, until backfill runs. Unaffected for shorter exchanges (the vast majority). For the rare pre-migration >8192-token exchanges, the hash-derivation change triggers a one-time re-embed at 4096 — a temporary downgrade, self-healing via backfill.
- **Backfill**: Gains a new upgrade trigger (`cap_tokens IS NOT NULL AND cap_tokens < MODEL_TOKEN_LIMIT`) alongside the existing `embedding_version` check. Additive.
- **Existing vectors**: Unchanged for exchanges <=8192 tokens (hash matches, no re-embed). For exchanges >8192 tokens, re-embedded at 4096 on first sync (temporary downgrade), then upgraded to 8192 by backfill.
- **Recall caveat**: Updated to distinguish "draft quality" from "not embedded" — users see accurate coverage information instead of a misleading "not embedded" message for branches that have real vectors at 4096 tokens.
- **Alert ordering**: Alert display ordering is not controlled — `active_keys` is a `set`, and adding a fourth alert class increases the chance of multi-alert sessions. Not addressed in this design.

## Known Observability Gaps

**sync_branch embedding failure is a dark operation.** `branch_ops.sync_branch`'s
broad `except Exception` around `embed_branch_chunks` logs and swallows any
exception without calling `record_embedding_failure`. If the embedding write
path regresses (a bug in `_prepare_exchange_data`, `_diff_exchanges`, or
`_should_stamp_watermark`), `sync-current` still calls `clear_embedding_failure()`
(gated only on vec availability), actively erasing prior alert state. The
`ALERT_DRAFT_QUALITY_VECTORS` alert also doesn't catch this — it requires an
existing chunk row with `cap_tokens < ceiling`, but a branch whose embed call
fails before writing any chunk is invisible to it. Net effect: a regression in
the embedding write path could silently disable embedding for every new session
with neither alert firing. Mitigated partially by the separate issue for
reason-scoped sidecar clearing (Finding 3); a rolling-failure-count mechanism in
`sync_branch` would close the remaining gap but needs an observed failure to
calibrate the threshold against.

## Open Questions

(none — all questions were resolved during discovery and blind-spot assessment)
