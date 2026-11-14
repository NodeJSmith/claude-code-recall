---
name: ccr-recall
user-invocable: true
description: >
  Use when the user asks to recall, search, or continue past conversations, or
  when answering needs something from a past session. Triggers on "what did we
  discuss", "continue where we left off", "remember when", "as I mentioned",
  "you suggested", "we decided", "search my conversations", "find the
  conversation where", "what did we work on". Also triggers on implicit signals
  like past-tense references ("the bug we fixed"), possessives without context
  ("my project"), or assumptive questions ("do you remember"). Use this instead
  of grepping transcript files under ~/.claude/projects.
---

# Recall

Answer the user's question from past Claude Code sessions, and say where the answer came from: which session, which project, and when. ccrecall keeps every session in a searchable database. Search that rather than reading or grepping the transcript JSONL under `~/.claude/projects`. The JSONL is large and escaped, and Claude Code deletes old transcripts on a schedule, while ccrecall keeps their messages.

## Commands

- `ccrecall search "<query>"` returns ranked session cards. Each card has a score, project, branch, date, topic, counts, and a handle. Ranking fuses keyword matches with semantic similarity.
- `ccrecall search-messages "<query>"` returns matched exchanges: short user and assistant excerpts, with the session handle and exchange index. It's semantic only, so an empty result can mean the vector index is unavailable. Fall back to `search` then.
- `ccrecall recent` prints the newest sessions with their conversation.
- `ccrecall recent --session <handle>` prints a session found by either search in full, from the database. `ccrecall tail <handle>` prints only its ending: the last instruction, last events, and any question it left unanswered. It reads the transcript file, so it fails for a session whose transcript has been deleted.

To pick up where the previous session in this project left off, run `ccrecall tail` with no argument. If that session ended on a question, the user still has to answer it. Ask it again instead of choosing an answer yourself.

Narrow with `--project`, `--path` (a cwd substring, such as a worktree name), and `--after`/`--before`. The searches take `--max-results` and `recent` takes `--limit`. Add `--json` for structured output. `ccrecall <command> --help` covers the rest. A `null` score isn't an error. It means the results gave nothing to rank against, as when there's only one.

## Searching

Query with words that set the session you want apart from the rest: names, error text, file paths, issue numbers, the technology involved. Words like "discussed", "decided", or "yesterday" appear in nearly every session and bury the match. If the first search misses, change the terms or add a filter before concluding. The current session is indexed too, so it can show up in results. Don't cite it as a past session.

## Answering

- Answer what was asked. A "what did we decide about X" question gets the decision and where it came from, not a report.
- A search that found nothing doesn't prove it never happened. Say what you searched for. If coverage looks thin, `ccrecall status` shows whether the index is behind.
- A recalled decision or file state was true then. Before acting on it, check the code as it is now.
