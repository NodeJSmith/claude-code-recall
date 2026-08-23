# Context: Sync-Current Memory Fix

## Problem & Motivation

The `sync-current` SessionStop hook can consume ~8.4 GB of memory from a single onnxruntime inference call, freezing the machine for 35+ minutes. The root cause is jina-v2-small's unfused ALiBi attention in its ONNX export, which materializes a quadratic seq x seq score matrix peaking at ~8.4 GB for one 8192-token text. Users have no visibility into this risk — no logging on the embedding hot path, no alert when embeddings are degraded, and no guidance on mitigation.

## Visual Artifacts

None.

## Key Decisions

1. **Sync-path token cap (4096)**: Cap sync-path embeddings at 4096 tokens via `SYNC_PATH_TOKEN_LIMIT`. This drops the worst-case peak from ~8.4 GB to ~4 GB. Backfill continues at 8192. Chosen over skip-embedding-entirely (Option B) because that requires backfill scheduling for any embeddings to exist.
2. **Raw-text content hash**: `content_hash` is derived from the raw, uncapped `combined` exchange text — not from the post-`cap_for_embedding` output. This decouples content identity from cap tier, preventing a sync/backfill ping-pong bug.
3. **`cap_tokens` column**: Only stored as non-NULL when `was_capped = True`. NULL means full quality (either pre-migration or untruncated). This keeps short exchanges from being flagged as draft-quality.
4. **Dynamic attention budget**: `embed_batch` accepts `max_token_cap` and computes the batch budget as `max_token_cap²` instead of the module constant. On the sync path, this limits batches to 1 text at 4096 tokens.
5. **Alert + schedule marker + dismiss**: `ALERT_DRAFT_QUALITY_VECTORS` fires at SessionStart. Suppressed by a schedule marker (via `ccrecall backfill schedule write`) or by dismissal (via `ccrecall backfill embeddings --dismiss`).
6. **Three-state caveat**: `compute_caveat` and `branch_embedding_coverage` distinguish "not embedded," "draft quality," and "full quality" — preventing watermark withholding from falsely degrading the coverage metric.
7. **`FULL_QUALITY_TOKEN_LIMIT`**: A deliberately duplicated constant in `health.py` (value 8192) so the alert check never imports `embeddings.py`'s fastembed-heavy stack.

## Constraints & Anti-Patterns

- **Do NOT lower `MODEL_TOKEN_LIMIT`** (8192). It's the embedding model's contract. `SYNC_PATH_TOKEN_LIMIT` is separate.
- **Do NOT import fastembed/onnxruntime on the hook hot path.** Alert detection uses a DB query + file read, never a model load. Invariant 3.
- **Do NOT break `_diff_exchanges` for version-stale chunks.** The `cap_tokens` check is additive.
- **NULL `cap_tokens` means full quality** — pre-migration or untruncated. Substitute `MODEL_TOKEN_LIMIT` for `None` before any `<` comparison (`effective_cap_tokens` helper).
- **The raw caller cap limit must NOT be used in `_should_stamp_watermark`'s freshly-embedded shortcut** — use the per-exchange `was_capped`-gated value (`cap_limit if was_capped else None`).
- **Config clamp lives at the consumption site** (`branch_ops.py` or `embed_ops.py`), not in `config.py`.

## Design Doc References

- `## Architecture` — Content hash fix, sync-path token cap, attention budget, cap-tokens tracking, backfill selection/watermark, alert, schedule marker, config, memory cleanup, logging
- `## Migration` — Schema v7→v8 with additive `cap_tokens INTEGER` column + partial index
- `## Edge Cases` — Short exchanges, backfill re-embed (4096-8192 vs >8192), pre-migration NULL, schedule marker states, char/token conflation, sync/backfill race
- `## Operational Lifecycle` — Unchanged backfill lifecycle with schedule marker
- `## Test Strategy` — Required test types, existing tests to adapt, new coverage

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

**DON'T:** Hash the post-cap text. This couples content identity to the cap tier.

**DO:** Hash `combined` (pre-cap) so content identity is stable across cap tiers.
