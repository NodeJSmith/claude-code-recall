"""Read-only: how zero-chunk branches looked in an older backup, and when they cluster.

Usage: stamped_zero_detail.py <live_db> <older_backup_db>
"""

import collections
import sqlite3
import sys

from ccrecall.db_vec import fetch_branch_messages
from ccrecall.embeddings import EMBEDDING_VERSION
from ccrecall.summarizer import build_exchange_pairs

live = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True).cursor()
aug = sqlite3.connect(f"file:{sys.argv[2]}?immutable=1", uri=True).cursor()
rows = live.execute(
    "SELECT b.id, s.uuid, b.ended_at, (SELECT COUNT(*) FROM chunks c WHERE c.branch_id=b.id) "
    "FROM branches b JOIN sessions s ON s.id=b.session_id WHERE b.is_active=1 AND b.embedding_version=?",
    (EMBEDDING_VERSION,),
).fetchall()
zero_days = collections.Counter()
partial_days = collections.Counter()
aug_state = collections.Counter()
partial_shape = collections.Counter()
for bid, uuid, ended, nchunks in rows:
    pairs = build_exchange_pairs(fetch_branch_messages(live, bid, False))
    have = {r[0] for r in live.execute("SELECT exchange_index FROM chunks WHERE branch_id=?", (bid,))}
    missing = [p["index"] for p in pairs if p["index"] not in have]
    if not missing:
        continue
    if nchunks:
        partial_days[(ended or "?")[:10]] += 1
        last_chunk = live.execute("SELECT MAX(timestamp) FROM chunks WHERE branch_id=?", (bid,)).fetchone()[0]
        first_missing_ts = pairs[missing[0]]["timestamp"]
        partial_shape[
            "missing all newer than newest chunk"
            if (first_missing_ts or "") > (last_chunk or "")
            else "missing older than newest chunk"
        ] += 1
        continue
    zero_days[(ended or "?")[:10]] += 1
    row = aug.execute(
        "SELECT b.id, b.embedding_version FROM branches b JOIN sessions s ON s.id=b.session_id "
        "WHERE s.uuid=? AND b.is_active=1",
        (uuid,),
    ).fetchone()
    if row is None:
        aug_state["not in backup"] += 1
        continue
    links = aug.execute("SELECT COUNT(*) FROM branch_messages WHERE branch_id=?", (row[0],)).fetchone()[0]
    msgs = aug.execute(
        "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.uuid=?", (uuid,)
    ).fetchone()[0]
    try:
        then_pairs = build_exchange_pairs(fetch_branch_messages(aug, row[0], False))
    except sqlite3.Error:
        then_pairs = []
    key = (
        f"backup links={'0' if links == 0 else '>0'} messages={'0' if msgs == 0 else '>0'} "
        f"pairs_then={'0' if not then_pairs else '>0'} now_pairs_same={len(then_pairs) == len(pairs)}"
    )
    aug_state[key] += 1
print("zero-chunk by ended day:", sorted(zero_days.items())[-30:])
print("partial by ended month:", sorted(collections.Counter(d[:7] for d in partial_days.elements()).items()))
print("partial shape:", dict(partial_shape))
print("zero-chunk state in backup:")
for k, v in aug_state.most_common():
    print(f"  {v:4d} {k}")
