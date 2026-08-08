"""Stop hook - background sync for current session."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ccrecall.config import SYNC_TEMP_PREFIX, log_hook_exception
from ccrecall.hooks.subprocess_utils import detached_popen_kwargs


def main():
    try:
        hook_input = sys.stdin.read()

        # Write to temp file (cross-platform stdin piping to detached process is unreliable)
        # Use os.fdopen on the fd directly to avoid TOCTOU race; mkstemp already sets 0o600
        fd, tmp_path = tempfile.mkstemp(prefix=SYNC_TEMP_PREFIX, suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(hook_input)
        except Exception:
            # fd is closed by os.fdopen even on error; clean up the file
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise

        try:
            subprocess.Popen(  # noqa: S603 — spawns the project's own installed CLI, not untrusted input
                ["ccrecall", "sync-current", "--input-file", tmp_path],  # noqa: S607 — entrypoint resolved via PATH by design
                **detached_popen_kwargs(),
            )
        except Exception:
            # Popen failed — clean up the temp file (ccrecall sync-current won't run to do it)
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except Exception:
        # Top-level hook guard: must never crash the session stop. Log
        # best-effort (no-op unless logging_enabled) so the failure isn't silent.
        log_hook_exception("sync")

    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
