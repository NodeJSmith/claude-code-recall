# Context: LLM Summary Enrichment

## Problem & Motivation

Deterministic branch context is reliable but loses the causal middle of long conversations: why decisions were made, what approaches failed, and what a future session should do next. This feature adds an optional Claude-backed Branch Resume Brief without replacing the deterministic summary, changing retrieval ranking, or moving model work onto a hook hot path. The brief must be bounded, grounded in the active branch, and fall back cleanly whenever enrichment is unavailable or invalid. It must also respect the user's local transcript privacy by passing only a temporary branch packet to Claude Code after explicit opt-in and a verified no-session-persistence capability check.

## Visual Artifacts

None.

## Key Decisions

1. Store enrichment separately on `branches`; deterministic `context_summary`, `context_summary_json`, and `summary_version` remain the baseline contract.
2. Persist a worker-owned envelope (`version`, configured model, completion time) around a validated Claude response body. Claude must not supply those metadata fields.
3. Make the summary branch-scoped, not a whole-session-family or project-history synthesis. Every factual field carries active-branch UUID provenance.
4. Give Claude a full validated branch packet plus a deterministic outline and summary. Preserve source fidelity rather than truncating the packet to meet a fixed cost target.
5. Use `sonnet` by default but allow `llm_summary_model` and the `$1.00` budget stop threshold to be configured. The threshold is not a guaranteed spend ceiling.
6. Keep Claude invocation isolated in a background worker with Read-only safe mode. SessionStart and Stop hooks only select/render/spawn.
7. Render a 2,400-character primary and 800-character supplementary Branch Resume Brief above, never instead of, deterministic context. Search display may prefer valid LLM title/latest-state fields without changing ranking.
8. Require manual evaluation of citation entailment and factual coverage. UUID membership alone does not establish that a citation supports a claim.

## Constraints & Anti-Patterns

- Do not put Claude invocation, capability probing, DB-heavy imports, sqlite-vec, fastembed, or onnxruntime on inline hook paths.
- Do not replace deterministic summaries, mutate `context_summary_json`, add enrichment to FTS/vector/`aggregated_content`, or alter search ranking.
- Do not use `shell=True`, `--bare`, writable tools, user customizations, original transcript directories, or raw transcript/prompt/response logging.
- Do not render stale, failed, uncited, schema-invalid, or over-budget enrichment. Preserve the deterministic fallback exactly.
- Do not retry `invalid_output` or branch-level `budget_exceeded` automatically; require `--force`. Treat capability-check and branch-run budget failures as separate domains.
- New code follows repository conventions: no lazy imports, no `from __future__ import annotations`, use `whenever` for new timestamps, and preserve hook stdout JSON envelopes.

## Design Doc References

## Functional Requirements — FR#1 through FR#18 define the fallback, worker, privacy, source, schema, rendering, and configuration behavior.
## Data model — branch columns, status recovery, source fingerprint, and compare-and-swap requirements.
## Stored enrichment envelope — worker-owned persisted fields and the Claude response-body boundary.
## Claude input packet — source validation, packet content, cleanup, and prompt requirements.
## Claude invocation — safe-mode subprocess, capability sidecar, cost semantics, and settings defaults.
## Worker placement and Eligibility — detached process, PID, selection, transaction, and retry behavior.
## Rendering — primary/supplementary context budgets and search-card compatibility.
## Migration and Test Strategy — schema rebuild requirements and automated/manual test obligations.

## Convention Examples

### Detached hook helper pattern

```python
kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
if sys.platform == "win32":
    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
else:
    kwargs["start_new_session"] = True
subprocess.Popen(
    ["ccrecall", "sync-current", "--input-file", tmp_path],
    **kwargs,
)
```

### Summary backfill batch pattern

```python
while True:
    cursor.execute(
        """
        SELECT id FROM branches
        WHERE summary_version IS NULL
           OR (summary_version < ? AND summary_version != ?)
        LIMIT ?
    """,
        (SUMMARY_VERSION, CONTENT_ERROR_VERSION, BATCH_SIZE),
    )
    rows = cursor.fetchall()
    if not rows:
        break
```

### Settings merge pattern

```python
DEFAULT_SETTINGS = {
    "auto_inject_context": True,
    "max_context_sessions": 2,
    "exclude_projects": [],
    "logging_enabled": True,
    "log_level": "INFO",
    "alert_snooze_hours": 24,
}
```

### Deterministic render contract

```python
summary_json = build_context_summary_json(branch_row, messages)
summary_md = render_context_summary(summary_json)
json_str = json.dumps(summary_json, ensure_ascii=False)
```
