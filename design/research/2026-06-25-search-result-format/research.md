---
topic: "container-vs-content split in search-result formatting (ccrecall two-entrypoint redesign)"
date: 2026-06-25
status: Draft
---

# Prior Art: "Find the Container" vs "Find the Content" in Search-Result Formatting

## The Problem

ccrecall surfaces past Claude Code conversations via one `ccrecall search -q` command that ranks at the
session/branch level but then **dumps the full transcript of every matched session** — ~31K tokens for just
3 results, default 5. It conflates two genuinely different jobs: (A) "find me *sessions* like X" (the
navigation / "which" question) and (B) "find me the specific *messages* about X" (the precision / "details"
question). The primary consumer is an AI agent that synthesizes a short answer for the user; a human may also
run it in a terminal. The question: how do mature search/retrieval systems split and format these two jobs?

## How We Do It Today

`search` and `recent` both rank/return at the session/branch granularity and route through
`formatting.py::format_markdown_session()`, which **unconditionally dumps every message in full** — no score,
no matched-term highlight, no snippet/excerpt. There is a global `--json` vs markdown switch, a
`MAX_SEARCH_RESULTS=10` cap (default 5), and a separate `tail` command that prints a session's raw transcript.
Crucially, every branch **already carries a precomputed structured summary** (`context_summary_json`: topic,
disposition COMPLETED/IN_PROGRESS/ABANDONED, first 2 + last 6 exchanges, a gap summary, and metadata —
files_modified, commits, tool_counts, exchange_count, timestamps) built for SessionStart injection — and
**search ignores it entirely**. The triage material is in the row; we just don't use it.

## Patterns Found

### Pattern 1: Two Explicit Output Modes on One Query (container vs content)
**Used by**: ripgrep/grep (`-l`/`--files-with-matches` vs default line output), Claude Code Grep
(`output_mode: files_with_matches | content | count`), Sourcegraph (`repo:`-only → repo list vs pattern → line matches).
**How it works**: One query, one engine; a flag selects the *shape* of the result. Container mode emits only
identifiers (filenames, session IDs) — cheap, scannable, pipeable. Content mode emits matching lines/passages
with locating metadata. A "count" mode gives matches-per-container as a pure triage signal. In agent-facing
tools the *default is the cheapest container mode*; content is opt-in.
**Strengths**: One engine, no duplicated ranking. Cheap default. Composable (container output pipes into a fetch).
**Weaknesses**: One more concept than a single command; if the default is wrong for the dominant case, every call pays a tax.
**Example**: https://code.claude.com/docs/en/tools-reference (Grep `output_mode`); https://www.mankier.com/1/rg (`-l`)

### Pattern 2: Ranked Container List + Bounded KWIC Fragments (one response, two layers)
**Used by**: Elasticsearch (`hits[]` + `_score` + opt-in `highlight`, `fragment_size`=100 × `number_of_fragments`=5),
Apache Solr (`highlighting` section keyed by doc ID, `hl.fragsize`), most full-text engines.
**How it works**: Response is a ranked list of containers, each with a relevance `_score` and source metadata.
*Separately and optionally*, a highlight layer returns the best-matching keyword-in-context fragments per
container — both fragment **size** and fragment **count** bounded by default, ordered by match density.
Snippets are *extracted*, not *dumped*: even a huge field yields only a capped set of short windows.
**Strengths**: One round-trip gives navigation + just-enough evidence. Output bounded regardless of document size.
Score enables triage. KWIC fragments are exactly what an LLM needs to decide whether to fetch more.
**Weaknesses**: Fragment extraction can clip across the relevant boundary. Needs a highlighting implementation.
**Example**: https://www.elastic.co/docs/reference/elasticsearch/rest-apis/highlighting

### Pattern 3: Two-Step Locate-Then-Fetch (return handles, fetch on demand)
**Used by**: Claude Code (`Glob → Read` / `Grep → Read`), Letta/MemGPT (`conversation_search` returns refs the
agent then reads), VS Code (Go to File → open), general RAG retrieve-then-read.
**How it works**: First call is cheap discovery returning *handles* + enough metadata/snippet to triage —
"consuming less context than reading multiple files upfront." The agent issues a *second* call to fetch full
content of only the one or few handles it wants. Expensive load is deferred until after the relevance decision.
**Strengths**: Minimizes tokens — full content loaded only for what's relevant. Keeps the agent's inference
budget clean. Naturally bounded. Agent applies judgment between steps.
**Weaknesses**: Two round-trips; the consumer must actually do step two. The discovery snippet must be good
enough to triage on. More orchestration.
**Example**: https://code.claude.com/docs/en/tools-reference ; https://github.com/letta-ai/letta

### Pattern 4: Coarse-to-Fine Two-Stage Ranking (recall → precision)
**Used by**: RAG retrieve-then-rerank (bi-encoder/BM25 → cross-encoder), candidate-generation + reranking.
**How it works**: Stage 1 cheap & high-recall — wide net for a candidate set of containers (top-100). Stage 2
expensive & high-precision — rerank only the survivors for the final top-k. Coarse answers "which containers";
fine answers "which passages within them, and in what order." Expensive pass bounded to survivors.
**Strengths**: Decouples recall from precision. Expensive compute bounded to a small set. Maps onto session-rank → message-rank.
**Weaknesses**: More moving parts. A coarse-stage miss is unrecoverable. Reranking adds latency.
**Example**: https://towardsdatascience.com/advanced-rag-retrieval-cross-encoders-reranking/

### Pattern 5: Separate Entrypoints / Vocabulary for the Two Intents
**Used by**: VS Code ("Go to File" Ctrl+P vs "Find in Files" Ctrl+Shift+F), LSP ("Go to Definition" vs "Find
References"), grep/ack/ag ("files with matches" vs "matching lines"), shell idiom (locate vs grep).
**How it works**: Instead of one overloaded box with a mode flag, two *distinct named commands* with distinct
mental models. Navigation command (Go to File / locate) reaches the one container you have in mind. Content
command (Find in Files / grep) enumerates all the places text matches. LSP adds smart defaulting: invoke "Go to
Definition" already at the declaration and it reinterprets as "Find References."
**Strengths**: Each command has a crisp single purpose and predictable output. Established vocabulary; agents
already understand the split. No mode confusion.
**Weaknesses**: More surface area. Risk of reaching for the wrong one. Some shared-engine duplication.
**Example**: https://code.visualstudio.com/docs/getstarted/tips-and-tricks ; https://go.dev/gopls/features/navigation

### Pattern 6: Bounded, Scored, Structured Chunks with Metadata (the retriever contract)
**Used by**: LlamaIndex/LangChain retrievers (`NodeWithScore`, `similarity_top_k`, min-score threshold), ES hits, mem0.
**How it works**: Retrieval returns a *list of chunks* (passages/messages), each with bounded text + locator
metadata + relevance score, capped by top-k and optionally a min-score threshold. Synthesis is a *downstream*
step, not the retriever's job. For agents this is typically structured JSON (id, score, text, metadata).
**Strengths**: Output bounded & predictable (k × fragment size). Score enables triage/threshold. Machine-consumable.
**Weaknesses**: Chunk boundaries can sever context. Fixed top-k is blunt. Consumer must synthesize.
**Example**: https://docs.llamaindex.ai/en/stable/examples/low_level/retrieval/

### Pattern 7: Default to Minimal Context, Expand by Explicit Bounded Parameter
**Used by**: grep/ripgrep (`-A`/`-B`/`-C N`, default = matching line only), ES (`fragment_size`), Solr (`hl.fragsize`, `0`=whole field).
**How it works**: Default content per result is deliberately small (grep = one line; ES/Solr ≈ 100 chars).
Expansion is opt-in and numerically capped; "whole thing" is an explicit non-default escape hatch. Matched
content is visually distinguished from context (grep `42:` for hit, `41-` for context).
**Strengths**: Cheap bounded default fits the common case. Full-document option still exists — you must ask.
**Weaknesses**: Too-tight default can clip needed context, forcing a re-query.
**Example**: http://www.gnu.org/s/grep/manual/html_node/Context-Line-Control.html

## Anti-Patterns

- **Dumping full transcripts ("context stuffing").** "Token bloat ≠ signal" — more text adds distractors,
  triggers "lost in the middle," ~4× tokens ≈ 2–3× latency, reasoning declines past ~50% of the window. This is
  *exactly* ccrecall's 31K-for-3 behavior, and it hurts answer **quality**, not just cost.
  https://www.marktechpost.com/2026/02/24/rag-vs-context-stuffing-why-selective-retrieval-is-more-efficient-and-reliable-than-dumping-all-data-into-the-prompt/
- **No relevance signal.** Mature systems always attach a score so the consumer can triage/threshold.
- **No snippets / no KWIC.** Containers with no in-context evidence of *why* they matched force a full fetch to verify.
- **Unbounded result size.** Defaults that scale with document size blow context budgets; cap fragment count × size, or top-k.
- **One overloaded mode for both intents.** Forcing "which sessions" to pay for full content conflates two jobs.

## Emerging Trends

- **Agent memory as on-demand search tools, not context injection.** Letta/MemGPT and mem0 keep history *out* of
  the window and expose it via search tools returning bounded fragments — the closest architectural analog to ccrecall.
- **Context compression / extractive summarization at retrieval time.** Newer RAG pipelines summarize each
  retrieved span to question-relevant sentences before it reaches the model — between "snippet" and "full document."

## Relevance to Us

The split the user proposed (A = "find sessions like," B = "find messages like") is the *dominant* industry
pattern, not a novel idea — it shows up as ripgrep `-l` vs default, Claude Code `Glob` vs `Grep`, VS Code "Go to
File" vs "Find in Files," and LSP "definition vs references." Two strong, ccrecall-specific implications:

1. **Entrypoint A is mostly already built — we're just not rendering it.** "Find sessions like" = a ranked list
   of sessions, each shown as its *existing* `context_summary_json` (topic, disposition, files, first/last
   exchange) + a relevance score + a handle. The precomputed summary is the perfect triage card (Pattern 2's
   "container + metadata"). Drill-in already exists: `ccrecall tail <session>` is the locate-then-fetch step
   (Pattern 3). So A is largely a *formatting* change over data we already store, plus exposing the score.

2. **Entrypoint B is the genuinely new capability.** "Find messages like" = scored *message-level* KWIC snippets
   with a locator (session id + position), top-k bounded, opt-in `--context N` window (Patterns 2, 6, 7). Today
   FTS/vector both index branch-level `aggregated_content`, so message-granular ranking/snippets don't exist yet —
   this needs message-level snippet extraction (and possibly message-level indexing) that the current schema
   doesn't provide.

The closest precedent to copy wholesale is **Claude Code's own Grep/Glob**: two tools, cheap container-level
default, content opt-in, structured + locator-bearing, two-step locate→fetch. ccrecall already has the "fetch"
leg (`tail`); the redesign is mainly (a) make A a summary-card list with scores, (b) add B as a bounded scored-snippet retriever.

## Recommendation

Adopt the **two-entrypoint split** the user proposed — it is the consensus pattern. Concretely:

- **A — session discovery** (rename/repurpose today's `search`): ranked session cards built from the existing
  `context_summary_json` + relevance score + session handle. Bounded, cheap, no transcript dump. Markdown for
  humans, structured JSON for the agent. This is the "which" answer and should be the cheap default.
- **B — message retrieval** (new): top-k scored message snippets (KWIC) with session+position locators and an
  opt-in bounded context window. This is the "details" answer. Requires message-level snippet/index work.
- **Keep `tail` as the explicit fetch leg** (locate→fetch). Stop inlining full transcripts in either A or B;
  full content becomes the deliberate, opt-in escape hatch, exactly as ES/Solr/grep treat "whole document."
- **Always attach a relevance score and bound the output** in both — the two universal invariants every mature
  system enforces and ccrecall currently violates.

Open design question to resolve next (in `/mine-define`): does B need its own message-level vector/FTS index, or
can it extract snippets on-the-fly from the branches A already surfaced (coarse-to-fine, Pattern 4)?

## Sources

*(URLs not live-verified.)*

### Reference implementations
- https://code.claude.com/docs/en/tools-reference — Claude Code Grep `output_mode` + Glob; locate→fetch workflow
- https://www.mankier.com/1/rg — ripgrep `-l`/`--files-with-matches` vs default line output
- https://github.com/letta-ai/letta — MemGPT tiered memory + `conversation_search`/`archival_memory_search` tools
- https://docs.llamaindex.ai/en/stable/examples/low_level/retrieval/ — `NodeWithScore` retriever contract, `similarity_top_k`

### Blog posts & writeups
- https://www.marktechpost.com/2026/02/24/rag-vs-context-stuffing-why-selective-retrieval-is-more-efficient-and-reliable-than-dumping-all-data-into-the-prompt/ — context-stuffing hurts quality
- https://towardsdatascience.com/advanced-rag-retrieval-cross-encoders-reranking/ — retrieve-then-rerank (coarse→fine)
- https://zeroentropy.dev/articles/biencoder-vs-crossencoder/ — bi-encoder recall vs cross-encoder precision
- https://kenhuangus.substack.com/p/how-ai-agents-actually-remember-inside — agent memory tiers as search tools
- https://mostlylucid.net/blog/reduced-rag — retrieval-time context compression

### Documentation & standards
- https://www.elastic.co/docs/reference/elasticsearch/rest-apis/highlighting — hits[] + `_score` + bounded highlight fragments
- https://solr.apache.org/guide/solr/latest/query-guide/highlighting.html — `hl.fragsize`, snippets keyed by doc, `0`=whole field
- http://www.gnu.org/s/grep/manual/html_node/Context-Line-Control.html — `-A`/`-B`/`-C` bounded context
- https://code.visualstudio.com/docs/getstarted/tips-and-tricks — Go to File vs Find in Files
- https://go.dev/gopls/features/navigation — LSP Go to Definition vs Find References
- https://www.nngroup.com/articles/progressive-disclosure/ — progressive disclosure (list → drill-in)
