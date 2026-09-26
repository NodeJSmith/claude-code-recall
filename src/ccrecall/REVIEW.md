# REVIEW.md — src/ccrecall/

## Malformed Transcript Input
Transcript entries are undocumented Claude Code JSONL. For each new read of an
entry, does the code survive a present-but-null `message`, a non-list `content`,
a non-dict `input`, and non-str ids — or does one `.get()` chain assume a shape
and raise? Compare against the `(entry.get("message") or {})` idiom in
`src/ccrecall/tail_pending.py`.

## Hook Hot Path
Does any module imported by a hook entry point (`src/ccrecall/hooks/`,
`src/ccrecall/health.py`, `src/ccrecall/config.py`) now import, even
transitively, `db_vec.py`, `embeddings.py`, fastembed, numpy, or onnxruntime?
Does any hook path print anything to stdout besides its JSON envelope?

## Active-Branch Filtering
Does every new query over `branches` filter on `is_active = 1`, or can an old
inactive fork row leak into search results, stats, or session selection?

## Embedding Memory Bound
Does every new embedding call go through `embed_batch`/`embed_text` in
`src/ccrecall/embeddings.py`, or does one call `model.embed` directly and skip
the attention-budget planner? Does the sync path pass `SYNC_PATH_TOKEN_LIMIT`?

## Parallel Transcript Classifiers
Several modules recognize the same harness-injected shapes (skill bodies,
command wrappers, task notifications). Is a new prefix or tag check defined
once and shared (e.g. `SKILL_BODY_PREFIX` in `src/ccrecall/tail_pending.py`),
or does it duplicate a literal with different case or whitespace handling
than its sibling? `src/ccrecall/summarizer.py` has its own copy — check it.
