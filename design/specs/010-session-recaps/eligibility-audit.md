# Eligibility Audit

## Purpose

This artifact freezes the de-identified policy evidence for Session Recaps.
The local review bundle, sample-selection query output, labels, and any
transcript-derived material remain outside the repository.

## Method

The reviewer drew a reproducible stratified sample from active imported session
branches in the local SQLite database. Sampling used stable sorted branch order
within each stratum and a fixed local review worksheet. The worksheet exposed
only the measures below plus locally rendered conversation context. Labels were
`meaningful`, `not meaningful`, or `uncertain`: meaningful means a recap would
help recognize a distinct work arc; it does not imply correctness, completion,
or user satisfaction. Uncertain labels were excluded from rule scoring.

The sampled population contained 2,317 active branches. The sample contained
48 branches. Strata overlap because each records a review dimension rather than
a mutually exclusive population partition.

## Sample strata

| Stratum | Sample count |
| --- | ---: |
| One to two exchanges | 12 |
| Three to eight exchanges | 18 |
| Nine or more exchanges | 18 |
| Prose-led activity | 16 |
| Tool-led activity | 16 |
| Mixed prose and tool activity | 16 |
| File or commit evidence present | 24 |
| No file or commit evidence | 24 |
| Planning-like work | 16 |
| Implementation-like work | 16 |
| Investigation or operational work | 16 |

## Measures

All measures are derived from normalized imported state; none requires reading
source transcript files.

| Measure | Definition |
| --- | --- |
| `exchange_count` | User-led exchanges after eligible-message filtering. |
| `substantive_prose_chars` | Trimmed user and assistant prose characters after excluding blank and harness-only turns. |
| `nonrepetitive_tool_actions` | Tool actions after consecutive same-tool runs are collapsed; raw call volume is not used. |
| `file_count` | Distinct imported modified-file entries. |
| `commit_count` | Imported commit entries. |
| `elapsed_seconds` | Non-negative difference between imported start and end timestamps. |

## Labels

| Label | Count |
| --- | ---: |
| Meaningful | 29 |
| Not meaningful | 13 |
| Uncertain | 6 |

## Candidate-rule comparison

Scores exclude the six uncertain labels. Useful-session recall is prioritized;
precision is reported as a guardrail against clearly trivial sessions.

| Rule | Eligible | Useful-session recall | Precision |
| --- | ---: | ---: | ---: |
| Existing nine-exchange cutoff | 18 | 17/29 | 17/18 |
| Prose-only: two exchanges and 600 prose characters | 34 | 25/29 | 25/34 |
| Selected evidence rule | 32 | 28/29 | 28/32 |

The existing nine-exchange cutoff misses planning and focused implementation
work, so it is not retained as a fallback or secondary threshold.

## Selected policy

`ELIGIBILITY_POLICY_VERSION = 1`

Absolute prerequisites are an active branch, a current deterministic summary,
and at least one eligible imported message. Given those prerequisites, a branch
is meaningful when it has at least two exchanges and either selected prose
threshold is met, or its work-evidence threshold is met. Tool activity alone
never qualifies a branch.

| Threshold | Value |
| --- | ---: |
| Minimum exchanges | 2 |
| Selected prose threshold | 600 substantive prose characters |
| Evidence-route prose floor | 240 substantive prose characters |
| Evidence-route nonrepetitive tool actions | 3 |
| Evidence-route elapsed duration | 120 seconds |

The evidence route requires the prose floor, elapsed duration, and at least one
of: three nonrepetitive tool actions, one modified-file entry, or one commit
entry. Consecutive repeated tool calls count as one collapsed action for this
purpose.

## Reason codes

| Reason code | Meaning |
| --- | --- |
| `missing_active_branch` | No active branch is available. |
| `missing_current_summary` | The deterministic summary is absent or stale. |
| `no_eligible_messages` | No eligible imported messages remain after filtering. |
| `below_min_exchanges` | Fewer than two exchanges are available. |
| `eligible_substantive_prose` | The selected prose threshold qualifies the branch. |
| `eligible_work_evidence` | The corroborated evidence route qualifies the branch. |
| `below_meaningful_threshold` | Neither meaningfulness route qualifies the branch. |
