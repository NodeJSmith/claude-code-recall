---
topic: "How tools that read Claude Code transcripts handle context compaction"
date: 2026-10-31
status: Draft
---

# Prior Art: Compaction in Claude Code Transcript Indexers

## The Problem

A `compact_boundary` entry has `parentUuid: null`, so a compacted session's JSONL holds two or more disconnected `parentUuid` chains. A tool that walks parents from the latest leaf loses everything before the last compaction. Tools also have to decide what to do with rewound forks and with the synthetic `isCompactSummary` user message that follows each boundary.

## How We Do It Today

`parsing.find_all_branches` walks `parentUuid` from `select_active_leaf_entry` and stops at the boundary. `ingestion_status._expected_uuids` repeats the same walk. Neither reads `logicalParentUuid`, `compactMetadata`, or `isCompactSummary`, so the summary is indexed as an ordinary user turn.

Measured on 25,692 local transcripts: 185 have more than one root, and 199 `compact_boundary` entries all have a `logicalParentUuid` that resolves in the same file. Following it raises reachable real user turns in those 185 sessions from 1584 to 3312 of 3456, and sessions under half indexed fall from 98 to 2. Of the 133 turns still unreached, 52 are on rewound forks, 67 are `/clear` or `/compact` command lines, 8 are in truly disconnected chains, and the rest are summaries and other commands.

## Patterns Found

### Pattern 1: Leaf walk plus `logicalParentUuid` hop

**Used by**: cross-code-organizer, clog, jetbrains-cc-gui (and claude-code-log as one step).
**How it works**: Walk `parentUuid` from the current leaf. At a `compact_boundary` with a `logicalParentUuid`, jump there and continue. The loop unwinds repeated compactions naturally.
**Strengths**: Keeps fork exclusion, small diff.
**Weaknesses**: Truncates when the pointer dangles, which upstream reports for some sessions (claude-code #49996, #46603, #55818). Every peer here adds some fallback.
**Example**: https://github.com/mcpware/cross-code-organizer/blob/main/src/session-distiller.mjs

### Pattern 2: Hop with fallbacks

**Used by**: claude-code-log, jetbrains-cc-gui; proposed upstream in claude-code #55818.
**How it works**: When `logicalParentUuid` doesn't resolve, try `compactMetadata.preservedSegment` head and tail, then `preservedMessages.uuids`, or the record physically preceding the boundary. clog instead keeps the coherent suffix and treats the summary as the stand-in for the lost prefix. cross-code-organizer keeps every record.
**Strengths**: Handles real dangling and preserved-segment shapes.
**Weaknesses**: Depends on free-form metadata; the "preceding record" heuristic can attach to the wrong branch after a rewind.
**Example**: https://github.com/daaain/claude-code-log/blob/main/claude_code_log/dag.py

### Pattern 3: Linear read, ignore the graph

**Used by**: episodic-memory, engineering-notebook, cli-continues, ccusage.
**How it works**: Stream the file and keep user and assistant text, with `parentUuid` as metadata at most.
**Strengths**: Trivial; loses nothing to compaction or dangling pointers.
**Weaknesses**: Abandoned forks are indexed as live, and replayed records need a separate dedupe. No peer documents this as an accepted limitation [no source found].
**Example**: https://github.com/obra/episodic-memory/blob/main/src/parser.ts

### Pattern 4: Full graph, linearized with heuristics

**Used by**: claude-code-log.
**How it works**: Builds the whole DAG, treats same-timestamp forks as compaction replays and different-timestamp forks as rewinds, and renders rewinds as branches.
**Strengths**: Handles every observed shape.
**Weaknesses**: Over 1000 lines of heuristics, built for rendering rather than search.
**Example**: https://github.com/daaain/claude-code-log/blob/main/dev-docs/dag.md

### Pattern 5: Handling the `isCompactSummary` message

**Used by**: skip it: engineering-notebook, ccglass, uniichat, csift, ClawMem. Skip unless it is the only copy of the prefix: clog. Keep as a separate artifact: cli-continues. Indexed by omission: episodic-memory (inferred from source).
**How it works**: Peers key off `isCompactSummary`, and ClawMem also off `isVisibleInTranscriptOnly`. clog's rule depends on whether the logical parent resolved.
**Strengths**: Skipping keeps a synthetic recap out of search results and exchange counts.
**Weaknesses**: Skipping unconditionally removes the only record of the prefix when the pointer dangles.
**Example**: https://github.com/BFriedland/clog/blob/main/docs/SOURCE_FORMATS.md

## Anti-Patterns

- Trusting `logicalParentUuid` with no answer for when it dangles (claude-code #49996).
- Choosing the live leaf by file order, which Claude Code's own loader does and which can orphan real replies (claude-code #88597).
- Recursing per boundary with global re-selection, which hit a stack overflow on nested compactions (jetbrains-cc-gui #1696).

## Emerging Trends

Newer Claude Code versions write `compactMetadata.preservedSegment` and `preservedMessages`, and peers report that `logicalParentUuid` alone can miss a re-parented preserved tail. Locally, 72 of 199 boundaries carry a preserved segment and none of the measured residual is attributable to it. Anthropic's own loader skips pre-boundary content for files over 5 MB (claude-code #44772), so this upstream loss is accepted for resume, which is a different job from search.

## Relevance to Us

Pattern 1 is the smallest change to the existing walk and keeps fork exclusion. Its known weakness did not appear locally: 0 of 199 pointers dangled. The fallback ladder in Pattern 2 is machinery that local data doesn't justify. No peer surveyed unions per-root chains for an index, which is what issue #168's root-cause comment proposed.

## Recommendation

Follow `logicalParentUuid` in the existing walk, skip the compact summary when the hop resolves, and keep it when the hop dangles (clog's rule). Don't build the fallback ladder until a dangling pointer shows up in real data.

Coverage limits: basic-memory wasn't examined; ccusage and clog were read through a commit page and a docs excerpt rather than source; claude-mem doesn't index transcripts, so it isn't a peer on this question.

## Sources

URLs were not live-verified.

### Reference implementations
- https://github.com/daaain/claude-code-log/blob/main/claude_code_log/dag.py — full DAG with candidate ladder for compaction
- https://github.com/mcpware/cross-code-organizer/blob/main/src/session-distiller.mjs — leaf walk with logical hop, keep-everything fallback
- https://github.com/obra/episodic-memory/blob/main/src/parser.ts — linear read
- https://github.com/prime-radiant-inc/engineering-notebook/blob/main/src/parser.ts — linear read, skips summaries
- https://github.com/yigitkonur/cli-continues/blob/main/src/parsers/claude.ts — linear read, summary as separate note
- https://github.com/thedotmack/claude-mem/blob/main/src/shared/transcript-parser.ts — not a transcript indexer

### Blog posts & writeups
- https://piebald.ai/blog/messages-as-commits-claude-codes-git-like-dag-of-conversations — DAG model background
- https://github.com/zhukunpenglinyutong/jetbrains-cc-gui/pull/1696 — experience report on pre-compact history vanishing

### Documentation & standards
- https://github.com/BFriedland/clog/blob/main/docs/SOURCE_FORMATS.md — documented leaf-walk algorithm and summary rule
- https://github.com/daaain/claude-code-log/blob/main/dev-docs/dag.md — fork heuristics
- https://github.com/anthropics/claude-code/issues/49996 — dangling `logicalParentUuid`
- https://github.com/anthropics/claude-code/issues/55818 — proposed read-side reattachment
- https://github.com/anthropics/claude-code/issues/44772 — loader skips pre-compact content in large files
- https://github.com/anthropics/claude-code/issues/88597 — file-order leaf selection orphans replies
- https://github.com/anthropics/claude-code/issues/82509 — summary record shape
