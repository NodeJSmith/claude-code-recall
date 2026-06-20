# Changelog

## Unreleased

### Fixed

- Session-start context no longer injects untruncated exchange text on the uncached fallback path. The fallback now builds its summary through the same canonical builder as the cached path, so long exchanges are mid-truncated identically. (#2)
- Summary backfill no longer permanently marks a branch failed on a transient DB error (only genuine content errors get the sentinel), and a sentinel-marked branch is no longer re-selected forever — the error sentinel is excluded from the eligible set, so the documented "avoid infinite retry" guarantee now actually holds. (#2)
- `token_snapshots`/`turn_tool_calls` column migrations use explicit existence checks instead of swallowing `OperationalError`, so a real schema error surfaces instead of being silently ignored. (#2)
- Importing a session whose content all filters out (tool results, notifications, empty text) no longer crashes with `sqlite3.IntegrityError: FOREIGN KEY constraint failed`. `find_all_branches` inserts branch rows before content filtering, so a zero-message session still has children; the `total_messages == 0` cleanup now tears down `branch_messages → branches → sessions` in FK order instead of a bare session delete. Surfaced importing a large transcript set onto a fresh machine. The test fixtures now enable `PRAGMA foreign_keys = ON` to match production, so the existing FK-safe guard tests actually enforce the constraint.

### Added

- Local semantic search fused with FTS via Reciprocal Rank Fusion (RRF). Search results from `cm-search-conversations` now combine keyword ranking (FTS5/FTS4/LIKE) with vector KNN from a locally-running jina-embeddings-v2-small-en model (512-dim, via fastembed). Degrades automatically to keyword-only when the model or sqlite-vec extension is unavailable. (#1)
- New runtime dependencies: `sqlite-vec` (vector KNN), `fastembed` (embedding model + ONNX runtime), `numpy` (vector math). (#1)
- `cm-backfill-embeddings` command: **opt-in** seeding of embeddings for historical active-leaf branches using jina-v2-small-en (via fastembed). Not auto-spawned — seeding the full history is a bounded but non-trivial CPU job, so it only runs when you invoke it. Supports `--days N` (recency window), `--limit N` (cap per run), and `--threads N` (inference threads); resumable. New sessions are covered automatically by embed-on-write. (#1)
- `cm-backfill-embeddings` is now usable unattended (e.g. a systemd timer): `--status [--json]` reports done/eligible/errored/total without embedding (safe to run mid-backfill), progress lines carry an up-front total + elapsed/ETA gated by `--progress-every N`, and every abort path now exits non-zero so a scheduler sees failures. (#382)
- Embedding scoped to **active-leaf branches only** (`is_active = 1`): the search path only returns active leaves, so inactive forks/retries are no longer embedded — ~6.6× less work and a leaner KNN index, with no recall change.
- Embedding throttle: inference runs at one thread by default (raise with `cm-backfill-embeddings --threads N`) and the backfill runs at lowered scheduling priority (`nice`), so it never saturates the machine. (#1)
- `cm-search-conversations --keyword-only`: skip embedding, use keyword search only.
- `cm-search-conversations --status`: print diagnostic info (vec extension loaded, model name, embedded/total branch count) and exit 0. (#1)
- `branch_vec` vec0 virtual table in `conversations.db` storing per-branch 512-dim embeddings. (#1)
