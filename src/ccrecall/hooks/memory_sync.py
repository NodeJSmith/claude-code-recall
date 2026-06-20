"""Stop hook - background sync for current session."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def main():
    try:
        # Read hook input from stdin
        hook_input = sys.stdin.read()

        # Write to temp file (cross-platform stdin piping to detached process is unreliable)
        # Use os.fdopen on the fd directly to avoid TOCTOU race; mkstemp already sets 0o600
        fd, tmp_path = tempfile.mkstemp(prefix="claude-memory-sync-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(hook_input)
        except Exception:
            # fd is closed by os.fdopen even on error; clean up the file
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise

        # Background the sync
        # Heterogeneous values (DEVNULL ints here, bool/int platform flags added below);
        # dict[str, Any] lets **kwargs satisfy Popen's individually-typed keyword params.
        kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen(  # noqa: S603 — spawns the project's own installed CLI, not untrusted input
                ["cm-sync-current", "--input-file", tmp_path],  # noqa: S607 — entrypoint resolved via PATH by design
                **kwargs,
            )
        except Exception:
            # Popen failed — clean up the temp file (cm-sync-current won't run to do it)
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except Exception:  # noqa: S110 — top-level hook guard: must never crash the session stop
        pass

    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
