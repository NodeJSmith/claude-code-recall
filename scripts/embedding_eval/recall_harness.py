"""Recall@k harness for the ccrecall embeddings-model decision (spec 029).

Compares candidate embedding models on the REAL corpus (the live conversations
DB) using a label-free, realistic fixture: for each sampled branch, a held-out
user utterance from that conversation is the query, and that branch's
context_summary is the one correct retrieval target. This mirrors the actual
recall task — "user types a natural question, find the right past conversation"
— without any hand-labeling or LLM-generated queries.

Baseline (bge-m3) runs through ccrecall's own embedding code (int8 ONNX, the
shipped path). Candidates run through fastembed. Run with:

    cd ~/source/claude-code-recall
    uv run --with fastembed --with numpy python scripts/embedding_eval/recall_harness.py

First run downloads the fastembed models (~1.3 GB total). Latency is irrelevant
to the decision, so no effort is spent on speed.
"""

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np

DB_PATH = Path.home() / ".claude-memory" / "conversations.db"
OUT_DIR = Path(__file__).resolve().parent
FIXTURE_PATH = OUT_DIR / "fixture.json"
RESULTS_JSON = OUT_DIR / "results.json"

ACTIVE_FILTER = (
    "is_active = 1 AND context_summary IS NOT NULL AND context_summary != ''"
)

# Candidate models. prefix_doc/prefix_query encode the per-model instruction
# convention (nomic requires search_document:/search_query:; jina does not).
FASTEMBED_MODELS = [
    {
        "key": "jina-v2-small-en",
        "name": "jinaai/jina-embeddings-v2-small-en",
        "prefix_doc": "",
        "prefix_query": "",
    },
    {
        "key": "nomic-v1.5-Q",
        "name": "nomic-ai/nomic-embed-text-v1.5-Q",
        "prefix_doc": "search_document: ",
        "prefix_query": "search_query: ",
    },
    {
        "key": "nomic-v1.5",
        "name": "nomic-ai/nomic-embed-text-v1.5",
        "prefix_doc": "search_document: ",
        "prefix_query": "search_query: ",
    },
]

MIN_QUERY_LEN = 40  # drop "yes please"-style acknowledgments — they don't identify a conversation


def load_corpus(conn: sqlite3.Connection) -> tuple[list[int], list[str]]:
    rows = conn.execute(
        f"SELECT id, context_summary FROM branches WHERE {ACTIVE_FILTER} ORDER BY id"
    ).fetchall()
    ids = [r[0] for r in rows]
    docs = [r[1] for r in rows]
    return ids, docs


def pick_query(topic: str, summary_json: dict) -> str | None:
    """Pick the most identifying held-out user utterance for a branch.

    Excludes the topic verbatim (it is echoed into context_summary, so using it
    would test prefix-matching, not semantic recall). Returns the longest
    content-bearing user message from the exchanges, or None if none qualifies.
    """
    topic_norm = (topic or "").strip()
    candidates: list[str] = []
    for key in ("first_exchanges", "last_exchanges"):
        for ex in summary_json.get(key) or []:
            u = (ex.get("user") or "").strip() if isinstance(ex, dict) else ""
            if len(u) < MIN_QUERY_LEN:
                continue
            if u == topic_norm or topic_norm.startswith(u) or u.startswith(topic_norm):
                continue
            candidates.append(u)
    if not candidates:
        return None
    # Longest = most identifying. Cap length so a giant code paste doesn't dominate.
    best = max(candidates, key=len)
    return best[:1000]


def build_fixture(conn: sqlite3.Connection, sample: int, corpus_ids: set[int]) -> list[dict]:
    rows = conn.execute(
        f"SELECT id, context_summary_json FROM branches "
        f"WHERE {ACTIVE_FILTER} AND context_summary_json IS NOT NULL "
        f"AND context_summary_json != '' ORDER BY id"
    ).fetchall()
    items: list[dict] = []
    for branch_id, raw in rows:
        if branch_id not in corpus_ids:
            continue
        try:
            j = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(j, dict):
            continue
        q = pick_query(j.get("topic", ""), j)
        if q is None:
            continue
        items.append({"target_id": branch_id, "query": q, "topic": (j.get("topic") or "")[:120]})
    # Evenly stride across the id-sorted list for topical variety, deterministic.
    if sample and len(items) > sample:
        step = len(items) / sample
        items = [items[int(i * step)] for i in range(sample)]
    return items


def l2norm(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def ranks_for(doc_emb: np.ndarray, query_emb: np.ndarray, target_idx: list[int]) -> list[int]:
    """1-based rank of each query's target document by cosine similarity."""
    doc_emb = l2norm(doc_emb)
    query_emb = l2norm(query_emb)
    sims = query_emb @ doc_emb.T  # (Q, D)
    ranks = []
    for qi, tgt in enumerate(target_idx):
        row = sims[qi]
        tgt_score = row[tgt]
        # rank = 1 + number of docs strictly more similar than the target
        rank = int(np.sum(row > tgt_score)) + 1
        ranks.append(rank)
    return ranks


def metrics(ranks: list[int]) -> dict:
    q = len(ranks)
    r1 = sum(1 for r in ranks if r <= 1) / q
    r5 = sum(1 for r in ranks if r <= 5) / q
    r10 = sum(1 for r in ranks if r <= 10) / q
    mrr = sum(1.0 / r for r in ranks) / q
    ndcg10 = sum((1.0 / math.log2(r + 1)) if r <= 10 else 0.0 for r in ranks) / q
    median = sorted(ranks)[q // 2]
    return {
        "recall@1": r1,
        "recall@5": r5,
        "recall@10": r10,
        "MRR": mrr,
        "nDCG@10": ndcg10,
        "median_rank": median,
        "queries": q,
    }


def embed_fastembed(model_cfg: dict, docs: list[str], queries: list[str]) -> tuple[np.ndarray, np.ndarray]:
    from fastembed import TextEmbedding

    # threads=1: onnxruntime otherwise grabs every core and spikes load on the
    # shared VPS. Single-thread keeps this process's load contribution at ~1.
    model = TextEmbedding(model_name=model_cfg["name"], threads=1)
    pd, pq = model_cfg["prefix_doc"], model_cfg["prefix_query"]
    doc_emb = np.array(list(model.embed([pd + d for d in docs])), dtype=np.float32)
    q_emb = np.array(list(model.embed([pq + q for q in queries])), dtype=np.float32)
    return doc_emb, q_emb


def load_stored_bge_vectors(conn: sqlite3.Connection, corpus_ids: list[int]) -> np.ndarray:
    """Read the bge-m3 doc vectors already stored in branch_vec, in corpus order.

    ccrecall embeds every branch on ingest, so the doc side of the bge-m3
    baseline is already computed — no need to re-embed 1988 texts (the
    single-threaded pass that thrashed the VPS overnight). Only queries are
    embedded live.
    """
    import sqlite_vec  # noqa: F401 - registers the extension loader

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    rows = conn.execute("SELECT branch_id, embedding FROM branch_vec").fetchall()
    by_id = {bid: np.frombuffer(blob, dtype=np.float32) for bid, blob in rows}
    missing = [b for b in corpus_ids if b not in by_id]
    if missing:
        raise RuntimeError(f"{len(missing)} corpus branches lack a stored bge-m3 vector")
    return np.vstack([by_id[b] for b in corpus_ids])


def embed_bge_m3(conn: sqlite3.Connection, corpus_ids: list[int], queries: list[str]) -> tuple[np.ndarray, np.ndarray]:
    from ccrecall import embeddings

    if not embeddings.model_available():
        raise RuntimeError("bge-m3 weights not in HF cache; baseline unavailable")
    doc_emb = load_stored_bge_vectors(conn, corpus_ids)
    q_emb = np.array([embeddings.embed_text(q) for q in queries], dtype=np.float32)
    return doc_emb, q_emb


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=150, help="number of eval queries")
    ap.add_argument("--rebuild-fixture", action="store_true", help="regenerate fixture.json")
    ap.add_argument("--models", default="all", help="comma list of model keys, or 'all'")
    ap.add_argument(
        "--corpus-file",
        type=Path,
        help="portable mode: read corpus from a JSON [{id, summary}] file instead of the "
        "live DB. Lets candidate runs execute on another machine with no DB/ccrecall/sqlite-vec. "
        "bge-m3 baseline is unavailable in this mode (it needs branch_vec) — run candidates only.",
    )
    ap.add_argument("--export-corpus", type=Path, help="dump the live-DB corpus to JSON and exit")
    args = ap.parse_args()

    chosen = args.models.split(",") if args.models != "all" else None

    if args.corpus_file:
        rows = json.loads(args.corpus_file.read_text())
        corpus_ids = [r["id"] for r in rows]
        corpus_docs = [r["summary"] for r in rows]
        conn = None
        print(f"corpus: {len(corpus_ids)} summaries from {args.corpus_file.name} (portable mode)")
        if chosen is None or "bge-m3" in chosen:
            print("bge-m3 baseline needs the live DB; run candidates only in portable mode", file=sys.stderr)
            return 1
    else:
        if not DB_PATH.exists():
            print(f"DB not found at {DB_PATH}", file=sys.stderr)
            return 1
        conn = sqlite3.connect(str(DB_PATH))
        corpus_ids, corpus_docs = load_corpus(conn)
        if args.export_corpus:
            args.export_corpus.write_text(
                json.dumps([{"id": i, "summary": d} for i, d in zip(corpus_ids, corpus_docs)])
            )
            print(f"exported {len(corpus_ids)} corpus rows -> {args.export_corpus}")
            return 0

    id_to_idx = {bid: i for i, bid in enumerate(corpus_ids)}
    print(f"corpus: {len(corpus_ids)} active summaries")

    if FIXTURE_PATH.exists() and not args.rebuild_fixture:
        fixture = json.loads(FIXTURE_PATH.read_text())
        print(f"fixture: loaded {len(fixture)} queries from {FIXTURE_PATH.name}")
    else:
        if conn is None:
            print("cannot build fixture in portable mode; ship fixture.json alongside", file=sys.stderr)
            return 1
        fixture = build_fixture(conn, args.sample, set(corpus_ids))
        FIXTURE_PATH.write_text(json.dumps(fixture, indent=2))
        print(f"fixture: built {len(fixture)} queries -> {FIXTURE_PATH.name}")

    queries = [f["query"] for f in fixture]
    target_idx = [id_to_idx[f["target_id"]] for f in fixture]

    # Preload prior results so running a subset (e.g. one model) accumulates
    # rather than clobbering an already-computed baseline.
    results: dict[str, dict] = json.loads(RESULTS_JSON.read_text()) if RESULTS_JSON.exists() else {}

    # Baseline first. Doc vectors are reused from branch_vec; only queries embed live.
    if chosen is None or "bge-m3" in chosen:
        print("embedding with bge-m3 (baseline; stored doc vectors + live queries)...", flush=True)
        try:
            de, qe = embed_bge_m3(conn, corpus_ids, queries)
            results["bge-m3"] = metrics(ranks_for(de, qe, target_idx))
            RESULTS_JSON.write_text(json.dumps(results, indent=2))  # checkpoint
            print("  done")
        except Exception as e:  # noqa: BLE001 - baseline optional, report and continue
            print(f"  bge-m3 baseline skipped: {e}")

    for cfg in FASTEMBED_MODELS:
        if chosen is not None and cfg["key"] not in chosen:
            continue
        print(f"embedding with {cfg['key']} (fastembed)...", flush=True)
        de, qe = embed_fastembed(cfg, corpus_docs, queries)
        results[cfg["key"]] = metrics(ranks_for(de, qe, target_idx))
        RESULTS_JSON.write_text(json.dumps(results, indent=2))  # checkpoint after each model
        print("  done")
    print("\n=== RESULTS ===")
    cols = ["recall@1", "recall@5", "recall@10", "MRR", "nDCG@10", "median_rank"]
    print(f"{'model':<18} " + " ".join(f"{c:>11}" for c in cols))
    for name, m in results.items():
        cells = []
        for c in cols:
            v = m[c]
            cells.append(f"{v:>11.3f}" if isinstance(v, float) else f"{v:>11}")
        print(f"{name:<18} " + " ".join(cells))
    print(f"\nwrote {RESULTS_JSON.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
