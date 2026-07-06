#!/usr/bin/env python3
"""Load previous session context from the memory database for the SessionStart hook.

Selection Algorithm (startup):
  Exclude current session, find most recent substantive (>2 exchanges)
  plus recent short sessions (2 exchanges) in remaining slots.

Selection Algorithm (clear):
  Read handoff file written by SessionEnd hook to hard-link to the exact
  cleared-from session. If not substantive (≤2 exchanges), append most
  recent substantive as supplementary. Falls through to startup logic
  if handoff file is missing, stale, or session not in DB.

Output: JSON with hookSpecificOutput for context injection
"""

import contextlib
import json
import sys
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ccrecall.config import (
    get_db_path,
    load_settings,
    log_hook_exception,
    setup_logging,
)
from ccrecall.db import get_connection
from ccrecall.formatting import get_project_key
from ccrecall.hooks.context_alerts import proactive_alert_block
from ccrecall.hooks.context_rendering import (
    build_context,
    build_origin_block,
    pending_question_block,
)
from ccrecall.hooks.session_selection import select_sessions
from ccrecall.models import HookInput

if TYPE_CHECKING:
    import sqlite3

# Rough chars-per-token ratio for the injected-context size estimate logged
# below. Not model-exact — just enough signal to spot an outsized injection.
_CHARS_PER_TOKEN_ESTIMATE = 4


def _emit_empty() -> None:
    """Print the empty SessionStart response (inject no context)."""
    print(json.dumps({}))


def _emit_with_proactive(proactive_block: str) -> None:
    """Emit hook output containing only the proactive alert block (no session context).

    Falls back to _emit_empty() when there is no proactive block to inject.
    Hook stdout must never contain bare text — only the JSON envelope.
    """
    if not proactive_block:
        _emit_empty()
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": proactive_block,
                }
            }
        )
    )


def main():
    settings = load_settings()
    logger = setup_logging(settings, process_name="context")

    raw = sys.stdin.read()
    try:
        hook_input = HookInput.model_validate_json(raw) if raw else HookInput()
    except ValidationError:
        hook_input = HookInput()

    cwd = hook_input.cwd
    session_id = hook_input.session_id
    # Default only when absent (None), mirroring the old .get("source", "startup");
    # an explicit "" must stay "" so the source gate below still rejects it.
    source = hook_input.source if hook_input.source is not None else "startup"

    # Only SessionStart events get proactive alerts or context injection.
    if source not in ("startup", "clear"):
        _emit_empty()
        return

    # ── DB connection — opened early for the DB probe ──────────────────────────
    # We attempt the connection here so probe_db has a live conn to work with.
    # A connection failure (dir/WAL unwritable) leaves conn=None; probe_db(None)
    # correctly classifies that as a persist fault.
    # The db_path.exists() guard prevents creating a fresh DB on a first-run
    # install where the DB hasn't been initialised yet (not a fault condition).
    # ExitStack (rather than a plain `with`) because this function has several
    # early returns before the connection's natural end-of-function close point;
    # entering get_connection() via the stack defers its commit/rollback/close
    # to whichever return statement fires, without duplicating the with-block
    # at every gate.
    db_path = get_db_path(settings)
    db_available = db_path.exists()
    conn: sqlite3.Connection | None = None
    with contextlib.ExitStack() as stack:
        if db_available:
            try:
                conn = stack.enter_context(get_connection(settings))
            except Exception:
                # conn stays None; DB probe will report this as a persist fault.
                logger.warning("DB connection failed — DB probe will report fault")

        # ── Proactive alert evaluation ──────────────────────────────────────────
        # Must run before ALL early-return gates so alerts fire even when sessions
        # is empty or the DB is inaccessible.
        proactive_block = proactive_alert_block(settings, conn, db_available)

        # ── Gate: context injection disabled ─────────────────────────────────────
        if not settings.get("auto_inject_context", True):
            logger.info("Context injection disabled by settings")
            _emit_with_proactive(proactive_block)
            return

        # ── Gate: must have cwd + session_id to inject session context ──────────
        if not cwd or not session_id:
            _emit_with_proactive(proactive_block)
            return

        # ── Gate: DB must exist for context injection ───────────────────────────
        if not db_available:
            _emit_with_proactive(proactive_block)
            return

        # ── Gate: DB connection must be open for context injection ──────────────
        if conn is None:
            # Connection failed earlier; proactive alert already captures this fault.
            _emit_with_proactive(proactive_block)
            return

        # ── Context injection ────────────────────────────────────────────────────
        try:
            project_key = get_project_key(cwd)
            max_sessions = settings.get("max_context_sessions", 2)
            sessions = select_sessions(
                conn,
                project_key,
                session_id,
                max_sessions,
                source=source,
                db_path=db_path,
                cwd=cwd,
            )

            if not sessions:
                _emit_with_proactive(proactive_block)
                return

            context = build_context(sessions)
            if not context:
                _emit_with_proactive(proactive_block)
                return

            logger.info("Injecting context from %s session(s) for project %s", len(sessions), project_key)

            # Top-of-context directive: placed first because the hook's inline
            # preview may be truncated by the harness, and because earlier tokens
            # receive more attention. Tells Claude how to read the rest of this
            # injection and when to reach for the persisted file or recall skill.
            directive = (
                "## How To Use This Context\n"
                "- Sessions below are ordered most-recent first, and within each session "
                "the most recent exchanges come first. Read top-down to get the freshest "
                "context before older context.\n"
                "- If this hook's output was truncated inline and a persisted file path "
                "is referenced, Read that file before answering any message that references "
                "prior work — the last exchanges of the previous session may live only there.\n"
                "- For anything beyond the sessions shown here, use the "
                "`recall-conversations` skill rather than guessing."
            )

            # Assemble: directive + proactive (if any) + origin + pending + context.
            # The directive is first (it tells Claude how to read the rest); the
            # proactive block immediately follows it, ahead of origin / pending /
            # prior-session content (highest-attention position for the alert).
            origin = build_origin_block(source, sessions)
            pending = pending_question_block(sessions, cwd)
            if proactive_block:
                full_context = f"{directive}\n\n{proactive_block}\n\n{origin}\n\n{pending}{context}"
            else:
                full_context = f"{directive}\n\n{origin}\n\n{pending}{context}"

            logger.info(
                "Injected context: ~%d tokens (%d chars)",
                len(full_context) // _CHARS_PER_TOKEN_ESTIMATE,
                len(full_context),
            )

            output = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": full_context,
                }
            }
            print(json.dumps(output))

        except Exception:
            log_hook_exception("context")
            # Don't block session start on errors; proactive alert (if any) still surfaces.
            _emit_with_proactive(proactive_block)
            sys.exit(0)


if __name__ == "__main__":
    main()
