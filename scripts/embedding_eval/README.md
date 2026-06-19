# Embedding-model recall eval (spec 029)

Compares candidate embedding models against the real ccrecall corpus, to decide
whether to move off bge-m3 (see `design/specs/029-.../embeddings-model-research.md`
in Claudefiles for the why).

## Status

- **bge-m3 baseline: done** — saved in `results.json` (computed on the VPS from stored
  `branch_vec` vectors).
- **Candidates pending:** `jina-v2-small-en`, `nomic-v1.5-Q`, `nomic-v1.5`.

The candidate runs were moved off the VPS — embedding 1988 docs there spiked load
and hung the box repeatedly. Run them on the gaming rig instead (plenty of RAM, no
thread cap needed).

## Portable bundle (run on the gaming rig)

These four files are self-contained — no DB, no ccrecall, no sqlite-vec, no bge-m3:

- `recall_harness.py`
- `corpus.json`     — 1988 summaries (id + text)
- `fixture.json`    — 150 query/target pairs
- `results.json`    — carries the bge-m3 baseline so the final table is complete

### Transfer (from the VPS)

```bash
# on the VPS — already tarred to:
#   /tmp/embedding-eval-bundle.tar.gz
# from the gaming rig (WSL2):
scp smithfamily:/tmp/embedding-eval-bundle.tar.gz .
tar xzf embedding-eval-bundle.tar.gz && cd embedding_eval
```

### Run (gaming rig)

```bash
uv run --with fastembed --with numpy python recall_harness.py \
    --models jina-v2-small-en,nomic-v1.5-Q,nomic-v1.5 \
    --corpus-file corpus.json
```

First run downloads the three models (~1.2 GB). No thread cap needed on the rig.
Results merge into `results.json` and print as a comparison table. Copy the final
`results.json` back so the recommendation can be written up.
