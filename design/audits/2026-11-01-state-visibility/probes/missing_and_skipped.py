"""Read-only: what do missing-source sessions still hold, and which transcripts are skipped?"""

import collections
import json
import sqlite3
import sys
from pathlib import Path

from ccrecall.embeddings import EMBEDDING_VERSION
from ccrecall.import_log_ops import import_log_source_index

conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
cur = conn.cursor()
sources = import_log_source_index(cur)
gone = [u for u, p in sources.items() if p["missing"]]
msgs = emb = zero = 0
ends = []
projects_gone = collections.Counter()
for u in gone:
    row = cur.execute(
        """SELECT s.id, b.ended_at, b.embedding_version FROM sessions s
           LEFT JOIN branches b ON b.session_id=s.id AND b.is_active=1 WHERE s.uuid=?""",
        (u,),
    ).fetchone()
    if row is None:
        continue
    n = cur.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (row[0],)).fetchone()[0]
    msgs += n
    zero += n == 0
    emb += row[2] == EMBEDDING_VERSION
    if row[1]:
        ends.append(row[1][:10])
    missing_paths = sources[u]["missing"]
    projects_gone[Path(missing_paths[0]).parent.name[:55]] += 1
print(f"missing-source: messages still in DB={msgs} sessions with zero messages={zero} embedded={emb}")
ends.sort()
if ends:
    print(f"   ended_at range {ends[0]} .. {ends[-1]}, median {ends[len(ends) // 2]}")
parent_exists = sum(Path(sources[u]["missing"][0]).parent.exists() for u in gone)
print(f"   project dir still on disk: {parent_exists}/{len(gone)}")
for proj, n in projects_gone.most_common(6):
    print(f"   {n:4d} {proj}")

# Top-level transcripts not in import_log: split by whether they look excluded.
excl = json.loads((Path.home() / ".ccrecall" / "config.json").read_text()).get("exclude_projects", [])
known = {r[0] for r in cur.execute("SELECT file_path FROM import_log")}
projects = Path.home() / ".claude" / "projects"
not_excl = []
total_unknown = 0
for p in projects.glob("*/*.jsonl"):
    if str(p) in known:
        continue
    total_unknown += 1
    if not any(p.parent.name.endswith(e) for e in excl):
        not_excl.append(p)
print(f"\nnot in import_log: {total_unknown}; not explained by exclude_projects suffix: {len(not_excl)}")
for proj, n in collections.Counter(p.parent.name[:60] for p in not_excl).most_common(10):
    print(f"   {n:4d} {proj}")
for p in sorted(not_excl, key=lambda p: -p.stat().st_size)[:5]:
    print("   biggest:", p.parent.name[:45], p.name[:8], p.stat().st_size)
