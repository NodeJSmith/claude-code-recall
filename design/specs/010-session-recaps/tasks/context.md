# Context: Session Recaps

## Problem & Motivation

The existing opt-in Branch Resume Brief is continuation-oriented, brittle, and tied to transcript JSONL source checks. The replacement is a concise recognition recap generated only from imported SQLite content. Private evaluation found successful DB recaps equivalent to JSONL recaps on recognition, work arc, and outcome, while Sonnet completed all sampled DB cases and Haiku did not. The implementation must also make SessionEnd finalization durable, globally serialize provider work, prevent duplicate provider processes after crashes, and expose honest recovery/accounting without a daemon.

## Visual Artifacts

None.

## Key Decisions

1. SQLite is the sole recap input authority; recap code never rereads transcript JSONL.
2. Sonnet is the default model; model, budget, and timeout remain one-run overrides.
3. `recap_input_hash` is a canonical hash of the exact ordered DB projection sent to Claude. It is separate from deterministic `summary_source_hash`.
4. Import persists ordered branch membership plus current input hash. Rendering compares stored current and materialized hashes without scanning messages on SessionStart.
5. SessionEnd records durable intent and detaches; one SQLite-backed global drainer serializes all provider work.
6. Fencing tokens protect every transition, but replacement launch additionally requires proof that the prior process group is dead and cleanup is complete.
7. Provider-wide failures use shared cooldown; transcript failures use a bounded per-input retry policy.
8. No daemon is added. Overdue work remains durable and visible until a hook or explicit recovery command runs.
9. The recap schema is created atomically in one versioned `llm_summary_db` migration, outside `SCHEMA_CORE` and pre-transaction additive migrations.
10. Eligibility thresholds come from a dedicated de-identified audit artifact before policy code is frozen.

## Constraints & Anti-Patterns

- Do not read JSONL from recap selection, packet, generation, retry, or status code.
- Do not invoke Claude, run migrations, or perform heavy orchestration synchronously in hooks.
- Preserve exact hook stdout and hot-path import boundaries.
- Never hold SQLite open during Claude execution.
- PID files and fallback markers are not ordinary queue ownership.
- Never bypass provider cooldown, platform safety, input freshness, fencing, or cleanup safety.
- Never delete the last good recap before guarded replacement.
- Keep v1 payloads physically stored but never render them as v2.
- Never persist packet content, raw model output, transcript text, or uncapped diagnostics in audit state/logs.
- Remove capability preflight, broad `--force`, citations, file evidence, source readiness, and continuation sections rather than retaining parallel paths.
- Do not change search ranking, FTS, chunks, vectors, embedding versions, deterministic-summary content, or JSONL decoding/content extraction.
- No daemon, resident scheduler, queue dependency, prompt configurability, or broad internal rename.

## Design Doc References

## Functional Requirements — FR#1 through FR#26 define observable contracts.
## Operational Lifecycle — jobs, attempts, fencing, cooldown, recovery, accounting, and retention.
## Architecture — recap contract, canonical DB input, finalization, provider boundary, eligibility, CLI/status, and schema.
## Migration — atomic version boundary and v1 compatibility.
## Test Strategy — required unit, integration, lifecycle, process, and evidence coverage.
## Impact — exact application, hook, test, and documentation surfaces.

## Convention Examples

### Close DB before detached/model work

**Source:** `src/ccrecall/hooks/sync_current.py`

```python
with get_connection(settings, load_vec=True) as conn:
    new_messages = sync_session(conn, session_file, project_dir)
# Connection is committed and closed before follow-up work.
```

The DB recap packet is copied and hashed inside its snapshot transaction, then Claude runs only after close.

### Conditional stale-result write

**Source:** `src/ccrecall/hooks/backfill_llm_summaries.py`

```python
UPDATE branches
SET summary_enrichment_json = ?, summary_enrichment_source_hash = ?
WHERE id = ? AND summary_source_hash = ?
```

V2 extends this pattern with `recap_input_hash`, active claim token, and contract-version predicates.

### Lightweight migration authority

**Source:** `src/ccrecall/llm_summary_db.py`

```python
conn.execute("BEGIN IMMEDIATE")
try:
    # Versioned DDL and backfill.
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.execute("COMMIT")
except Exception:
    conn.execute("ROLLBACK")
    raise
```

Recap objects must all live inside this atomic version boundary.

### Read-only status boundary

**Source:** `src/ccrecall/status.py`

```python
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
conn.execute("PRAGMA query_only = ON")
```

Status introspects schema capability before any recap query and never repairs state.
