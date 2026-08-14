# Design: Session Recaps

> **Not implemented.** This feature was built on branch `114-115` (PR #118) and abandoned
> before merge. Nothing described below ships. The design itself is still substantially
> correct and is the intended starting point for any second attempt — read
> [`retrospective.md`](retrospective.md) first for what to keep and what went wrong.

**Status:** abandoned (attempt 1)

**Date:** 2026-08-12
**Scope-mode:** hold

## Problem

The opt-in LLM feature currently produces a rigid Branch Resume Brief from transcript JSONL files. Its continuation-oriented schema, citations, file-evidence checks, capability preflight, per-Stop invocation, and overloaded branch status make a simple recognition aid brittle and operationally difficult to trust.

The desired product is a concise Session Recap: enough to recognize what a past session was about and what happened. Evaluation showed that normalized imported SQLite content is sufficient for this purpose. Successful DB- and JSONL-sourced recaps were equivalent on recognition, work arc, and outcome across the private sample, while DB packets were smaller and avoid a second transcript parse/readiness boundary. Sonnet completed every evaluated DB case; Haiku did not. The design therefore uses imported SQLite as the only recap authority and Sonnet as the default.

Automatic generation still needs durable lifecycle semantics. SessionEnd cannot wait for sync or Claude, a detached spawn can fail, concurrent hooks and manual backfills can race, global provider failures can cause retry storms, and process timeouts can leave descendants alive. The feature must preserve intent and converge honestly without adding a daemon.

## Goals

- Generate a concise, recognizable, accurate account of a session's work arc and outcome from imported SQLite data only.
- Use Sonnet by default while retaining per-run model, budget, and timeout overrides.
- Generate automatically after meaningful ended sessions without adding model work or extra output to hook paths.
- Preserve finalization intent, serialize provider work per installation, fence recovered workers, and prevent retry storms.
- Expose complete, reconcilable pending, excluded, current, deferred, blocked, attempt, and recovery state.
- Preserve deterministic summaries, existing v1 data, opt-in behavior, existing databases, incremental embeddings, and search ranking.

## Non-Goals

- Reading transcript JSONL files during recap selection, packet construction, generation, retry, or status.
- A recap browser, daemon, resident scheduler, configurable prompts, or unsupported max-token control.
- Per-item citations, file-evidence validation, continuation instructions, or authoritative task-completion claims.
- Converting or deleting stored v1 envelopes.
- Changing transcript JSONL decoding/content extraction, deterministic-summary content, search ranking, FTS, chunks, vectors, or embedding versions. Branch path discovery may additionally preserve order without changing parsed message content or membership.
- Renaming every internal `summary_enrichment` symbol or public compatibility command in this release.

## User Scenarios

### Claude Code user: Automatic recap generation
- **Goal:** Find a useful recap after meaningful work ends.
- **Context:** LLM recaps are explicitly enabled and the platform supports safe provider-process cleanup.

#### End a meaningful session

1. **End the session**
   - Sees: No recap UI, delay, or additional hook output.
   - Decides: Nothing; prior opt-in controls automatic generation.
   - Then: SessionEnd durably upserts finalization work and best-effort starts the detached drainer.
2. **Let ccrecall finalize**
   - Sees: Status later reports pending, deferred, current, excluded, blocked, or overdue work.
   - Decides: Whether an overdue or blocked item needs the displayed recovery command.
   - Then: The drainer performs final sync, captures one canonical DB input snapshot, and serially invokes Sonnet when admitted.
3. **Recall the session later**
   - Sees: A bounded Session Recap above the deterministic context summary.
   - Decides: Whether this is the remembered session and whether to inspect existing recall/tail/search detail.
   - Then: Missing, stale, malformed, failed, or legacy recap data silently falls back to deterministic context.

### Claude Code user: Backfill and recover
- **Goal:** Generate historical recaps and recover durable pending or failed work.
- **Context:** `ccrecall status` identifies work and exact recovery actions.

#### Run historical backfill

1. **Inspect status**
   - Sees: Recap schema/platform readiness, provider cooldown, policy/defaults, population counts, overdue jobs, and latest attempt outcomes.
   - Decides: Whether to backfill, recover overdue work, reset provider health, or retry a targeted failure.
   - Then: Status remains read-only and never invokes Claude.
2. **Run backfill or recovery**
   - Sees: Session/day selectors, attempt limit, targeted failure retry, and one-run model/budget/timeout options.
   - Decides: Which selector or recovery action to use.
   - Then: The command reconciles expired leases, queues matching work, and uses the same globally serialized drainer as SessionEnd.
3. **Review accounting**
   - Sees: Separate population, examined-candidate, deferred, and started-attempt partitions that reconcile.
   - Decides: Whether any blocked failure warrants another explicit action.
   - Then: Re-running unchanged converged work performs no provider calls.

## Functional Requirements

- **FR#1** A valid current v2 recap must contain a bounded non-empty summary and may contain a bounded title and normalized outcome; malformed optional fields must not invalidate a usable summary.
- **FR#2** Recap generation must describe the session's recognizable work arc and evidenced outcome without advice, continuation planning, exhaustive chronology, or project-wide claims.
- **FR#3** Recap input must come only from normalized imported SQLite records; recap code must not resolve or read transcript JSONL files.
- **FR#4** The exact canonical DB projection written to the provider packet must produce a versioned `recap_input_hash` stored on the attempt.
- **FR#5** A successful recap may materialize only when the active branch, claim token, recap contract, and recomputed `recap_input_hash` still match the captured attempt.
- **FR#6** Rendering must display only a valid v2 envelope whose materialized input hash matches the branch's current recap input; every other case must use deterministic context only.
- **FR#7** Automatic generation must remain opt-in, use Sonnet by default, and begin from SessionEnd rather than ordinary Stop.
- **FR#8** SessionEnd must durably record finalization intent before best-effort detachment, return without waiting for sync or Claude, and preserve exact hook stdout on every path.
- **FR#9** One durable finalization job per session must coalesce repeated requests and survive spawn failure, process death, concurrent Stop sync, and undocumented hook ordering.
- **FR#10** Job and attempt claims must use monotonic fencing tokens so an expired worker cannot publish, terminalize, heartbeat, or acknowledge cleanup after recovery reclaims its work.
- **FR#11** Provider invocation must be globally serialized per installation across automatic finalization, recovery, and manual backfill; capacity contention must leave durable pending work.
- **FR#12** Classified global provider failures must open a shared capped cooldown, abort the current drain, leave remaining jobs pending, and never become transcript-specific materialized failures.
- **FR#13** A write-capable recovery command must reconcile expired leases and cleanup obligations; read-only status must expose overdue work and the exact command without claiming deadline-driven recovery.
- **FR#14** One versioned meaningful-session policy must be shared by automatic selection, manual backfill, and status, with stable reason codes and explainable measures.
- **FR#15** Manual recovery must replace broad `--force` with targeted failure retry and optional one-run model, budget, and timeout overrides that do not mutate configuration.
- **FR#16** Timeout handling must terminate, escalate, wait for, and reap the POSIX provider process group before recording ordinary timeout or deleting the packet.
- **FR#17** Cleanup uncertainty must produce an actionable `cleanup_failed` outcome, block the job, retain only owner-protected quarantined packet/process metadata, and prevent a normal success/timeout claim.
- **FR#18** Platforms without proven process-tree cleanup must visibly disable provider invocation while hooks may still preserve durable work; status and completion output must explain the limitation.
- **FR#19** Worker completion output must separately reconcile a durable selector snapshot, final run-candidate dispositions, and started-attempt outcomes, including limits and global aborts.
- **FR#20** Read-only status must distinguish a current recap schema with zero work from a missing, outdated, or partial recap schema and provide writable migration guidance.
- **FR#21** Recap schema creation, constraints, ordering backfill, and indexes must become claim-capable atomically before any worker can process jobs.
- **FR#22** Attempt and run history must use bounded indexed queries and a maintenance retention policy that preserves live/current/retry lineage while pruning eligible old terminal detail.
- **FR#23** A continued and re-imported session must stale its old recap without deleting it, preserve unchanged exchange embeddings, and become normally eligible for refresh.
- **FR#24** Every packet and provider process group must have a committed token-bound attempt owner before creation or launch; recovery must prove that process group dead and cleanup complete before replacement admission.
- **FR#25** Automatic transcript-specific retries must use a durable versioned per-input budget and backoff, then transition exhausted work to a visible blocked reason until input changes or targeted retry resets it.
- **FR#26** Packet quarantine must have visible count and byte limits that pause new provider admission when exceeded, plus oldest-age warnings, without deleting content while process liveness is uncertain.

## Edge Cases

- SessionEnd races with Stop or transcript persistence. The durable job is authoritative; the detached drainer coordinates with sync, performs a final sync, and captures recap input only from the committed DB state.
- SessionEnd records intent but spawn fails and no later hook runs. A DB job or owner-only fallback marker remains visible; `ccrecall recap recover` is the explicit write-capable maintenance path. No design claim promises timely execution without an invocation.
- Two SessionEnd events or manual backfill target one session. `UNIQUE(session_id)` coalesces the job; global and per-job fenced claims permit one live attempt.
- A worker's lease expires while Claude may still run. Recovery does not admit replacement work until persisted PID/process-group/start identity proves that exact group exited and packet cleanup completes. Missing or ambiguous launch identity becomes `cleanup_failed`; fencing still rejects all late DB mutations.
- Provider authentication, rate limiting, unsupported CLI, missing binary, or infrastructure failure occurs. The current attempt records `global_abort`, shared health opens a cooldown, the run stops, and no branch receives a transcript failure stamp.
- Cooldown has not elapsed. Work remains durable as `deferred_cooldown`; no provider process starts. Elapsed cooldown admits one serialized normal probe.
- Imported messages change during generation. Recomputed `recap_input_hash` differs; the attempt becomes `stale_discarded` and the previous recap remains untouched.
- Message order is ambiguous in an upgraded DB. Migration backfills explicit `branch_messages.position` deterministically; branch detection is changed to retain an ordered root-to-leaf UUID list and subsequent imports write that order. Duplicate positions fail schema validation rather than silently changing packet order.
- A tool-only assistant turn has empty prose. Non-empty `tool_content` remains in the ordered input projection and packet.
- Claude returns malformed JSON or an unusable core summary. The attempt is `unusable_output`; oversized/invalid optional fields are normalized or dropped.
- Timeout group termination succeeds but packet deletion fails. The result is `cleanup_failed`, not ordinary timeout; maintenance owns quarantine cleanup.
- A process crashes after packet reservation but before file creation, or after file creation but before launch. The committed attempt's deterministic path/nonce lets recovery distinguish absent, removable unlaunched, and uncertain launched artifacts without scanning arbitrary files.
- The platform cannot provide the tested POSIX group semantics. Provider work is disabled and visible; deterministic context remains fully functional.
- Status opens an old or interrupted schema read-only. It inspects schema capabilities before recap queries and reports `unavailable` or `out_of_date`, not empty counts or a SQL exception.
- A v1 payload remains stored. It is counted as legacy, never interpreted as v2, and never prevents deterministic fallback.
- Attempt history is large. Latest-state/status queries use bounded indexes; maintenance prunes eligible terminal history in bounded transactions without touching jobs or materialized recaps.
- Quarantine reaches its configured count or byte budget. Provider admission pauses globally and status reports the oldest age and recovery command; hooks may continue recording intent without creating packets.

## Operational Lifecycle

### Persistent state

Four primary SQLite lifecycle records separate execution responsibilities; run-accounting tables described below supplement them:

1. `session_recap_jobs`: one row per session, containing requested generation/input hash, trigger, state, reason, claim token, lease deadline, active attempt, next eligibility, and timestamps.
2. `session_recap_attempts`: append-only provider-attempt audit containing captured input hash and versions, effective controls, trigger, retry/evaluation identity, claim token, timestamps, terminal outcome, reason, and capped diagnostic.
3. `session_recap_runtime`: singleton globally fenced drainer lease containing monotonic token, owner PID, lease deadline, and heartbeat.
4. `session_recap_provider_health`: singleton cooldown containing classified reason, consecutive global failure count, capped diagnostic, last failure, and `retry_after`.

Filesystem wake-up markers are optional owner-only hints containing identifiers and timing only. They are never queue or ownership state and may be removed after the DB job commits. Existing PID files may suppress redundant process creation but never cause work loss.

### Job and attempt transitions

```text
job:
  pending -> claimed -> current
                     -> excluded
                     -> blocked
                     -> pending (cooldown, stale input, bounded retry, reclaimed lease)
  claimed --expired/recovered--> pending + prior attempt abandoned

attempt:
  reserved -> cancelled_before_launch
           -> cleanup_failed
           -> abandoned
           -> running -> succeeded
                      -> stale_discarded
                      -> timeout
                      -> budget_exceeded
                      -> unusable_output
                      -> global_abort
                      -> cleanup_failed
                      -> abandoned
```

Job claim occurs under `BEGIN IMMEDIATE`: reclaim expired ownership only after prior process cleanup is proven, increment the job token, and assign the lease. The claimant then performs final sync, refreshes the current input hash, and evaluates current/eligibility/cooldown before any attempt exists. Provider admission creates and binds a `reserved` attempt with deterministic owner-only packet path and nonce before packet creation. The provider wrapper itself supplies a preselected process-group identity: on POSIX the child becomes group leader with PGID equal to its PID, and the parent knows that identity from `Popen` before returning control. The parent persists leader PID/PGID plus OS process start identity before any further work; a crash after spawn but before persistence leaves an ambiguous reservation, never a safely removable one, and blocks replacement until conservative group discovery/reaping or explicit cleanup. Only a reservation proven never launched may become `cancelled_before_launch`. Every later mutation includes the active token. `current`, `excluded`, and `blocked` are decisions for the job's recorded requested input hash; the import transaction that stores a different current input hash or versions atomically resets old content-dependent `current`, `excluded`, or transcript-failure blocks to `pending` and clears their terminal fields. Stable environment/policy blocks such as `platform_unsupported` remain blocked across content changes and requeue only on capability change or targeted action. SessionEnd may also request a newer generation. Retryable/deferred work remains `pending` with a reason and optional next-eligible time.

Automatic retries are per `(session, recap_input_hash, recap/input/policy versions)`. Timeout receives one automatic retry after capped backoff; `unusable_output`, `budget_exceeded`, `cleanup_failed`, and global failures receive none at the transcript level. Exhaustion sets a stable blocked reason with a targeted retry command. A changed input identity resets the budget; `--retry-failures` explicitly creates a new budget lineage without erasing prior attempts.

### Global drainer and cooldown

SessionEnd, manual backfill, and recovery all enqueue jobs and best-effort start the same serialized drainer. The drainer obtains the singleton runtime lease and processes one provider invocation at a time. It renews both runtime and job leases around bounded work. A second process exits without consuming or dropping jobs. Lease expiry alone never authorizes a replacement launch: recovery first resolves persisted process identity and cleanup phase, reaps the exact process group when live, and blocks globally on uncertainty.

Before a provider attempt starts, health admission checks `retry_after`. Global failures close only the real started attempt as `global_abort`, update capped exponential cooldown with provider retry-after taking precedence, stop the drain, and leave other jobs pending. Success resets consecutive failures. `ccrecall recap reset-health` explicitly clears health; targeted session retry does not bypass it.

### Recovery and convergence

There is no daemon. Lease deadlines make overdue state detectable, not self-executing. SessionStart/SessionEnd may cheaply best-effort detach recovery. Every write-capable recap command first reconciles expired runtime/job leases, abandons fenced attempts, retries safe packet cleanup, and requeues eligible work. `ccrecall recap recover` exposes this directly. Read-only status reports overdue counts and exact guidance.

An unchanged workload converges when each selected session is current, excluded, blocked after user-action-required failure, pending behind a future cooldown/lease, or already running. Once cooldowns and bounded automatic retries are exhausted or repaired, unchanged normal reruns make no duplicate provider calls.

### Accounting

Each manual run creates a `session_recap_runs` row and immutable `session_recap_run_candidates` membership rows for every candidate matching its selectors before applying `--limit`. Each membership row stores the snapshot input hash, initial disposition, final run disposition, and optional started attempt ID:

- **Snapshot population:** immutable run-candidate membership count.
- **Final candidate partition:** ineligible, already current, already running, deferred cooldown, deferred by limit, blocked, attempted, or deferred after incomplete abort.
- **Started-attempt partition:** succeeded, timeout, budget exceeded, unusable output, stale discarded, global abort, cleanup failed, abandoned.

`--limit` caps attempts associated with that run, not discovery. The serialized drainer atomically attaches an admitted attempt to one run-candidate row and decrements that run's remaining allowance; coalesced jobs may satisfy several run snapshots, but one attempt is owned by at most one run and other matching memberships finish as already running/current. A global abort marks the owning run incomplete and finalizes all unresolved membership rows as deferred after abort. Bounded run records persist selector metadata and counts without transcript content.

### Retention

Retain all nonterminal attempts, the latest successful attempt for each current input, the latest terminal attempt per job, and retry ancestors needed by retained rows. Other terminal attempts and detailed run records become prune-eligible after 90 days; run detail may compact to aggregate counts. Quarantine metadata records owner attempt, path nonce, byte size, age, process identity, and cleanup phase without content. Configured count and byte ceilings pause all new provider admission; age is warning/status information because age alone cannot prove safe deletion. `ccrecall recap maintain` reports dry-run counts by default and prunes only with an explicit option, in bounded transactions. It never deletes jobs or materialized recaps and never purges quarantine until process death is proven and cleanup ownership is reconciled. There is no force-purge override while liveness is uncertain.

## Acceptance Criteria

- **AC#1** `uv run pytest tests/test_summary_enrichment.py` proves loose v2 normalization, bounded rendering, v1 rejection, and deterministic fallback for absent, malformed, stale, failed, or hash-mismatched recaps. (FR#1, FR#2, FR#6)
- **AC#2** DB-input tests prove packet bytes and `recap_input_hash` derive from the same transaction snapshot; import persists the current hash after links/metadata/deterministic summary finalize; changes to ordered content, tool content, admitted metadata, or policy/version stale the hash, while transcript path changes do not. (FR#3, FR#4, FR#5)
- **AC#3** DB-input tests prove tool-only turns are retained and inactive, notification, child/session-external, and superseded messages are excluded. (FR#3, FR#4)
- **AC#4** Hook tests prove Stop never starts recap inference; SessionEnd uses a migration-free bounded DB upsert when recap schema is ready, otherwise writes an owner-only fallback marker; intent precedes detachment; all paths emit exactly `{}`; and neither hook migrates, imports, or waits for the provider boundary. (FR#7, FR#8, FR#9)
- **AC#5** Lifecycle tests prove concurrent drainers create one invocation, lease reclaim increments fencing, and every late heartbeat/write/terminal/cleanup acknowledgement from the old token is rejected. (FR#9, FR#10, FR#11)
- **AC#6** Repeated-run tests prove shared cooldown prevents calls before `retry_after`, one serialized probe is admitted afterward, success resets health, explicit reset works, and global failures leave branch materialized state untouched. (FR#12)
- **AC#7** Recovery tests prove expired jobs/attempts and cleanup obligations reconcile idempotently; status remains read-only and reports overdue work plus exact commands. (FR#13)
- **AC#8** A committed de-identified eligibility artifact records sampled strata, labels, selected policy/version/reason codes, and candidate-rule comparison; synthetic tests cover every rule and worker/status parity. (FR#14)
- **AC#9** CLI tests prove broad `--force` and `--check-capability` are absent; targeted retry and one-run model/budget/timeout controls are validated and never persist settings. (FR#15)
- **AC#10** A real POSIX child/grandchild fixture proves TERM/grace/KILL/wait cleanup leaves no descendants; injected teardown/deletion failures produce `cleanup_failed` and quarantine behavior. (FR#16, FR#17)
- **AC#11** Platform tests prove unsupported cleanup platforms do not invoke Claude and report the limitation in human and JSON status/output without affecting deterministic context. (FR#18)
- **AC#12** Mixed-population tests prove immutable run membership, final candidate dispositions, run-owned attempt limits, coalesced jobs, global abort deferral, stale discards, and zero-attempt reruns reconcile without double ownership. (FR#19)
- **AC#13** Read-only status tests over current, pre-recap, and intentionally partial schemas prove capability diagnosis occurs before recap SQL and never migrates the DB. (FR#20)
- **AC#14** Migration interruption and concurrency tests prove the DB exposes either the old schema or the complete claim-capable recap schema, never a usable partial state; fresh and upgraded final shapes match. (FR#21)
- **AC#15** Large-history tests prove latest-state queries use declared indexes and retention preserves live/current/latest/retry lineage while bounded pruning leaves jobs and materialized recaps intact. (FR#22)
- **AC#16** Continued-session tests prove re-import changes recap input freshness, retains the old payload, preserves unchanged vectors/chunks, and makes the session refreshable. (FR#23)
- **AC#17** A committed aggregate evaluation artifact records the private DB/JSONL and Haiku/Sonnet method and de-identified results, selects DB-only input and Sonnet, and contains no transcript excerpts, paths, UUIDs, prompts, raw outputs, or reviewer notes. (FR#3, FR#7)
- **AC#18** `uv run pytest` and `uvx prek run --all-files` pass with hook hot-path import and stdout invariants intact. (FR#6, FR#8, FR#18)
- **AC#19** Launch crash-window tests prove every packet has a committed owner, replacement waits for exact process-group death, ambiguous identity blocks admission, and no two provider groups overlap. (FR#11, FR#24)
- **AC#20** Repeated failure tests prove one automatic timeout retry per unchanged input, no automatic retries for other transcript failures, durable blocked exhaustion, and explicit/new-input reset behavior. (FR#25)
- **AC#21** Quarantine tests prove count/byte ceilings pause provider admission, status reports count/bytes/oldest age, and maintenance never automatically deletes while liveness is uncertain. (FR#17, FR#26)

## Key Constraints

- Never read transcript JSONL in recap code; transcript knowledge remains at the existing parsing/import boundary.
- Never invoke Claude, migrate recap schema, or perform recap DB orchestration synchronously on a hook path beyond the minimal durable job upsert.
- Never emit additional hook stdout.
- Never hold a DB transaction or connection while Claude runs.
- Never treat PID files or filesystem markers as authoritative work ownership.
- Never let retry selectors bypass active-session, current-input, platform, provider cooldown, or cleanup safety.
- Never delete the last good recap before a guarded replacement succeeds.
- Never interpret v1 structured envelopes as v2 recaps.
- Never persist packet content, model stdout, transcript text, or uncapped diagnostics in attempt/run state or logs.
- Do not retain capability preflight/sidecar, broad `--force`, citations, file-evidence output checks, or rigid continuation sections.

## Dependencies and Assumptions

- Claude Code's installed CLI remains the only provider boundary. Real invocations, not a removed heuristic smoke test, detect compatibility and provider failures.
- SQLite is authoritative only after ccrecall import. This accepted tradeoff gives up JSONL-only user text and linkage metadata; the private evaluation found no material recognition/work-arc/outcome loss for successful DB recaps. Existing message UUID rows are append-oriented and normally immutable except null `tool_content` backfill. A controlled full reimport/repair path is the authority for parser corrections: it atomically updates recap-relevant message fields, links/order, deterministic summary, current input hash, and job invalidation before recaps resume.
- Sonnet's accepted cost/latency tradeoff buys materially better observed completion reliability: DB+Sonnet succeeded in 6/6 sampled calls, while DB+Haiku succeeded in 4/6. The sample is directional rather than statistically conclusive, so the model remains overridable.
- The package has no explicit Windows support contract and CI exercises Ubuntu. This release guarantees automatic provider cleanup on POSIX only; unsupported platforms retain deterministic summaries and visible queued recap state.
- No daemon means overdue work cannot repair itself after the final failed spawn. Durable visibility plus explicit recovery is the honest guarantee accepted for this scope.
- Local transcripts may contain sensitive content. Private evaluation bundles stay outside the repository; only aggregate methodology and metrics are committed.

## Architecture

### Minimal recap contract

Replace `summary_enrichment.py`'s continuation schema with a v2 envelope whose model body requires only `summary`; `title` and `outcome` are optional. Unknown fields are ignored, optional defects are dropped, and bounded text is safely truncated. Only unparseable output or a missing/unusable summary fails.

The worker adds version, model, generation timestamp, attempt ID, and `recap_input_hash`. Rendering validates v2, successful state, and current input hash, displays `### Session Recap`, and leaves deterministic context below it. Search hydration may map title/summary preview without ranking changes.

### Canonical DB recap input

Create `recap_input.py` as the only recap projection boundary. In one read transaction it loads the active branch/session/project metadata, deterministic summary, and ordered active non-notification messages. Message records include stable order, role, timestamp, origin, UUID/parent identity as available in imported schema, `content`, and `tool_content`. Tool-only turns remain useful.

Add `position INTEGER` to `branch_messages`. Change branch detection to preserve an ordered root-to-leaf UUID list alongside membership; `diff_branch_messages()` updates positions for retained links and inserts new links from that order. Upgrade backfill orders existing links deterministically by normalized timestamp and message row ID. A uniqueness constraint on `(branch_id, position)` makes order corruption explicit. Existing timestamp-ordered consumers remain unchanged unless they explicitly need recap order.

The projection canonicalizes decoded JSON metadata, explicit nulls, UTF-8, sorted keys, and fixed separators. The exact canonical object is both hashed and serialized to an owner-only packet before the transaction closes:

```text
recap_input_hash = sha256(canonical_json({
  input_contract_version,
  recap_contract_version,
  eligibility_policy_version,
  ordered_messages,
  deterministic_summary,
  admitted_session_and_branch_metadata
}))
```

`summary_source_hash` retains its existing deterministic-summary freshness meaning; it is not recap packet identity. Add separate branch columns `recap_input_hash` (current imported projection) and `summary_enrichment_input_hash` (the projection represented by the materialized recap), plus stored current input-contract and eligibility-policy versions. Import recomputes and stores current hash/versions in the same transaction after message membership/order, metadata, and deterministic summary are finalized. Rendering compares the two stored hashes and validates the materialized recap contract, captured input-contract version, and captured policy version against code constants; a release-time version change therefore fails closed even before re-import. Packet capture recomputes current hash/versions in its snapshot transaction and persists refreshed current values. SessionStart never scans all messages. After Claude returns, rebuild the projection and perform one token/hash/version-guarded materialization. Source paths, import-log proof, source-file safety, UUID packet coverage, and transcript rereads leave the recap subsystem.

### Durable finalization and serialization

Replace the current clear-only SessionEnd command with a lightweight coordinator. It writes clear handoff when applicable, then uses a dedicated no-migrate SQLite connection with a short busy timeout and read-only schema precondition check to upsert/coalesce `session_recap_jobs`. If the DB/schema is absent, outdated, partial, or busy, it writes one owner-only versioned fallback journal entry per validated session UUID using a deterministic filename and atomic temp-file replace. The entry contains requested generation/end timing only; repeated events merge monotonically. Every drainer starts by replaying a bounded journal batch before selecting DB jobs: it quarantines malformed entries, upserts each DB job before deleting its marker, and fsyncs file and directory where supported. Explicit recovery uses the same replay function. SessionEnd then best-effort detaches the drainer and guarantees `{}`. Stop retains incremental sync/summaries/embeddings but removes recap spawning.

The drainer owns the singleton runtime lease and serially processes jobs. It coordinates with current sync, performs final sync through factored non-hook orchestration, refreshes current input hash, evaluates policy/current/cooldown under fenced job ownership, and creates an attempt only after provider admission. It then captures the exact input packet, closes SQLite, invokes Claude, and conditionally materializes the result. Manual backfill creates durable run membership and enqueues selected jobs rather than invoking a parallel worker path.

### Provider boundary and health

`llm_summarizer.py` retains constrained argv, isolated owner-only cwd, `--no-session-persistence`, structured output, budget, and timeout boundaries but no longer knows source files or capability sidecars. POSIX invocation uses a new process session/group with TERM, grace, KILL, wait/reap escalation.

Provider health is installation-global SQLite state. Missing CLI, unsupported CLI, authentication, rate limiting, provider unavailability, and infrastructure failures open capped exponential cooldown; a provider retry-after wins. Ordinary work after expiry supplies one serialized probe. `cleanup_failed` additionally blocks the job and quarantines the packet until maintenance proves cleanup.

### Eligibility, CLI, and status

Create one lightweight `recap_eligibility.py` used by queue selection and status. Absolute prerequisites are an active session branch, current deterministic summary, and non-empty eligible imported messages. Meaningfulness uses substantive user/assistant prose, non-repetitive tool activity, modified files, commits, and elapsed duration. A private local audit freezes policy v1 before implementation of automatic selection.

The implementation plan must begin with a dedicated eligibility-audit task. It commits only a de-identified aggregate artifact containing strata, measures, labels, candidate-rule comparison, selected thresholds, policy version, and reason codes. The subsequent eligibility implementation task depends on that artifact and must not choose thresholds independently. Private labels, identifiers, paths, and transcript content remain outside the repository.

Keep `ccrecall backfill llm-summaries` as a compatibility command while making Session Recap terminology canonical in help. Add a `recap` command group or equivalent thin commands for `recover`, `reset-health`, and `maintain`. Remove `--check-capability` and `--force`; add `--retry-failures`, `--model`, `--max-budget-usd`, and `--timeout-seconds`. Existing session/day/limit selectors remain, with limit counting started attempts.

Status first introspects recap schema capability read-only. When available, it uses shared queries to report opt-in, platform, policy/contracts, Sonnet defaults, provider cooldown, jobs, overdue leases, current/stale/legacy populations, latest attempt outcomes, quarantine budget, and maintenance guidance. Unsupported platforms set durable jobs to `blocked/platform_unsupported`, exclude them from runnable/overdue counts, suppress automatic drainer detachment, and requeue only after detected capability change or targeted action. Status never imports provider or embedding-model boundaries for recap reporting.

### Schema and retention

One new version migration in `llm_summary_db._apply_migrations()` creates/updates all recap objects in one `BEGIN IMMEDIATE`: `branch_messages.position` and backfill, both branch input-hash columns, jobs, attempts, runtime lease, provider health, run/run-candidate accounting, checks, foreign keys, and named indexes. None of these objects may be added to `SCHEMA_CORE` or the existing pre-transaction additive migration block. `user_version` advances only after all postconditions hold. `schema.py` remains supporting baseline DDL only, and `db.py` delegates to the single `llm_summary_db` migration entry point. Workers verify the required object/column/index set before claims.

Required indexes serve jobs by readiness/lease, attempts by job/latest/input/status, one partial unique live attempt, and runs by start time. Diagnostics and payload text are not indexed. Maintenance pruning is explicit, bounded, and lineage-preserving.

## Implementation Preferences

- Retain `llm_summary_db.py` as the sole embedding-free recap migration authority; add a separate no-migrate, bounded connection helper for SessionEnd job upsert.
- Use SQLite `BEGIN IMMEDIATE`, monotonic integer tokens, conditional updates, and partial unique indexes rather than a new queue dependency.
- Use plain IDs/dicts/tuples and small local frozen result types; do not introduce cross-module domain dataclasses.
- Use direct console scripts and `detached_popen_kwargs()` for hook-spawned processes.
- Use `whenever` timestamps, stdlib logging, owner-only atomic files, and one global CLI `--json` surface.
- Keep Sonnet, medium effort, evaluated budget, and a 120-second provisional timeout; permit bounded one-run overrides. The successful DB+Sonnet maximum was below 90 seconds, leaving cleanup margin.
- Apply the smallest compatible internal renaming; user-facing terminology changes immediately.

## Replacement Targets

- `summary_enrichment.py`: replace v1 exact continuation/citation/file-evidence schema and renderer with loose v2 recap plus input-hash freshness.
- `llm_summarizer.py`: replace source-file packet/capability machinery with canonical DB-packet invocation and POSIX process-group cleanup.
- `backfill_llm_summaries.py`: replace source readiness, force/capability states, direct scanning/invocation, and success-only output with queue selection, targeted retry, and reconciled accounting.
- `sync_current.py`: remove per-Stop recap spawning and expose reusable final-sync orchestration.
- `clear_handoff.py`/SessionEnd hook: replace clear-only behavior with clear handoff plus durable DB job upsert.
- Mutable branch-only provider failure state: replace as authority with jobs, attempts, runtime lease, and provider health; retain branch columns only as the current materialized recap cache.
- Existing tests for citations, source files, capability preflight, broad force, strict continuation sections, and Stop generation are removed or rewritten rather than preserved beside v2.

## Migration

Set recap contract version to 2 and input-contract version to 1. Existing v1 payloads and public branch columns remain physically untouched; version mismatch prevents v1 rendering. A successful v2 generation replaces only the materialized current payload/cache.

Advance the DB schema in one atomic recap migration under `llm_summary_db._apply_migrations()`. Unlike current additive migrations that run and commit before the version transaction, recap claim objects cannot be exposed piecemeal. Keep every recap table, recap branch/link column, constraint, backfill, and index out of `SCHEMA_CORE` and the pre-transaction additive migration block. Fresh and upgraded DBs both execute the same versioned recap migration after baseline creation and reach the same verified shape.

Backfill `branch_messages.position` deterministically without changing message membership. No recap generation occurs until schema postconditions pass. Rollback to old code leaves deterministic summaries and v1 payloads functional; old code ignores new tables/columns. Full downgrade support for v2 job/attempt state is not required.

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

## Alternatives Considered

- **Continue using JSONL packets with a file fingerprint.** Rejected because successful DB recaps were equivalent for recognition and DB-only input removes source discovery, proof, path safety, coverage, and dual-authority races.
- **Reuse `summary_source_hash` as recap input identity.** Rejected after code inspection: it covers deterministic-summary fields and aggregate content but not the exact ordered recap projection. A separate canonical hash is required.
- **Use Haiku by default.** Rejected because only 4/6 DB calls completed versus 6/6 for Sonnet; quality of successful outputs was good, but completion reliability was not.
- **Keep automatic generation on Stop with debounce.** Rejected because Stop is per-turn and repeatedly spends while the session evolves.
- **Use only PID files and detached workers.** Rejected because skip-on-contention loses work and cannot fence recovered processes.
- **Allow lease expiry to launch a replacement immediately.** Rejected because fencing protects SQLite state but not duplicate provider cost or sensitive transmission; exact process-group death must be proven first.
- **Add a daemon or worker pool.** Rejected as unnecessary operational surface for asynchronous local work; one serialized drainer plus durable jobs is sufficient.
- **Promise automatic deadline recovery.** Rejected because no process can execute a deadline without a scheduler. Durable overdue visibility and explicit maintenance are honest and testable.
- **Support automatic invocation on every OS immediately.** Rejected because only POSIX cleanup can be proven in current CI; deterministic fallback remains available elsewhere.

## Test Strategy

### Required Test Types

- **Unit:** recap contract, canonical projection/hash, eligibility, failure classification, cooldown math, transition guards, schema capability, CLI validation, and retention selection.
- **Integration:** SQLite migration, snapshot/packet equivalence, final sync to queue to materialization, rendering/status, stale races, cooldown, recovery, and accounting.
- **Lifecycle:** repeated mixed-population runs with concurrency, crashes, expired leases, global failures, limits, cleanup failure, repair, and convergence.
- **Real process fixture:** POSIX child/grandchild timeout and escalation; provider calls remain faked elsewhere.
- **Manual evidence:** private DB/JSONL and model evaluation, with only aggregate de-identified results committed.

### Existing Tests to Adapt

- `tests/test_summary_enrichment.py`: v2 normalization/input freshness/fallback.
- `tests/test_llm_summarizer.py`: DB packet invocation and real process cleanup; remove source/capability/citation expectations.
- `tests/test_backfill_llm_summaries.py`: queue lifecycle, cooldown, targeted retry, recovery, and accounting.
- `tests/test_llm_summary_cli.py`: recap terminology, commands, selectors, and per-run overrides.
- `tests/test_sync_hook.py`: remove Stop recap spawn and preserve sync behavior.
- `tests/test_clear_handoff_contract.py`: SessionEnd durable-job coordinator and stdout.
- `tests/test_context_injection.py` and `tests/test_search.py`: v2 rendering/preview without ranking changes.
- `tests/test_status.py`: schema capability, cooldown/jobs/attempts/maintenance output.
- `tests/test_db.py`: atomic migration, ordering backfill, object/index postconditions.

### New Test Coverage

- Canonical DB input and message-position invariants. (FR#3-FR#6)
- Fenced job/runtime/provider state transitions. (FR#9-FR#13)
- Packet/process ownership crash windows and non-overlap. (FR#24)
- Per-input retry budget/exhaustion. (FR#25)
- Quarantine capacity and safe maintenance. (FR#26)
- Eligibility audit scenarios and shared-query parity. (FR#14)
- POSIX cleanup and quarantine maintenance. (FR#16-FR#18)
- Stable-denominator lifecycle accounting. (FR#19)
- Schema interruption/read-only diagnosis. (FR#20-FR#21)
- Large-history retention and query-budget behavior. (FR#22)
- Continued-session invalidation with embedding preservation. (FR#23)

### Tests to Remove

- Capability sidecar/fingerprint/smoke-test and `--check-capability` tests.
- JSONL source readiness, packet coverage, source path/file evidence, and citation tests for recap generation.
- Exact v1 continuation-section and broad `--force` tests.
- Stop-triggered automatic generation tests.

## Documentation Updates

- `README.md`: Session Recaps, DB-only data flow, opt-in SessionEnd behavior, Sonnet default, platform support, status/recovery/cooldown/retention, and removed capability/source concepts.
- CLI help: selectors, targeted retry, overrides, recovery, reset-health, maintenance, accounting, and unsupported-platform messages.
- `skills/ccr-recall/SKILL.md`: recap as orientation, not authoritative evidence.
- `skills/ccr-resume/SKILL.md`: tail/pending questions remain authoritative for continuation.
- `design/specs/008-llm-summary-enrichment/`: retain history and add a superseded pointer.
- `design/specs/010-session-recaps/evaluation.md`: commit aggregate methodology/results only.

## Impact

### Changed Files

- create `src/ccrecall/recap_input.py`: canonical DB projection, serialization, and hash.
- create `src/ccrecall/recap_eligibility.py`: shared policy/measures/reasons/queries.
- create `src/ccrecall/recap_state.py`: jobs, attempts, runtime lease, provider health, accounting, and maintenance transitions.
- modify `src/ccrecall/summary_enrichment.py`: v2 contract/rendering and input-hash freshness.
- modify `src/ccrecall/llm_summarizer.py`: DB-packet prompt/invocation, classification, and POSIX cleanup; remove source/capability code.
- modify `src/ccrecall/hooks/backfill_llm_summaries.py`: queue-backed selectors, retries, overrides, and accounting.
- modify `src/ccrecall/hooks/sync_current.py`: reusable final sync and no Stop recap spawn.
- create `src/ccrecall/hooks/session_end.py`: clear handoff plus durable job upsert.
- create `src/ccrecall/hooks/drain_session_recaps.py`: serialized final sync/eligibility/input/provider/materialization orchestration.
- modify `src/ccrecall/parsing.py`, `branch_ops.py`, and message-link writes: preserve and persist active-path positions.
- modify `src/ccrecall/hooks/session_selection.py`, `context_rendering.py`, and `search_hydrate.py`: v2 materialized hash/render/preview.
- modify `src/ccrecall/status.py`: read-only recap schema/platform/provider/job/attempt status.
- modify `src/ccrecall/cli/commands.py`: recap recovery/health/maintenance and retry controls.
- modify `src/ccrecall/config.py`: Sonnet/default controls, opt-in false, and cooldown/lease policy.
- modify `src/ccrecall/llm_summary_db.py`: sole atomic recap migration entry point, schema postcondition checks, and no-migrate hook connection.
- modify `src/ccrecall/db.py`: continue delegating main connections to `llm_summary_db` migration authority.
- do not add recap claim objects to `src/ccrecall/schema.py` baseline DDL in this migration.
- modify `hooks/hooks.json` and `pyproject.toml`: SessionEnd/drainer direct entry points and compatibility worker.
- modify affected tests and documentation listed above.

<!-- Gap check 2026-08-12: branch_messages.position dependencies included in T02 — tests/test_parsing.py, tests/test_import_pipeline.py, tests/test_session_ops.py, tests/test_integration.py, tests/test_summarizer.py, tests/test_backfill_embeddings.py, tests/test_backfill_tool_content.py, tests/test_recent_chats.py, tests/test_sync_hook.py, tests/test_status.py, and tests/test_context_alerts.py contain direct two-column inserts, link comparisons, or ordering assumptions. README.md, CHANGELOG.md, tests/test_llm_summary_evaluation.py, and design/specs/008-llm-summary-enrichment references are owned by T10. -->

### Behavioral Invariants

- Hook stdout and hot-path import constraints remain unchanged.
- Automatic recaps remain explicitly opt-in and detached.
- Deterministic summaries remain available whenever recap data is absent, stale, malformed, legacy, unsupported, pending, or failed.
- One active branch per session remains enforced.
- Existing search ranking, embeddings, chunks, vectors, FTS, and session selection semantics do not change.
- Existing v1 data remains physically preserved and never renders as v2.
- Per-run settings never mutate configuration.
- Transcript content remains user-local and is transmitted only through the explicitly enabled constrained Claude CLI invocation.

### Blast Radius

The change crosses imported branch-path ordering, hook orchestration, detached processes, persistent SQLite state, provider invocation, rendering/search hydration, CLI/status, migration, docs, and extensive tests. It removes recap dependence on JSONL/source files and does not alter JSONL decoding, extracted message content/membership, deterministic summary behavior, embedding/search algorithms, external APIs, or package dependencies. Users who never opt in continue to receive deterministic context without provider invocation.

## Open Questions

None.
