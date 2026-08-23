---
topic: "embedding memory management for large texts"
date: 2026-08-23
status: Draft
---

# Prior Art: Embedding Memory Management for Large Texts

## The Problem

When embedding text for semantic search, documents that approach or exceed the embedding model's context window (typically 512-8192 tokens) create memory spikes during inference — particularly with models whose attention mechanism scales quadratically with sequence length. For ONNX-based local inference (as opposed to API calls to OpenAI/Cohere), the spike is borne by the local process and can freeze the machine.

## How We Do It Today

ccrecall uses exchange-based chunking (one user turn + its assistant replies = one chunk), capped at 8192 tokens via a head+tail strategy that preserves both ends of the text. An attention-budget-based batch planner (`EMBED_BATCH_ATTENTION_BUDGET = MODEL_TOKEN_LIMIT²`) groups texts by length and bounds per-batch cost. `enable_cpu_mem_arena=False` ensures onnxruntime returns transient workspace to the OS after each call. The core issue is that a single exchange can legitimately hit 8192 tokens, triggering ~8.4 GB of transient attention workspace from the unfused ALiBi attention in jina-v2-small's ONNX export.

## Patterns Found

### Pattern 1: Token-Bounded Chunking with Boundary Awareness

**Used by**: LangChain (`RecursiveCharacterTextSplitter`), LlamaIndex (`SentenceSplitter`), OpenAI Cookbook, Weaviate.

**How it works**: Text is split into pieces well under the embedding model's context window — typically **400-1024 tokens** — at natural boundaries (sentence/paragraph breaks), with 10-20% overlap between chunks. This is the universal default across the RAG ecosystem.

**Strengths**: No content lost; smaller chunks improve retrieval precision (more focused vectors); individual embedding calls stay cheap and bounded.

**Weaknesses**: Multiplies embedding calls and stored vectors per source document; requires tuning chunk size per corpus; doesn't by itself bound per-batch memory if many chunks arrive in one batch.

**Example**: https://developers.llamaindex.ai/python/framework/module_guides/loading/node_parsers/modules/

### Pattern 2: Hard Truncation at the Model's Max Token Length

**Used by**: OpenAI Cookbook (as "simplest option"), ChromaDB (`WithTruncation` flag).

**How it works**: Input tokenized, anything beyond max is discarded. `encoding.encode(text)[:max_tokens]`.

**Strengths**: Trivial, bounded, predictable.

**Weaknesses**: Silently loses information beyond the truncation point. Explicitly called the lower-quality option by OpenAI's own docs.

**Example**: https://developers.openai.com/cookbook/examples/embedding_long_inputs

### Pattern 3: Length-Sorted / Bucketed Dynamic Batching

**Used by**: General ML-serving practice; ccrecall already does this with `EMBED_BATCH_ATTENTION_BUDGET`.

**How it works**: Inputs grouped by similar length before batching, batch size chosen dynamically so attention-area cost stays under a budget. Since attention is quadratic in sequence length, an attention-area budget (length²) is more accurate than a token-count budget.

**Strengths**: Directly targets the actual memory driver; orthogonal to chunking strategy.

**Weaknesses**: Requires empirical measurement of the specific model export's memory behavior.

**Example**: https://introl.com/blog/embedding-infrastructure-scale-vector-generation-production-guide-2025

### Pattern 4: Deferred / Background Embedding via Queue

**Used by**: Supabase (`pgmq` + `pg_cron`), general vector-DB guidance.

**How it works**: Write path enqueues an embedding job; a separate worker drains the queue in batches. Decouples write latency from embedding cost and isolates memory spikes to the background worker.

**Strengths**: Write path never blocks on embedding; crash isolation; batches sized for throughput.

**Weaknesses**: Search is stale until the worker catches up; doesn't solve within-batch memory bounding.

**Example**: https://supabase.com/blog/automatic-embeddings

### Pattern 5: Two-Tier Quality Embedding (Draft Now, Full Later)

**Used by**: **No source found.** This pattern does not exist as a documented, named practice in the surveyed literature.

**How it works**: (Hypothetical) Embed with a cheaper/faster/lower-quality pass on ingest, then re-embed with a higher-quality pass in the background.

**Gap note**: The closest analogues found were compute-tier separation (GPU bulk vs CPU interactive — a resource split, not a quality split) and lazy re-embedding during model-version migrations (a one-time transition, not a standing architecture). No tool or production system documents a deliberate "draft quality now, full quality later" embedding strategy.

### Pattern 6 (Adjacent): Lazy Re-Embedding on Model Migration

**Used by**: General RAG-ops guidance.

**How it works**: When switching embedding models, old vectors are served while documents are gradually re-embedded in the background. Structurally similar to what a draft/full tiered strategy would need (watermark tracking, background sweep), but framed as a migration mechanism, not a standing architecture.

## Anti-Patterns

- **Naive fixed-size batching without length awareness.** Padding to the batch's longest member wastes 20-40% compute/memory. ccrecall already avoids this.
- **Assuming `enable_cpu_mem_arena=False` fully solves ONNX Runtime memory growth.** Multiple GitHub issues report it doesn't reliably reclaim memory in every configuration. Treat as one mitigation among several.
- **Silent truncation as a default.** The ecosystem treats data loss as requiring explicit opt-in, not a hidden default.

## Emerging Trends

- **Late chunking** — embed the full document with a long-context model first (preserving cross-chunk context), then split token representations into retrieval chunks afterward.
- **Attention-area budget batching** (length² not count) appearing as the standard cost model for bounding peak memory — consistent with ccrecall's existing `EMBED_BATCH_ATTENTION_BUDGET`.

## Relevance to Us

The most striking finding: **the entire ecosystem keeps chunks small (400-1024 tokens)**, well under their models' max context windows. ccrecall's exchange-based chunking allows up to 8192 tokens per exchange — 8-20x larger than standard practice. This isn't necessarily wrong (conversation exchanges are a natural semantic unit, and the head+tail cap preserves both ends), but it means ccrecall is operating in a regime no other tool targets.

No tool uses a "draft now, full later" tiered quality strategy. This is a novel approach with no prior art to validate or warn against. The closest pattern — deferred/background embedding — simply skips embedding on the write path entirely and defers all of it.

ccrecall's existing attention-budget batching is already aligned with the most sophisticated production practice (Pattern 3). The gap is in per-call peak, not per-batch composition.

## Recommendation

The prior art strongly suggests that **smaller chunks are the industry solution to this problem**, not caps or tiers. Every major framework uses 400-1024 token chunks and treats the model's max context window as a hard limit, not a target.

For ccrecall, the exchange-based chunking unit is a deliberate design choice (semantic coherence of a full exchange), so switching to 512-token arbitrary chunks isn't appropriate. But a 4096-token sync-path cap is still 4-10x larger than what the rest of the ecosystem uses for chunking — it's generous, not restrictive. The head+tail cap strategy already preserves both ends of long exchanges, so the quality loss from a lower cap on the sync path is mitigated by design.

The "no prior art for draft/full tiering" finding cuts both ways: it means the dual-cap approach is novel (unvalidated), but also that nobody has found it necessary — because they chunk small enough that per-call peaks are never a problem in the first place.

## Sources

### Reference implementations
- https://developers.llamaindex.ai/python/framework/module_guides/loading/node_parsers/modules/ — LlamaIndex SentenceSplitter defaults
- https://docs.langchain.com/oss/python/integrations/splitters — LangChain text splitters
- https://github.com/chroma-core/chroma/issues/1049 — ChromaDB batch submission limits

### Blog posts & writeups
- https://weaviate.io/blog/chunking-strategies-for-rag — Weaviate chunking strategies
- https://www.firecrawl.dev/blog/best-chunking-strategies-rag — Chunking strategy evaluation
- https://introl.com/blog/embedding-infrastructure-scale-vector-generation-production-guide-2025 — Production embedding guide
- https://supabase.com/blog/automatic-embeddings — Supabase deferred embedding architecture

### Documentation & standards
- https://developers.openai.com/cookbook/examples/embedding_long_inputs — OpenAI Cookbook on long inputs
- https://github.com/microsoft/onnxruntime/issues/5711 — ONNX Runtime memory arena behavior
- https://github.com/microsoft/onnxruntime/discussions/18013 — ONNX Runtime memory discussions
