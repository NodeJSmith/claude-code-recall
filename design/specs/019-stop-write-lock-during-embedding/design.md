# Design: Stop holding the database write lock while embedding

**Date:** 2026-11-02
**Status:** built
**Mode:** sketch
**Issues:** #232
**Prior art:** design/research/2026-11-02-sqlite-lock-during-embedding/prior-art.md

## Summary

`sync-current` and `ccrecall backfill embeddings` both run embedding inference inside an open SQLite write transaction, so every other writer waits on them and then fails with `database is locked`. Sync's hold is usually short: 4 of 8,766 logged syncs ran past import's busy timeout, though 2,328 ran past the shorter one other connections use (values in Assumed). Backfill's hold is much worse: it commits every 20 branches, and on 2026-11-02 the 45-minute run overlapped import failures from 00:31 through 01:11, covering every 5-minute timer import in the window checked.

This change runs inference with no transaction open on both paths. An embed runs in this order:

1. Read and diff, with no write transaction.
2. If any exchange needs embedding, commit the watermark clear (D3), writing 0 as `embed_branch_chunks` does today.
3. On sync only, apply the backfill check (D7).
4. Infer, with no transaction open.
5. Run the write transaction (D2).

Sync runs this after committing its message and link writes (D1). Backfill runs it once per branch and commits each branch (D4). The paths that don't infer keep today's behavior but go through step 5, so their stamps are re-checked too: a branch with no exchanges, a stamp repair, and a prune-only write. Prune-only doesn't clear the watermark.

CLAUDE.md invariant 3 is rewritten to the new boundary, per the issue, and the stale `sync_current.py:239` reference in `embed_branch_chunks`'s docstring goes with it.

**Where things live:**
- The sequence lives in `src/ccrecall/embed_ops.py`, in a reworked `embed_branch_chunks` that takes the connection so it can own its transactions, and is used by both paths. They differ in what they pass: sync uses the configured sync cap and `MAX_WRITE_PATH_EMBEDS_PER_SYNC`; backfill uses `MODEL_TOKEN_LIMIT` and no embed limit, plus D5's wider selection.
- `sync_current` looks up the branch by its session after `sync_session` commits; a session has one branch (see Assumed).
- Sync's message and link writes stay in `session_ops.py` and `branch_ops.py`; embedding leaves that call chain (D1).
- `hooks/sync_current.py` and `hooks/backfill_embeddings.py` own calling `embed_branch_chunks` and handling its failures.
- The forward-only pass (D8) lives in `select_batch_cb` in `hooks/backfill_embeddings.py`.
- The read-only backfill liveness check (D7) lives in `config.py`, beside `try_acquire_pid_file`.
- No new tables or columns.

**Out of scope:**
- Replacing the branch watermark with per-chunk completeness.
- The busy timeout values.
- Whether tool content belongs in embeddings. It makes up 36% of embedded text in a sample of 675 exchanges, so it affects inference cost, but once inference is outside the transaction it no longer affects the lock. Changing it re-embeds all history and needs a retrieval comparison first.
- New metrics. `lock_timeline.py` will overstate overlaps after this change, because `sync complete`'s `duration_s` will include time when no lock is held. The import failure count remains the outcome measure.

## Posture

- **Failure handling:** No retries. A failed embed or a lock timeout on the write is logged and leaves the branch eligible for backfill, except that backfill still marks a per-branch content error with its sentinel (D4). Lock contention follows the convention from #245: it costs one item, the next run retries it, and other database errors still abort. Each outcome that leaves a branch unembedded logs a line with the branch id: sync's backfill skip at INFO with the marker PID, and the write step's discarded-on-hash count and withheld stamp at INFO. The backfill's existing completion line adds the count of branches attempted and not updated.
- **Compatibility:** Mainly a personal tool, so internal signatures, public API, and CLI output may all change when the work calls for it. Tests move with the code. Stored data isn't expected to change, and a user's synced history must not be lost.
- **Scope:** Only what stops the lock being held during inference, on the paths the decisions below bring in. The watermark stays.
- **Rollout:** Ships directly with no flag or migration. Existing stamps stay valid.
- **Hardening:** Project defaults; no new metrics or settings. Log lines for new outcomes are under Failure handling.
- **Concurrency:** No new cross-process lock. Concurrent writers are made safe by what the write does, not by excluding each other. Duplicate inference is accepted as rare. Existing PID guards stay.
- **Altitude:** `build`; one session builds this right after the sketch, so citing files is safe.

## Decisions

### D1: Where does embedding run relative to the sync transaction?

**Situation:** `sync_session` → `_sync_branches_and_messages` → `sync_branch` calls `embed_branch_chunks` inline (`src/ccrecall/branch_ops.py:308`), after the first writes and before the commit at `get_connection` exit (`src/ccrecall/hooks/sync_current.py:195`). Only `sync_current` reaches this with `embed=True`; both import paths pass `embed=False`. Inference has to end up outside that transaction, and `sync_branch` can't simply commit partway through, since import shares it and owns its own transaction.

**Deciding factor:** fewest parameters threaded through layers, and no hidden commit inside a function shared with import.

| | A: Separate step after sync commits | B: `sync_branch` commits mid-function | C: Drop embed-on-sync; backfill only |
|---|---|---|---|
| Lock held during sync inference | No | No | No inference on sync |
| Sync chain after the change | Loses `embed`, `vec_writable`, `settings`, `sync_path_token_limit` | Keeps them, plus needs `conn` passed down | Same loss as A |
| Hidden commit in code import shares | None | Yes, guarded by `embed` | None |
| New sessions searchable by vector | Next Stop, as today | Next Stop, as today | Only after a backfill, and backfill is opt-in |
| Peers | episodic-memory: embed, then write | none found | session-indexer: separate `embed` command |
| Test churn | High: `tests/test_session_ops.py` (22 refs), `tests/test_integration.py` (7) | Low | High, plus behavior tests removed |

**Recommendation:** A, because it removes parameters and has no hidden commit, at the cost of moving tests. `sync_current.run` calls `sync_session`, commits, then calls `embed_branch_chunks` for that session's branch. A failed embed step never undoes committed messages.
**Pick B instead if** minimizing test churn matters more than the shape of the call chain. **Pick C instead if** you'd rather make a scheduled backfill the only embedder and accept vector search lagging the current session.
**Reversibility:** easy (internal structure; no stored data)
**Ratified:** Chose a separate embed step after sync commits over committing inside `sync_branch` or dropping embed-on-sync, to remove the embed parameters from the sync chain and avoid a hidden commit in code import shares, accepting higher test churn.

### D2: How does the write step detect a branch that changed during inference?

**Situation:** Once the lock is released, an import or repair can change links or message content during inference, or delete the branch (empty-session cleanup, `src/ccrecall/hooks/import_conversations.py:582`). `update_missing_tool_content` changes exchange text with no link change (`src/ccrecall/session_ops.py:128-131`), so a check on links alone misses it. A stale vector is harmless because its `content_hash` mismatches on the next diff (`src/ccrecall/embed_ops.py:150`). The real danger is a false watermark stamp. Peers don't re-check, but no peer has a document-level stamp.

**Deciding factor:** the stamp is true at commit, with no new state.

| | A: Re-read and re-hash inside the write transaction | B: Fingerprint `branch_messages`; discard if changed | C: Generation counter on `branches` |
|---|---|---|---|
| Catches content change with no link change | Yes | No; a false stamp stays possible | Only if every writer bumps it |
| Work kept when one exchange changed | All vectors whose hash still matches | None | None |
| New state | None | None | Column, migration, every writer changes |
| Lock-time cost | About 10 ms on the largest live branch (98 exchanges), including the vector-presence check | One indexed read of link ids | One column read |
| Peers | none re-check; llm and Khoj use the hash as identity | unknown | none found |

**Recommendation:** A, because it's the only option that keeps the stamp true without new state. Behavior to pin, all evaluated inside the write transaction:
- The write step runs `BEGIN IMMEDIATE` before the re-read, then re-reads, checks, writes, prunes, stamps, and commits, so `busy_timeout` waits for the lock and nothing can commit between the check and the stamp. A competing commit interleaved between the read and the first write must not produce a false stamp or a lost write. The D3 clear and the D4 sentinel don't need this: each is a single-statement write with no earlier read to upgrade from.
- A vector is written only if its exchange's current `content_hash` equals the one it was computed from.
- A chunk is current when its `content_hash` matches, its `embedding_version` and `embedding_model` are current, and a `chunk_vec` row exists. The write-skip below additionally requires `effective_cap_tokens` at least that of the vector being written; the stamp requires `MODEL_TOKEN_LIMIT`. Backfill's diff (D5) selects the same chunks this excludes; sync's diff stays narrower, per challenge finding H6 in `design/specs/001-chunk-level-embeddings/design.md`.
- A vector doesn't replace a chunk that is already current at equal or better quality, so a sync at 4096 can't downgrade a chunk backfill wrote at 8192 during inference.
- Pruning uses the fresh exchange set.
- If the branch no longer exists, nothing is written and no error is raised.
- The watermark is stamped only when, for the fresh exchanges, every chunk as it stands after the write is current at `MODEL_TOKEN_LIMIT`, as defined above.

The hash has to be computable without the tokenizer, since `_prepare_exchange_data` reaches `get_model()` through `cap_for_embedding`.
**Pick B instead if** the fingerprint were extended to message content, at which point it's A with an extra hash. **Pick C instead if** the re-read ever shows up as a measurable lock hold.
**Reversibility:** easy (no stored data)
**Ratified:** Chose re-reading and re-hashing under `BEGIN IMMEDIATE` inside the write transaction over a link fingerprint or a generation counter, to keep the stamp true with no new state and keep vectors that are still valid, accepting about 10 ms of extra lock time on the largest branch.

### D3: When does the watermark clear commit, so a crash during inference can't leave a false stamp?

**Situation:** `sync_branch` already clears the watermark when links change (`src/ccrecall/branch_ops.py:284`), and that clear commits with sync's writes. But a branch can need embedding with its links unchanged, for example after a tool-content repair. Today `embed_branch_chunks` clears it at step 5a (`src/ccrecall/embed_ops.py:316`) in the same transaction as the vectors. Once inference is outside the transaction, that clear must commit before inference starts, or a crash leaves a stamped branch with stale chunks. Backfill wouldn't select it, which breaks the issue's crash criterion.

**Deciding factor:** every crash point leaves the branch selected by backfill, with no embedding logic in the sync transaction.

| | A: Own short transaction, right before inference | B: Inside sync's transaction via a hash diff | C: Teach backfill selection to detect hash mismatches |
|---|---|---|---|
| Crash during inference | Selected | Selected | Selected, once backfill runs |
| Crash between sync commit and clear | Stamp stays false until next sync; only in the no-link-change case | No window | No window |
| Embedding logic in sync's call chain | None | Diff logic returns, undoing D1's cleanup | None |
| Backfill path covered too | Yes, same code | No | Yes |
| Cost | One extra short write, only when there's work | None extra | Re-hash every branch on each selection; `clear_false_watermarks` already takes about 2 s on 7,513 branches |
| Peers | n/a: no peer has a document-level watermark | n/a | Pattern 3 derives pending at each run, without a watermark |

**Recommendation:** A, because it covers both paths with one rule. The remaining window, a crash between sync's commit and the clear with links unchanged, needs a tool-content repair and a crash within milliseconds, and the next sync heals it. If the clear itself hits a lock timeout, inference doesn't run for that branch this time, per the failure posture.
**Pick B instead if** you want that window closed outright and accept embedding logic back in the sync chain. **Pick C instead if** the watermark is going to be replaced soon anyway.
**Reversibility:** easy
**Ratified:** Chose a short transaction that commits the clear right before inference over clearing in sync's transaction or teaching backfill to detect hash mismatches, to cover sync and backfill with one rule and keep embedding logic out of the sync chain, accepting a millisecond crash window after a tool-content repair that the next sync heals.

### D4: Is the backfill's batch transaction fixed here or in a follow-up?

**Situation:** `backfill_embeddings.run` commits every 20 branches (`src/ccrecall/hooks/backfill_embeddings.py:287`), with inference inside. The 2026-11-02 run took 45m41s for 138 branches, about 6.6 minutes of lock per batch, far past import's 30 s timeout. Import failures ran from 00:31 to 01:11 during that run. The issue's acceptance criteria name the sync path only and leave this open. It also runs nightly on this machine: six runs are logged in the last six days.

**Deciding factor:** fix the largest measured cause of the symptom, with one embed protocol.

| | A: Same change; shared entry point, commit per branch | B: Follow-up issue | C: Commit per branch; inference stays in the transaction |
|---|---|---|---|
| Worst lock hold on a backfill run | Milliseconds | Minutes per batch, unchanged | One branch's inference: seconds to minutes |
| Import failures during backfill | Gone | Unchanged | Reduced, still present on long branches |
| Embed protocols in the code | One | Two until the follow-up lands | Two |
| Diff size | Larger; rewrites `_embed_one_branch` and its tests (D5) | Smaller | Smallest |
| What abort rolls back | Nothing committed is lost; only the current branch's inference | Current batch | Current branch |
| Peers | episodic-memory's migration path, llm | obliscence holds per batch of 100 | obliscence-like, smaller batch |

**Recommendation:** A, because the evidence says this is the bigger half of the problem and B would keep the old protocol alive. Each branch commits on its own, including the content-error sentinel, which is written only after `_model_still_healthy` passes. A lock timeout on one branch's write counts as not updated, and the run continues per the failure posture. The sentinel is decided from content read before inference, so a branch whose content changes during a failing inference can carry it until a later sync finds work on that branch or its links change. An OOM kill bypasses the sentinel path, which handles only Python exceptions, so it leaves no record of the branch.
**Pick B instead if** you want this PR limited to the issue's literal criteria and are fine with nightly import failures for now. **Pick C instead if** rewriting `_embed_one_branch` turns out riskier than the lock it removes.
**Reversibility:** easy
**Ratified:** Chose fixing backfill in this change, with a shared entry point and per-branch commits, over a follow-up issue or per-branch commits with inference still in the transaction, to remove the largest measured lock hold and keep one embed protocol, accepting a larger diff that rewrites `_embed_one_branch` and its tests, a branch whose content changes during a failing inference carrying the content-error sentinel until a later sync finds work on it or its links change, and an OOM kill during backfill not recording which branch was running.

### D5: How does backfill replace stale chunks without deleting them before inference?

**Situation:** `_embed_one_branch` deletes version-stale and missing-vector chunks in a SAVEPOINT (`src/ccrecall/hooks/backfill_embeddings.py:413`), then relies on the diff seeing them as new. A failed embed rolls the delete back today. With inference outside any transaction, a delete committed before inference would lose those chunks on failure, which the issue forbids.

**Deciding factor:** a failed embed leaves existing chunks and vectors intact.

**Recommendation:** Drop the pre-delete. Backfill asks the diff to also select chunks that are version-stale, wrong-model, or missing a vector, and the write step replaces them, as `_write_embedded_chunks` already does per exchange. Sync's diff still leaves version-stale chunks alone (challenge finding H6 in `design/specs/001-chunk-level-embeddings/design.md`; `src/ccrecall/embed_ops.py:132`).
**Pick a committed delete before inference instead if** old vectors staying in `chunk_vec` during a long run caused a problem. Query-time filtering on `chunks.embedding_version` already excludes them.
**Reversibility:** easy
**Ratified:** Chose dropping the pre-delete and replacing stale chunks at write time over committing a delete before inference, so a failed embed leaves existing chunks and vectors intact, accepting old vectors staying in `chunk_vec` until their replacement commits.

### D6: How many write transactions does one branch's embed use?

**Situation:** Sync embeds at most 8 exchanges per call (`MAX_WRITE_PATH_EMBEDS_PER_SYNC`). Backfill embeds a whole branch (`max_embeds=None`), which can be many exchanges and minutes of inference. A crash or lock timeout at write time loses whatever hasn't been written.

**Deciding factor:** one atomic write with the stamp in it, unless losing work is a measured problem.

**Recommendation:** One write transaction per branch: infer everything the call selected, then write it all and evaluate the stamp together. A crash loses at most one branch's inference, which is better than today, when a backfill crash loses its whole uncommitted batch of 20. No peer writes more finely than per batch except session-indexer.
**Pick per-group writes instead if** a long branch keeps failing partway. That adds a partial-progress state to reason about, for a problem not observed so far.
**Reversibility:** easy
**Ratified:** Chose one write transaction per branch over per-group writes, to keep vectors and the stamp in one atomic write, accepting that a crash or lock timeout loses that branch's inference.

### D7: What should sync do when a backfill is running, now that the lock no longer keeps them apart?

**Situation:** Today sync's first write blocks while a backfill batch holds the lock, so the two rarely run inference at once. That separation is accidental, and this change removes it. Peak memory is about 8.4 GB for backfill at the 8192 cap plus about 4 GB for sync at 4096 (CLAUDE.md), so about 12.4 GB together. WSL has already been OOM-killed by inference on this project, and the laptop has 15 GB and the VPS 15 GB. `cli/commands.py:287` guards against a second backfill for exactly this reason.

**Deciding factor:** don't turn a lock failure into an OOM.

| | A: Sync skips inference while backfill's PID marker is live | B: Accept concurrent inference | C: Shared inference lock for sync and backfill |
|---|---|---|---|
| Worst-case peak | Backfill's peak, plus a brief overlap if backfill starts during a sync's inference | About 12.4 GB, for as long as both run | One peak only |
| New sync exchanges during backfill | Embedded at a later sync or backfill | Embedded right away | Sync skips, as in A |
| New machinery | A read-only liveness check on an existing marker | None | New marker; backfill must also handle a held lock at each branch |
| Conflicts with the concurrency posture | No | No | Yes |
| Peers | episodic-memory: losers skip | obliscence, llm, session-indexer: no lock | episodic-memory's file lock |

**Recommendation:** A, because it prevents the memory stacking with only a check of existing state. Sync still commits messages and clears the watermark first (D3), then skips inference, so the branch stays eligible. `config.py` has no read-only liveness check today, so the build adds one beside `try_acquire_pid_file`, sharing its liveness logic.
**Pick B instead if** you only ever run backfill on a machine with plenty of memory to spare. **Pick C instead if** the brief overlap in A ever causes a real OOM.
**Reversibility:** easy
**Ratified:** Chose having sync skip inference while backfill's PID marker is live over accepting concurrent inference or adding a shared lock, to avoid stacking inference memory with only a read-only check of existing state, accepting a brief overlap of up to about 12.4 GB if backfill starts during a sync's inference, later vectors for exchanges synced during a backfill, and a stale backfill marker whose PID gets reused making sync skip inference until that process exits or the marker is removed.

### D8: How does a backfill run avoid re-inferring a branch it couldn't finish?

**Situation:** Selection is `ORDER BY id LIMIT 20` with no exclusion (`src/ccrecall/hooks/backfill_embeddings.py:224-227`). With per-branch commits (D4), an unfinished branch is the lowest eligible id, so it heads every later batch and is inferred again each time, despite the "no retries" stance. If it's the last one left, the batch repeats and the stuck guard exits with `EXIT_ABORT` (`src/ccrecall/hooks/backfill_runner.py:57-72`). A lost write repeats the full inference and needs a lock wait over 30 s. A stamp withheld because a live session appended during inference repeats only the changed exchanges.

**Deciding factor:** each branch is attempted at most once per run, with the least new state.

| | A: Forward-only pass | B: Per-run exclusion set | C: Leave re-selection |
|---|---|---|---|
| Repeat inference after a lost write in one run | None | None | Once per later batch |
| Run ending with an unfinished branch | Normal completion | Normal completion | Stuck guard returns `EXIT_ABORT` |
| New state | One local integer | One local set | None |
| Matches "the next run retries it" | Yes, by construction | Yes | No, it retries inside the run |
| Branch turning eligible mid-run with a lower id | Waits for the next run | Picked up this run | Picked up this run |
| Repo precedent | None | `hooks/backfill_tool_content.py` | Today's shape |
| Ongoing cost against how often it happens | One predicate; `on_stuck` becomes unreachable but stays as a required callback | Every not-updated outcome must remember to add to the set | No code; costs minutes of inference when it happens |
| Peers | Derive pending once at run start, so a failure waits for the next run | unknown | unknown |

**Recommendation:** A, because it removes the re-selection that causes the problem instead of tracking its victims: batch selection only takes ids above the last id seen.
**Pick B instead if** you'd rather match the tool-content backfill's shape, or want branches that become eligible during a long run picked up in that run. **Pick C instead if** logs after shipping show lost writes essentially never happen and you'd rather add no code until they do.
**Reversibility:** easy
**Ratified:** Chose a forward-only pass over a per-run exclusion set or leaving re-selection in place, to attempt each branch at most once per run with one local integer, accepting that a branch turning eligible mid-run with a lower id waits for the next run.

## Assumed

- `find_all_branches` returns exactly one branch, so sync embeds one branch per session. Evidence: `src/ccrecall/parsing.py:275-296`.
- Sync's `import_log` row doesn't depend on embedding and can commit before inference: sync passes `file_hash=None`, and a stored NULL hash is never a match for import. Evidence: `src/ccrecall/import_log_ops.py:79-92`, `src/ccrecall/session_ops.py:7-15`.
- Python's default sqlite3 mode opens a transaction implicitly at the first DML, so after `conn.commit()` the connection holds no lock until the next write, and reads don't start a transaction. Evidence: no `isolation_level` set under `src/ccrecall/`; `conn.execute("BEGIN IMMEDIATE")` is already used at `src/ccrecall/hooks/import_conversations.py:349`.
- Vector connections, including sync's, backfill's, and import's, wait 30 s on a lock; other connections wait 5 s. Evidence: `src/ccrecall/db_vec.py:29,197`, `src/ccrecall/models.py:22`.
- Sync's non-embedding writes are short: syncs that do no inference finish in about 0.12 s, parsing included. Evidence: `~/.ccrecall/ccrecall-sync.log`.
- Only the sync and backfill paths run inference. Evidence: callers of `embed_branch_chunks` in `src/ccrecall/branch_ops.py:308` and `src/ccrecall/hooks/backfill_embeddings.py:435`.
- Re-reading and hashing the largest live branch costs about 4 ms; the vector-presence check brings it to the figure in D2's table. Evidence: read-only measurement on 2026-11-02 (1,120 links, 98 exchanges).

## Build

- [x] Implementation and tests committed
- [x] Docs: CLAUDE.md invariant 3 and the architecture notes; no user-facing docs change
- [x] Ship-time challenge

**Calls made during the build:**
- `updated` means stamped: a backfill branch counts as updated only when the write stamped it, so the new "not updated" count covers content errors, lock timeouts, and a branch that changed or vanished during inference. The ledger left "updated" undefined, and this is the reading that keeps `remaining` honest.
- The tokenizer runs only on exchanges selected for inference, after the D7 check. Hashing no longer needs it (D2), so a sync with nothing to embed never loads the model, and neither does a sync that yields to a backfill.
- `--json` backfill output gains `not_updated`, alongside the completion line the posture names, since both report the same run.
- A zero-exchange branch now also prunes its leftover chunks, because every path shares the write step and pruning uses the fresh exchange set (D2). Today's code left them.
- The backfill PID key moved to `config.py` as `PID_KEY_BACKFILL_EMBEDDINGS`, where that module's comment says job keys live, since `embed_ops` needs it for D7.
- Ship-time challenge, user's choice: a call with nothing to infer skips the write step when it would neither stamp nor prune, so a no-op Stop takes no second lock. This narrows the Summary's "go through step 5" to the calls that would write; every stamp is still re-checked under the lock.
- Ship-time challenge, user's choice: backfill aborts after 3 consecutive lock timeouts. This narrows the Posture's "costs one item" to a transient holder, because a stuck writer would otherwise cost about 30 s per eligible branch and still report success.
- Ship-time challenge, user's choice: `--limit` counts attempts, so it stays a ceiling on work when `updated` means stamped.
- Ship-time challenge, user's choice: every embed logs the branch before inference, which D4 had gone without, and `embed_branch_chunks` refuses an open transaction instead of committing it.
- A lock timeout on sync's embed logs a WARNING with no traceback, matching how backfill and repair report contention.
