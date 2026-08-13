import json
import os
import signal
import subprocess
import sys
import time

mode = sys.argv[2] if len(sys.argv) > 2 else "ignore-term"
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen(
    [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump([os.getpid(), grandchild.pid], handle)
if mode == "normal-exit":
    print('{"summary":"ok"}', flush=True)
else:
    while True:
        time.sleep(1)
