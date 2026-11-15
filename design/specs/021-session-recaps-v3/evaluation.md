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

## Decisions this supports

- Imported SQLite is a sufficient recap input (consistent with `../010-session-recaps/evaluation.md`).
- Post-compaction input is adopted: equal or better on every column and about a third cheaper.
- The prompt hint is dropped: no gain, and it once dropped the session's overall goal.
- `claude-sonnet-5-5` with the harness prompt met the 400-character cap on every generated recap in both runs. Calls run one at a time took about 3 s each; latency is not a design constraint because generation is unattended.
- Each SDK call is a headless Claude Code session. Without `--no-session-persistence`, each one is saved as a transcript containing its whole input, which ccrecall then imports. The probe produced 146 such sessions, which had to be purged by hand.
