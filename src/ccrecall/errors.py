"""Structured error output for agent-friendly diagnostics.

Every error is written to stderr as a single JSON line with remediation hints,
so agents can parse it programmatically regardless of --json mode. This replaces
the former unstructured stderr text — the JSON envelope is the only error output.
"""

import json
import sys


def emit_error(
    message: str,
    *,
    code: str,
    exit_code: int,
    remediation: str | None = None,
) -> None:
    """Write a structured error to stderr and exit.

    The JSON envelope is always one line on stderr, parseable by agents even
    when stdout carries other output. The same message is printed to stderr
    as plain text for humans reading directly.

    Exit codes:
      1 — runtime error (DB missing, query failed, transient)
      2 — usage error (bad flags, missing required args)
    """
    envelope: dict[str, str | int] = {"error": message, "code": code}
    if remediation:
        envelope["remediation"] = remediation
    envelope["exit_code"] = exit_code
    print(json.dumps(envelope), file=sys.stderr)
    raise SystemExit(exit_code)


def emit_error_return(
    message: str,
    *,
    code: str,
    exit_code: int,
    remediation: str | None = None,
) -> int:
    """Write a structured error to stderr and return the exit code.

    Same as emit_error but returns instead of raising SystemExit — for
    functions that communicate exit codes via return value (e.g. session_tail).
    """
    envelope: dict[str, str | int] = {"error": message, "code": code}
    if remediation:
        envelope["remediation"] = remediation
    envelope["exit_code"] = exit_code
    print(json.dumps(envelope), file=sys.stderr)
    return exit_code
