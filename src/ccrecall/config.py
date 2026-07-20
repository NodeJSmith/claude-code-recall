"""Paths, config/settings loading, PID files, and logging.

Zero heavy dependencies — no fastembed, onnxruntime, or sqlite_vec. This is
the module hooks that don't touch the DB (health checks, handoff writers,
lightweight probes) import instead of pulling the full db.py stack onto the
hook hot path.
"""

import contextlib
import json
import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ccrecall.models import LOGGER_NAME

# Claude Code config directory — respects CLAUDE_CONFIG_DIR when set.
CLAUDE_CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")

# Default paths
RUNTIME_DIR = Path.home() / ".ccrecall"
DEFAULT_DB_PATH = RUNTIME_DIR / "conversations.db"
DEFAULT_LOG_PATH = RUNTIME_DIR / "ccrecall.log"
CONFIG_PATH = RUNTIME_DIR / "config.json"

# Claude Code transcript directory
DEFAULT_PROJECTS_DIR = CLAUDE_CONFIG_DIR / "projects"

# Hook filenames/prefixes — writer and reader live in different modules and must agree.
CLEAR_HANDOFF_FILENAME = "clear-handoff.json"
SYNC_TEMP_PREFIX = "ccrecall-sync-"

# Default settings. Every key here is user-overridable from config.json —
# load_settings() merges any of these present in the file over the defaults.
# (db_path is deliberately absent: it is not a config key. The CLI --db flag
# injects it into the settings dict, which get_db_path reads; without it,
# get_db_path falls back to DEFAULT_DB_PATH. The settings dict thus doubles as
# the transport for that programmatic override.)
DEFAULT_SETTINGS = {
    "auto_inject_context": True,
    "max_context_sessions": 2,
    "exclude_projects": [],
    "logging_enabled": True,
    "log_level": "INFO",
    "alert_snooze_hours": 24,
}

# Rotating memory-log handler sizing.
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 2

# PID sentinel file permissions: owner read/write only.
PID_FILE_MODE = 0o600


def ensure_parent_dir(path: Path) -> None:
    """Create ``path``'s parent directory (idempotent) before writing to it.

    Centralizes the mkdir flags for runtime-dir writers, any of which may be the
    first to run on a fresh machine before ~/.ccrecall/ exists. Takes a path
    rather than hardcoding the runtime dir so a settings-overridden db_path
    materializes under its own parent, not the default dir.
    """
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write ``data`` as JSON to ``path`` (tempfile + replace + cleanup).

    The single implementation of the runtime-dir atomic-write pattern (config.json,
    the embedding-status sidecar, the snooze ledger). Ensures the parent dir exists, writes via
    a temp file in the same directory, and replaces in one step so a concurrent reader
    never sees a partial file. Removes the temp file on any error before re-raising.
    """
    ensure_parent_dir(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        Path(tmp).replace(path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def pid_file_path(pid_key: str) -> Path:
    """Path to a background job's PID sentinel (lives in the runtime dir, beside the DB)."""
    return RUNTIME_DIR / f".pid-{pid_key}"


def remove_pid_file(pid_key: str) -> None:
    """Delete a job's PID sentinel so the next session can spawn again (best-effort)."""
    with contextlib.suppress(OSError):
        pid_file_path(pid_key).unlink(missing_ok=True)


def load_config() -> dict:
    """Read ~/.ccrecall/config.json. Returns empty dict on missing/malformed config."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        result = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # OSError = read failure; ValueError = malformed JSON (JSONDecodeError /
        # UnicodeDecodeError). A real bug surfaces instead of masking as "no config".
        return {}
    return result if isinstance(result, dict) else {}


def load_settings() -> dict:
    """Return settings with config.json overrides merged on top of defaults."""
    settings = DEFAULT_SETTINGS.copy()
    config = load_config()
    for key in DEFAULT_SETTINGS:
        if key in config:
            settings[key] = config[key]
    return settings


def get_db_path(settings: dict | None = None) -> Path:
    """Get database path from settings or default."""
    if settings and "db_path" in settings:
        return Path(settings["db_path"]).expanduser()
    return DEFAULT_DB_PATH


def setup_logging(
    settings: dict | None = None,
    process_name: str = "ccrecall",
    verbose: bool = False,
) -> logging.Logger:
    """Set up logging with rotation. Returns a null logger if logging is disabled.

    Each process type gets its own rotating log file
    (``RUNTIME_DIR/ccrecall-<process_name>.log``) so concurrent processes never
    race on the same file's rotation. ``process_name`` defaults to the bare
    ``"ccrecall"`` process name for callers that don't identify themselves.

    When ``verbose`` is True, a StreamHandler writing to stderr is added so log
    lines appear on the terminal as well as in the file.
    """
    logger = logging.getLogger(LOGGER_NAME)
    for h in logger.handlers:
        h.close()
    logger.handlers.clear()

    if not settings or not settings.get("logging_enabled", True):
        logger.addHandler(logging.NullHandler())
        return logger

    log_path = RUNTIME_DIR / f"ccrecall-{process_name}.log"
    ensure_parent_dir(log_path)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if verbose:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
        logger.setLevel(logging.DEBUG)
    else:
        level_name = settings.get("log_level", "INFO")
        logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))

    return logger


def log_hook_exception(context: str) -> None:
    """Best-effort: route the active exception to the memory log without ever raising.

    Top-level hook guards must never crash the session, but a bare ``except: pass``
    also hides every failure. This logs the in-flight exception (a no-op unless
    logging_enabled) while suppressing any error from logging itself, so the guard
    stays crash-proof and failures become observable when logging is turned on.
    """
    with contextlib.suppress(Exception):
        setup_logging(load_settings(), process_name=context).exception("%s hook failed", context)
