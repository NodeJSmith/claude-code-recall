"""Does busy_timeout protect a deferred transaction that reads, then writes after another writer commits?"""

import sqlite3
import tempfile
import time
from pathlib import Path

path = Path(tempfile.mkdtemp()) / "t.db"
setup = sqlite3.connect(path)
setup.execute("PRAGMA journal_mode=WAL")
setup.execute("CREATE TABLE t (x)")
setup.execute("INSERT INTO t VALUES (1)")
setup.commit()


def attempt(begin):
    a = sqlite3.connect(path, isolation_level=None, timeout=0)
    a.execute("PRAGMA busy_timeout = 5000")
    a.execute(begin)
    a.execute("SELECT * FROM t").fetchall()
    if begin == "BEGIN":
        setup.execute("INSERT INTO t VALUES (2)")
        setup.commit()
    start = time.monotonic()
    try:
        a.execute("UPDATE t SET x = 3")
        result = "write ok"
    except sqlite3.OperationalError as e:
        result = f"{e}"
    a.execute("ROLLBACK")
    a.close()
    print(f"{begin:16s} {result:22s} after {time.monotonic() - start:.3f}s (busy_timeout 5s)")


attempt("BEGIN")
attempt("BEGIN IMMEDIATE")
