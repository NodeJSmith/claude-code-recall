---
task_id: "T01"
title: "Add enrichment schema and light DB access"
status: "planned"
depends_on: []
implements: ["FR#2", "AC#5"]
---

## Summary

Add the additive `branches` columns needed to store LLM enrichment and source fingerprints.
Create an embedding-free database access boundary for the detached LLM worker so its entry point never imports sqlite-vec, fastembed, or onnxruntime.
Preserve the existing migration transaction, rebuild compatibility, and normal `get_connection()` behavior.

## Target Files

- modify: `src/ccrecall/schema.py`
- modify: `src/ccrecall/db.py`
- create: `src/ccrecall/llm_summary_db.py`
- modify: `tests/test_db.py`

## Prompt

Implement the `Data model` and `Migration` sections of `design/specs/008-llm-summary-enrichment/design.md`. Append the seven enrichment/fingerprint columns to `SCHEMA_CORE`'s trailing `branches` layout, bump `SCHEMA_VERSION`, and add an idempotent additive migration that works even when a database reports a newer `user_version`.

Update both historical `branches` rebuild migrations to preserve the new columns. Replace v1's `SELECT *` copy with an explicit old-column projection and defaults/NULLs so genuine pre-v1 databases still upgrade. Recreate indexes/triggers through the existing helper and do not add a speculative enrichment index.

Introduce a narrow `llm_summary_db.py` boundary that lets the direct LLM worker open a migrated, foreign-key/WAL-safe SQLite connection without importing `ccrecall.db`, `sqlite_vec`, `ccrecall.embeddings`, fastembed, or onnxruntime. Factor only shared non-vec connection/migration pieces needed to avoid duplication; preserve the existing public `get_connection()` semantics for every current caller. Keep all imports module-level under repository conventions.

## Focus

`db.py` imports `sqlite_vec` and `ccrecall.embeddings` at module import time, so a worker that calls its `get_connection()` violates AC#7 before it does useful work. The light boundary must not bypass schema initialization or silently fork migration logic. `SCHEMA_CORE` is followed by v1 and v2 table rebuilds on a new install; every rebuild DDL and explicit copy list must retain the appended columns. Use `tests/test_db.py`'s migration fixtures and subprocess import-isolation pattern as the regression seam.

## Verify

- [ ] FR#2: Enrichment and source-fingerprint state is stored only in appended `branches` columns, separate from deterministic summary versioning.
- [ ] FR#2: A clean subprocess opens the migrated lightweight enrichment DB boundary without importing `ccrecall.db`, sqlite-vec, embedding modules, fastembed, or onnxruntime.
- [ ] AC#5: Migration tests cover fresh install, genuine old-schema upgrade, repeated connection idempotence, and preservation of deterministic rows.
