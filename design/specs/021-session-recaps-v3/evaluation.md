# Recap Probe Evaluation

**Date:** 2026-11-15

Aggregate evidence from a private local probe. No transcripts, recap text, prompts, identifiers, or per-session mappings are committed; the review material stayed outside the repository.

## Method

Sessions were chosen whose last Claude Code `away_summary` was current (no user or assistant entry after it). For each, a recap was generated with `claude-sonnet-5-5` through the Claude Agent SDK (one turn, no tools, no settings sources), from input built out of ccrecall's imported SQLite rows (user and assistant prose plus `tool_content`, in timestamp order, up to the recap's timestamp), with Claude Code's own recap prompt used verbatim. A second `claude-sonnet-5-5` call judged each generated recap against the `away_summary` on meaning: same goal, same next step (each `same` / `partial` / `different`), and whether a user would recognize the session from it. The judge's notes on every pair were read by hand; they were accurate except one case where it called two equivalent phrasings an inversion.

## Run 1: 20 sessions, 9 repos, 10 compacted

| Generated | Goal same | Next same | Recognizable | Under 400 chars |
|---:|---:|---:|---:|---:|
| 20/20 | 17 | 15 | 19 | 20 |

No outright wrong recaps. The three real misses: two compacted sessions where the generated recap described earlier work instead of where the session ended, and one uncompacted session with a wrong follow-up step.

## Run 2: 15 fresh sessions (none from run 1), 9 compacted, four variants

| Variant | Goal same | Next same | Next different | Recognizable | Relative cost |
|---|---:|---:|---:|---:|---:|
| Full input, harness prompt | 14 | 10 | 2 | 15 | 1.00 |
| Full input, "recap where the session ended" hint added | 13 | 10 | 2 | 15 | 1.03 |
| Post-compaction input (compaction summary + turns after the boundary), harness prompt | 14 | 11 | 1 | 15 | 0.65 |
| Both | 13 | 11 | 2 | 15 | 0.68 |

Rerunning run 1's two compacted misses with post-compaction input: one went from "different goal, not recognizable" to "same goal, recognizable"; the other became recognizable but still omitted one of two changes.

## Run 3: input shape (prose only)

Size inventory of the largest stored sessions: `<task-notification>` rows (subagent reports) were 46–79% of the input in notification-heavy sessions, and `tool_content` (mostly Bash command text) was 15–54%. `_collapse_consecutive_tool_markers` removed nothing from either. User prose was 0–4%.

- **One large compacted session, four shapes, two runs each, judged against its last current `away_summary`:** unshaped input (344k characters); notifications cut to their summary line with per-message caps and 120-character tool lines (214k); the same with 600 characters of notification result (231k); and the first without tool lines (134k). Every recap named the right next step. Only the unshaped input produced a recap describing finished work as still in progress. The prose-only shape cost 30% of the unshaped input per uncached call.
- **Five notification-heavy sessions, prose only (user and assistant `content`, notifications and `tool_content` dropped; post-compaction for the two compacted ones), judged by hand:** inputs of 12k–29k characters, against 180k–235k unshaped. Four had a reference recap. All four matched it on goal and next step, and the one whose reference was stale described the later work correctly. The fifth matched the session's final reply. Each cost $0.02–0.05.
- **Size bound:** across all 9,088 stored sessions, the largest prose-only branch is 137k characters (p99 52k, p95 29k), and compaction summaries in a month of transcripts run up to 17k.

## Run 4: compacted sessions, post-compaction vs whole branch (prose only)

Eight compacted sessions across six repos, each ending on a current `away_summary`, judged as in runs 1 and 2. All three inputs were prose only.

| Input | Goal same | Next same | Recognizable | Relative cost |
|---|---:|---:|---:|---:|
| Post-compaction (compaction summary + turns after the boundary) | 7 | 7 | 8 | 0.71 |
| Whole branch | 7 | 6 | 8 | 1.00 |
| First user turn + most recent turns to 30k characters | 6 | 6 | 8 | 0.59 |

Whole-branch inputs were 25k–66k characters. The whole branch never drifted to earlier work; the tight head-plus-tail input did once, pulled there by the session's opening goal. The one next-step difference between post-compaction and whole branch was a partial match, not a wrong step.

## Decisions this supports

- Imported SQLite is a sufficient recap input (consistent with `../010-session-recaps/evaluation.md`).
- Post-compaction input was adopted on runs 1–2 (inputs with `tool_content`), then dropped after run 4: with prose-only input the whole branch recaps as well, so storing compaction facts isn't worth the schema change.
- The prompt hint is dropped: no gain, and it once dropped the session's overall goal.
- `claude-sonnet-5-5` with the harness prompt met the 400-character cap on every generated recap in both runs. Calls run one at a time took about 3 s each; latency is not a design constraint because generation is unattended.
- Each SDK call is a headless Claude Code session. Without `--no-session-persistence`, each one is saved as a transcript containing its whole input, which ccrecall then imports. The probe produced 146 such sessions, which had to be purged by hand.
- Generation input is user and assistant prose only, with no per-message caps and a 400,000-character whole-input cap (run 3).
