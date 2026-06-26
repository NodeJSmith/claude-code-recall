"""Detect a pre-rename install and migrate its data into ~/.ccrecall/.

ccrecall previously shipped under a different name with its data dir at
``~/.claude-memory/``. The current package hardcodes ``~/.ccrecall/``, so an
upgrading user would otherwise strand their synced history — the SessionStart
hook would see no ``~/.ccrecall/conversations.db`` and kick off a from-scratch
re-import, abandoning every summary and embedding already computed.

This module is the single source of truth for "what does an old install look
like, and how do we carry it forward":

- ``find_legacy_db()`` is a cheap path check the SessionStart hooks call to
  decide whether to defer to migration instead of importing fresh.
- ``run_migration()`` does the one-time copy (DB + the still-meaningful config
  keys), leaving the original in place as a backup. It is spawned as a
  background ``ccrecall migrate`` so an 800 MB+ copy never blocks session start.

No embedding "fix" lives here on purpose: old-model vectors are already
neutralized by the query-time chunk-grain version/model filter
(search_conversations), chunk_vec repopulation via EMBEDDING_VERSION bump and
backfill, and build_selection's re-embed-on-mismatch clause. Migration only
relocates; semantic search rebuilds through those existing paths and the opt-in
`ccrecall backfill embeddings`.
"""

import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from ccrecall.db import (
    CONFIG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_SETTINGS,
    ensure_parent_dir,
    get_db_connection,
    remove_pid_file,
)

# Old data directories from pre-rename installs, in priority order. The live DB
# is always conversations.db; older zero-byte names (e.g. claude_memory.db) and
# the *.pre-*-backup.db snapshots are deliberately ignored.
LEGACY_DATA_DIRS = [Path.home() / ".claude-memory"]
LEGACY_DB_NAME = "conversations.db"
LEGACY_CONFIG_NAME = "config.json"

# Config keys the current ccrecall understands. A legacy config also carries
# keys from the old tool (consolidation_*) that mean nothing here — drop them so
# they don't accrete in the new config. onboarding_* is preserved so a migrated
# user is not asked to re-onboard.
PORTABLE_CONFIG_KEYS = set(DEFAULT_SETTINGS) | {"onboarding_completed", "onboarding_version"}

# PID key for the background-spawn concurrency guard. Must match the key the
# SessionStart hook spawns under so at most one migration runs at a time.
PID_KEY = "ccrecall-migrate"


def find_legacy_db() -> Path | None:
    """Return the path to a migratable legacy DB, or None.

    Qualifies only when the current ~/.ccrecall/conversations.db does NOT exist
    (never clobber a live current DB) and a non-empty conversations.db sits in a
    known old data dir. Pure path/stat checks — cheap enough for the hot path.
    """
    if DEFAULT_DB_PATH.exists():
        return None
    for old_dir in LEGACY_DATA_DIRS:
        candidate = old_dir / LEGACY_DB_NAME
        with contextlib.suppress(OSError):
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    return None


def portable_config(old: dict) -> dict:
    """Keep only the config keys the current ccrecall understands."""
    return {k: v for k, v in old.items() if k in PORTABLE_CONFIG_KEYS}


def copy_legacy_db(src: Path) -> bool:
    """Copy the legacy DB to DEFAULT_DB_PATH. Returns True if a copy was made.

    Checkpoints the source WAL into its main file first, so copying the single
    main file captures the complete database (the old tool is uninstalled, so
    nothing is writing concurrently). Copies to a temp file in the destination
    dir, then atomically renames — a crash mid-copy can never leave a truncated
    conversations.db that the next session would treat as a real, corrupt DB.
    The source is left untouched as a backup.
    """
    if DEFAULT_DB_PATH.exists():
        return False

    # Fold any uncheckpointed WAL into the main file so a plain copy is complete.
    # Best-effort: a missing/locked WAL just means there's nothing extra to fold.
    with contextlib.suppress(sqlite3.Error):
        conn = sqlite3.connect(src)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    ensure_parent_dir(DEFAULT_DB_PATH)
    fd, tmp = tempfile.mkstemp(dir=DEFAULT_DB_PATH.parent, suffix=".migrating")
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        Path(tmp).replace(DEFAULT_DB_PATH)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def copy_legacy_config(src_config: Path) -> bool:
    """Copy portable keys from a legacy config.json. Returns True if written.

    No-op when a current config already exists (never overwrite the user's live
    config) or the legacy file is absent/malformed. Atomic tmp+replace write.
    """
    if CONFIG_PATH.exists() or not src_config.exists():
        return False
    try:
        old = json.loads(src_config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(old, dict):
        return False

    kept = portable_config(old)
    ensure_parent_dir(CONFIG_PATH)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(kept, indent=2) + "\n")
        Path(tmp).replace(CONFIG_PATH)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def run_migration() -> int:
    """Carry a pre-rename install forward into ~/.ccrecall/. Idempotent.

    Copies the legacy DB and the portable config keys, then opens the new DB once
    with vec loaded so _ensure_vec_schema runs immediately: the obsolete branch_vec
    is dropped unconditionally, chunk_vec is created, and branch watermarks are reset
    to 0 so backfill re-embeds the legacy corpus at chunk grain. Safe to run twice:
    once ~/.ccrecall/conversations.db exists, find_legacy_db() returns None.
    """
    try:
        src = find_legacy_db()
        if src is None:
            print("ccrecall migrate: nothing to migrate (no legacy DB, or ~/.ccrecall is already populated).")
            return 0

        copied = copy_legacy_db(src)
        copy_legacy_config(src.parent / LEGACY_CONFIG_NAME)

        # Trigger the vec schema setup now so the migrated DB lands in a
        # consistent state rather than on the first search: branch_vec is
        # dropped unconditionally, chunk_vec is created, and branch watermarks
        # are reset to 0 so backfill re-embeds at chunk grain. Best-effort: if
        # vec can't load here, the search path handles it later all the same.
        with contextlib.suppress(sqlite3.Error, OSError):
            get_db_connection(load_vec=True).close()

        if copied:
            print(f"ccrecall migrate: copied {src} -> {DEFAULT_DB_PATH}")
            print(f"  Original left in place at {src.parent} — delete it once you've confirmed the migration.")
            print("  Semantic search rebuilds as you work; run `ccrecall backfill embeddings` to seed history now.")
        else:
            print(f"ccrecall migrate: {DEFAULT_DB_PATH} already exists; left it untouched.")
        return 0
    finally:
        remove_pid_file(PID_KEY)
