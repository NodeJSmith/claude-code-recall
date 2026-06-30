---
name: ccr-recall
user-invocable: true
description: >
  Use when the user asks to recall, search, or continue past conversations.
  Triggers on "what did we discuss", "continue where we left off", "remember when",
  "as I mentioned", "you suggested", "we decided", "search my conversations",
  "find the conversation where", "what did we work on". Also triggers on implicit
  signals like past-tense references ("the bug we fixed"), possessives without
  context ("my project"), or assumptive questions ("do you remember").
---

## Tools

Three commands retrieve data. For full option catalogs, load `references/tool-reference.md`.

Under a plugin install this skill is invoked as `/ccrecall:ccr-recall`; under a vendored install, `/ccr-recall`.

Semantic recall is active: `ccrecall search` fuses keyword ranking with chunk-vector similarity by default.

**recent_chats.py** — retrieve recent sessions with full transcript:
```bash
ccrecall recent --n 3
```

**search_conversations.py / Entrypoint A** — search sessions, returns **ranked session cards** (compact, no transcript):
```bash
ccrecall search --query "keyword"
```
Each card carries a relevance `score` (normalized 0–1; `null` for a single result or the unranked LIKE path), `project`, `git_branch`, date, `topic`, exchange/file/commit counts, and a `handle`. The JSON envelope includes a `ranked` field (`false` on the LIKE-only fallback). To read the full session, use `ccrecall tail <handle>`.

**search_conversations.py / Entrypoint B** — search matched exchanges, returns **bounded snippets**:
```bash
ccrecall search-messages --query "keyword"
```
Each snippet carries a `(handle, exchange_index, timestamp)` locator plus bounded `user`/`assistant` excerpts. Semantically ranked by chunk distance; exits 0 with an empty result when the vector index is unavailable. No keyword fallback in this release.

**session_tail.py** — full-fetch drill-in for any handle from A or B:
```bash
ccrecall tail <handle>
```

---

## Workflow

1. **Identify the lens** from user intent:

| User Says | Lens |
|-----------|------|
| "where were we", "recap" | restore-context |
| "gaps", "struggling" | find-gaps |
| "mentor", "review process" | review-process |
| "retro", "project review" | run-retro |
| "decisions", "CLAUDE.md" | extract-decisions |
| "bad habits", "antipatterns" | find-antipatterns |

   Load `references/lenses.md` for per-lens parameters, core questions, and supplementary search patterns.

2. **Gather context** using lens-appropriate tools:
   - For recent context: `ccrecall recent --n N`
   - For session discovery: `ccrecall search --query "keywords"` — returns scored cards; triage by `score` and `topic` before tailing
   - For a specific exchange: `ccrecall search-messages --query "phrase"` — returns bounded snippets with locators
   - To open a full session: `ccrecall tail <handle>` (drill-in after A or B)

3. **Apply lens questions** to analyze the retrieved conversations.

4. **Deepen the search** if initial results are insufficient:
   - Retrieve more sessions: `--n 20` on `recent`, or `--max-results 10` on `search`
   - Search for specific terms that surfaced
   - Filter by project: `--project projectname`
   - Filter by session: `--session <uuid-prefix>` (when a specific session ID is known)
   - If 2 rounds of deepening yield no new relevant sessions, synthesize from available data.

---

## Query Construction

Search terms should be content-bearing words that discriminate between sessions — high information value words that are rare enough to rank relevant sessions above irrelevant ones. BM25 ranking (when FTS5 is available) weights rare terms higher automatically.

**Include:** specific nouns, technologies, concepts, project names, domain terms, unique phrases. More terms improve ranking precision.

**Exclude:** generic verbs ("discuss", "talk"), time markers ("yesterday"), vague nouns ("thing", "stuff"), meta-conversation words ("conversation", "chat") — these appear in nearly every session and add noise rather than signal.

**Algorithm:**
1. Extract substantive keywords from user request
2. If 0 keywords, ask for clarification ("Which project specifically?")
3. If 1+ specific terms, search with those terms; use `--project` to narrow scope

---

## Synthesis

### Principles

- **3-5 key findings**, not exhaustive lists — each specific (file paths, dates, project names) and backed by a quote or reference.
- **Make it actionable** — every finding suggests a response.

### Structure

```markdown
## [Analysis Type]: [Scope]

### Summary
[2-3 sentences]

### Findings
[Organized by whatever fits: categories, timeline, severity]

### Patterns
[Cross-cutting observations]

### Recommendations
[Actionable next steps]
```

### Length

Default: 300-500 words. Expand only when data warrants it.
