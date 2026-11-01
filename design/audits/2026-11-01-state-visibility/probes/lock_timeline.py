"""Correlate one day's import failures with sync and import activity.

Usage: lock_timeline.py YYYY-MM-DD
"""

import re
import sys
from pathlib import Path

HOME = Path.home() / ".ccrecall"
TS = re.compile(rf"^({re.escape(sys.argv[1])} \d\d:\d\d:\d\d) - (\w+) - (.*)")


def secs(ts):
    h, m, s = ts[11:].split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def load(*names):
    rows = []
    for name in names:
        path = HOME / name
        if path.exists():
            for line in path.read_text(errors="replace").splitlines():
                m = TS.match(line)
                if m:
                    rows.append((secs(m[1]), m[1][11:], m[3]))
    return sorted(rows)


imports = load("ccrecall-import.log.1", "ccrecall-import.log")
syncs = load("ccrecall-sync.log.1", "ccrecall-sync.log")

# A sync's window is [end - duration, end].
windows = []
for t, _, msg in syncs:
    m = re.search(r"sync complete \| duration_s=([\d.]+)", msg)
    if m:
        windows.append((t - float(m[1]), t, float(m[1])))

fails = [r for r in imports if "uncaught exception" in r[2]]
print(f"import process failures: {len(fails)}")
long_syncs = [w for w in windows if w[2] > 5]
print(f"syncs: {len(windows)}, longer than 5s busy timeout: {len(long_syncs)}")
for t, clock, _ in fails:
    start = t - 5  # the writer waited up to the busy timeout before failing
    over = [w for w in windows if w[0] <= t and w[1] >= start]
    best = max(over, key=lambda w: w[2], default=None)
    near = [f"{c} {m[:44]}" for tt, c, m in imports if t < tt <= t + 40 and "complete" in m][:1]
    desc = f"sync overlap {best[2]:.1f}s" if best else "no sync overlap"
    print(f"  {clock}  {desc:24s} next completion: {near[0] if near else '-'}")
