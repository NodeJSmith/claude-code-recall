"""Read-only: characterise branches stamped embedded that have fewer chunks than exchanges."""

import collections
import sqlite3
import sys

from ccrecall.db_vec import fetch_branch_messages
from ccrecall.embeddings import EMBEDDING_VERSION
from ccrecall.import_log_ops import import_log_source_index
from ccrecall.summarizer import build_exchange_pairs

conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
cur = conn.cursor()
gone = {u for u, p in import_log_source_index(cur).items() if p["missing"]}
rows = cur.execute(
    "SELECT b.id, s.uuid, b.ended_at FROM branches b JOIN sessions s ON s.id=b.session_id "
    "WHERE b.is_active=1 AND b.embedding_version=?",
    (EMBEDDING_VERSION,),
).fetchall()
months = collections.Counter()
t = collections.Counter()
ex = []
for bid, u, end in rows:
    pairs = build_exchange_pairs(fetch_branch_messages(cur, bid, False))
    idx = {r[0] for r in cur.execute("SELECT exchange_index FROM chunks WHERE branch_id=?", (bid,))}
    miss = [p["index"] for p in pairs if p["index"] not in idx]
    if not miss:
        continue
    t["branches"] += 1
    t["exchanges"] += len(miss)
    t["zero_chunks"] += not idx
    t["transcript_gone"] += u in gone
    t["only_last_exchange"] += miss == [len(pairs) - 1]
    months[(end or "?")[:7]] += 1
    if len(ex) < 400:
        ex.append((u[:8], len(pairs), len(idx), (end or "")[:10]))
print(dict(t))
print(sorted(months.items()))
print(sorted(ex, key=lambda e: e[2] - e[1])[:6])
