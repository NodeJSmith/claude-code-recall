"""Read-only ground truth for the ccrecall state audit. Opens the DB with mode=ro."""

import collections
import json
import sqlite3
import sys
from pathlib import Path

from whenever import Instant

from ccrecall import ingestion_status
from ccrecall.ingestion_status import STALE_TAIL_SECONDS, classify_sessions

db = sys.argv[1]
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
cur = conn.cursor()
index_fn = ingestion_status.import_log_source_index
sources = index_fn(cur)

missing_all = [u for u, p in sources.items() if p["missing"]]
in_sessions = [u for u in missing_all if cur.execute("SELECT 1 FROM sessions WHERE uuid=?", (u,)).fetchone()]
print("sessions with import_log rows whose source file is gone:", len(in_sessions))

cats = collections.Counter()
rows = []
for uuid, cat, _, missing, _ in classify_sessions(cur, sources, Instant.now(), STALE_TAIL_SECONDS):
    cats[cat] += 1
    if cat in ("stale_tail", "ingestion_gap", "pending_tail", "missing_source"):
        paths = sources[uuid]["existing"]
        mtime = max((p.stat().st_mtime for p in paths if p.exists()), default=0)
        rows.append(
            {
                "uuid": uuid[:8],
                "cat": cat,
                "missing": len(missing),
                "files": len(paths),
                "age_days": round((Instant.now().timestamp() - mtime) / 86400, 1),
                "project": paths[0].parent.name[:60] if paths else "",
            }
        )
print("classified:", dict(cats))
for cat in ("pending_tail", "stale_tail", "ingestion_gap", "missing_source"):
    sel = [r for r in rows if r["cat"] == cat]
    if not sel:
        continue
    ages = sorted(r["age_days"] for r in sel)
    print(
        f"\n{cat}: n={len(sel)} turns={sum(r['missing'] for r in sel)} "
        f"age_days min={ages[0]} median={ages[len(ages) // 2]} max={ages[-1]} "
        f"multi_file={sum(r['files'] > 1 for r in sel)}"
    )
    by_proj = collections.Counter(r["project"] for r in sel)
    for proj, n in by_proj.most_common(6):
        print(f"   {n:4d} {proj}")
    for r in sorted(sel, key=lambda r: -r["missing"])[:4]:
        print("   top:", json.dumps(r))

# Transcripts on disk the DB does not know about at all.
projects = Path.home() / ".claude" / "projects"
known = {r[0] for r in cur.execute("SELECT file_path FROM import_log")}
top = [p for p in projects.glob("*/*.jsonl")]
sub = [p for p in projects.glob("*/*/subagents/*.jsonl")]
unknown = [p for p in top if str(p) not in known]
print(f"\ntop-level transcripts on disk={len(top)} subagent files={len(sub)} import_log rows={len(known)}")
print(f"top-level transcripts NOT in import_log: {len(unknown)}")
empties = sum(p.stat().st_size == 0 for p in unknown)
print(f"   of which zero bytes: {empties}")
by_proj = collections.Counter(p.parent.name[:60] for p in unknown)
for proj, n in by_proj.most_common(8):
    print(f"   {n:4d} {proj}")
now = Instant.now().timestamp()
print("   modified in last 24h:", sum(now - p.stat().st_mtime < 86400 for p in unknown))

print("\nexchange vs chunk totals (active branches):")
for row in cur.execute("SELECT SUM(b.exchange_count), (SELECT COUNT(*) FROM chunks) FROM branches b WHERE is_active=1"):
    print("   sum(exchange_count), chunks =", row)

print("\nbranches not embedded:")
for row in cur.execute(
    """SELECT substr(s.uuid,1,8), b.embedding_version, b.exchange_count, b.ended_at,
       (SELECT COUNT(*) FROM chunks c WHERE c.branch_id=b.id),
       (SELECT COUNT(*) FROM branch_messages bm WHERE bm.branch_id=b.id),
       length(b.aggregated_content)
       FROM branches b JOIN sessions s ON s.id=b.session_id
       WHERE b.is_active=1 AND (b.embedding_version IS NULL OR b.embedding_version < 3)
       ORDER BY b.ended_at DESC"""
):
    print("  ", row)

print("\ndraft branches (cap_tokens=4096):")
for row in cur.execute(
    """SELECT substr(s.uuid,1,8), COUNT(*), MAX(c.timestamp), b.embedding_version
       FROM chunks c JOIN branches b ON b.id=c.branch_id JOIN sessions s ON s.id=b.session_id
       WHERE c.cap_tokens=4096 GROUP BY b.id ORDER BY 3 DESC"""
):
    print("  ", row)
