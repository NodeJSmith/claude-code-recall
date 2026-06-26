"""Warm the fastembed model cache (detached background process, spawned by the setup hook).

Pre-warms the embedding model so the sync-current detached path never triggers an
invisible multi-minute ~120 MB download on first install or after a cache eviction.

Registered as a direct console-script entry point (not routed through the cyclopts
app) for the same reason as the other hooks: avoiding the ~1800 ms eager import of
the full CLI surface. Here the fastembed/numpy/onnxruntime load IS the goal, so
there is no hot-path concern — but keeping it a direct entry point is consistent
and avoids coupling the warm to the CLI command lifecycle.
"""

from ccrecall.db import remove_pid_file
from ccrecall.embeddings import model_available

# PID key written by _spawn_background in memory_setup (imported there as WARM_MODEL_PID_KEY)
# and removed by main() here.
PID_KEY = "ccrecall-warm-model"


def main() -> None:
    """Load (or download) the fastembed model cache, then release the PID sentinel."""
    try:
        model_available()  # downloads on cold cache; no-ops on warm; never raises
    finally:
        remove_pid_file(PID_KEY)


if __name__ == "__main__":
    main()
