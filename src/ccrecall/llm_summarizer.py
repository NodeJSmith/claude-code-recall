"""DB-packet-only, background Claude provider boundary."""

import json
import os
import signal
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccrecall.config import PID_FILE_MODE
from ccrecall.process_cleanup import posix_process_groups_supported, process_group_absent, process_start_identity
from ccrecall.summary_enrichment import CLAUDE_RESPONSE_SCHEMA, validate_claude_response_body

STATUS_OK = "ok"
STATUS_INVALID_OUTPUT = "invalid_output"
STATUS_ERROR = "error"
STATUS_CLAUDE_UNAVAILABLE = "claude_unavailable"
STATUS_AUTH_REQUIRED = "auth_required"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_BUDGET_EXCEEDED = "budget_exceeded"
STATUS_TIMEOUT = "timeout"
STATUS_UNSUPPORTED_CLI = "unsupported_cli"
STATUS_PLATFORM_UNSUPPORTED = "platform_unsupported"
STATUS_CLEANUP_FAILED = "cleanup_failed"
DIAGNOSTIC_CAP = 240
TERM_GRACE_SECONDS = 5

CLAUDE_SECURITY_ARGV = (
    "--safe-mode",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--tools",
    "Read",
    "--allowedTools",
    "Read",
    "--permission-mode",
    "dontAsk",
)


@dataclass(frozen=True)
class InvocationResult:
    status: str
    response_body: dict[str, Any] | None = None
    diagnostic: str | None = None
    process_id: int | None = None
    process_group_id: int | None = None
    process_started_at: str | None = None


def _cap(text: str | None) -> str | None:
    return text[:DIAGNOSTIC_CAP] if text else None


def build_prompt(packet_path: Path) -> str:
    return "\n".join(
        (
            "You are ccrecall-summary-enricher. Produce a factual recap of one conversation branch.",
            f"Read the canonical DB packet at {packet_path}.",
            "The packet is the complete authority for this recap.",
            "summary is required: state the recognizable work arc and evidenced outcome.",
            "title is optional: use a short, evidence-backed label only when useful.",
            "outcome is optional and must be completed, partial, blocked, or unknown.",
            "Do not give advice, continuation plans, next steps, source references, or an exhaustive chronology.",
            "Return only the response body matching the response schema.",
        )
    )


def build_claude_argv(packet_path: Path, settings: dict[str, Any]) -> list[str]:
    return [
        "claude",
        "-p",
        *CLAUDE_SECURITY_ARGV,
        "--append-system-prompt",
        "You summarize only the supplied canonical DB packet.",
        "--add-dir",
        str(packet_path.parent),
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(CLAUDE_RESPONSE_SCHEMA, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        "--max-budget-usd",
        str(settings["llm_summary_max_budget_usd"]),
        "--model",
        settings["llm_summary_model"],
        "--effort",
        settings["llm_summary_effort"],
        "--no-session-persistence",
        build_prompt(packet_path),
    ]


def _packet_cleanup_metadata(packet_path: Path, packet_nonce: str | None) -> dict[str, Any]:
    try:
        byte_size = packet_path.stat().st_size
    except OSError:
        byte_size = None
    return {"packet_path": str(packet_path), "packet_nonce": packet_nonce, "byte_size": byte_size}


def _remove_unwritten_packet(packet_path: Path, temporary: str | None) -> bool:
    removed = remove_packet(packet_path)
    if temporary is None:
        return removed
    try:
        Path(temporary).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return removed


def write_packet(
    packet_path: Path,
    packet: bytes,
    *,
    admit_launch: Any | None = None,
    persist_write_failure: Any | None = None,
    packet_nonce: str | None = None,
) -> bool:
    """Write an already-owned canonical packet without recording its content elsewhere.

    ``persist_write_failure`` receives only cleanup state and packet metadata. It
    must cancel a reservation after ``verified_removed`` or quarantine a packet
    whose removal cannot be proved; no provider can be launched from this path.
    """
    if admit_launch is not None and not admit_launch():
        # No path was created, but the caller may already hold a fenced provider
        # reservation. Release it through the same content-free cancellation seam.
        if persist_write_failure is not None:
            _persist_cleanup(
                persist_write_failure,
                "verified_removed",
                _packet_cleanup_metadata(packet_path, packet_nonce),
            )
        return False
    temporary = None
    fd = None
    try:
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        packet_path.parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{packet_path.name}.", dir=packet_path.parent)
        os.fchmod(fd, PID_FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(packet)
        Path(temporary).replace(packet_path)
    except Exception:
        if fd is not None:
            with suppress(OSError):
                os.close(fd)
        removed = _remove_unwritten_packet(packet_path, temporary)
        if persist_write_failure is not None:
            _persist_cleanup(
                persist_write_failure,
                "verified_removed" if removed else "uncertain",
                _packet_cleanup_metadata(packet_path, packet_nonce),
            )
        return False
    return True


def remove_packet(packet_path: Path) -> bool:
    try:
        packet_path.unlink()
        with_exception = False
    except FileNotFoundError:
        with_exception = False
    except OSError:
        with_exception = True
    if with_exception:
        return False
    with suppress(OSError):
        packet_path.parent.rmdir()
    return True


def _classify_process_failure(stderr: str, stdout: str) -> tuple[str, str | None]:
    text = "\n".join(bit for bit in (stderr, stdout) if bit).lower()
    if "unknown option" in text or "unrecognized option" in text or "usage:" in text:
        return STATUS_UNSUPPORTED_CLI, _cap("unsupported_cli")
    if "rate limit" in text:
        return STATUS_RATE_LIMITED, _cap("rate_limited")
    if "budget" in text:
        return STATUS_BUDGET_EXCEEDED, _cap("budget_exceeded")
    if any(word in text for word in ("authenticate", "login", "log in", "sign in")):
        return STATUS_AUTH_REQUIRED, _cap("auth_required")
    return STATUS_ERROR, _cap("provider_error")


def _parse_stdout(stdout: str) -> dict[str, Any]:
    data = json.loads(stdout)
    if not isinstance(data, dict):
        raise ValueError("response is not an object")
    body = data.get("structured_output", data)
    return validate_claude_response_body(body)


def _wait_for_group_absence(group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if process_group_absent(group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _terminate_group(process: subprocess.Popen[str], group_id: int, grace_seconds: float) -> bool:
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    if _wait_for_group_absence(group_id, grace_seconds):
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=grace_seconds)
        return True
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    if _wait_for_group_absence(group_id, grace_seconds):
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=grace_seconds)
        return True
    return False


def _cleanup_metadata(
    packet_path: Path, packet_nonce: str | None, process_id: int, group_id: int, started_at: str | None
) -> dict[str, Any]:
    try:
        byte_size = packet_path.stat().st_size
    except OSError:
        byte_size = None
    return {
        "packet_path": str(packet_path),
        "packet_nonce": packet_nonce,
        "byte_size": byte_size,
        "process_id": process_id,
        "process_group_id": group_id,
        "process_started_at": started_at,
    }


def _persist_cleanup(callback: Any, state: str, metadata: dict[str, Any]) -> None:
    """Cleanup bookkeeping must never turn a conservative result into an exception."""
    with suppress(Exception):
        callback(state, metadata)


def invoke_claude(
    packet_path: Path,
    settings: dict[str, Any],
    *,
    persist_launch: Any,
    persist_cleanup: Any,
    admit_launch: Any | None = None,
    packet_nonce: str | None = None,
    popen: Any = subprocess.Popen,
    platform_supported: bool | None = None,
    grace_seconds: float = TERM_GRACE_SECONDS,
) -> InvocationResult:
    """Invoke one owned packet and prove its group is reaped before normal completion.

    ``admit_launch`` is the T08-wired quarantine-capacity callback and must approve
    before spawn. ``persist_cleanup`` receives content-free packet and process
    metadata for every uncertain cleanup; T08 persists it to quarantine.
    """
    if platform_supported is None:
        platform_supported = posix_process_groups_supported()
    if not platform_supported:
        return InvocationResult(STATUS_PLATFORM_UNSUPPORTED, diagnostic=STATUS_PLATFORM_UNSUPPORTED)
    try:
        admitted = admit_launch is None or bool(admit_launch())
    except Exception:
        admitted = False
    if not admitted:
        deleted = remove_packet(packet_path)
        _persist_cleanup(
            persist_cleanup,
            "verified_removed" if deleted else "uncertain",
            _packet_cleanup_metadata(packet_path, packet_nonce),
        )
        return InvocationResult(STATUS_CLEANUP_FAILED, diagnostic=STATUS_CLEANUP_FAILED)
    try:
        process = popen(
            build_claude_argv(packet_path, settings),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=packet_path.parent,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError) as exc:
        deleted = remove_packet(packet_path)
        _persist_cleanup(
            persist_cleanup,
            "verified_removed" if deleted else "uncertain",
            _packet_cleanup_metadata(packet_path, packet_nonce),
        )
        if isinstance(exc, FileNotFoundError):
            return InvocationResult(STATUS_CLAUDE_UNAVAILABLE, diagnostic=STATUS_CLAUDE_UNAVAILABLE)
        return InvocationResult(STATUS_ERROR, diagnostic="provider_error")

    started_at = process_start_identity(process.pid)
    try:
        group_id = os.getpgid(process.pid)
        persisted = (
            started_at is not None
            and group_id == process.pid
            and bool(persist_launch(process.pid, group_id, started_at))
        )
    except Exception:
        persisted = False
        group_id = process.pid
    if not persisted:
        reaped = _terminate_group(process, group_id, grace_seconds)
        _persist_cleanup(
            persist_cleanup,
            "reaped" if reaped else "uncertain",
            _cleanup_metadata(packet_path, packet_nonce, process.pid, group_id, started_at),
        )
        return InvocationResult(STATUS_CLEANUP_FAILED, diagnostic=STATUS_CLEANUP_FAILED)

    try:
        stdout, stderr = process.communicate(timeout=settings["llm_summary_timeout_seconds"])
    except subprocess.TimeoutExpired:
        reaped = _terminate_group(process, group_id, grace_seconds)
        deleted = remove_packet(packet_path) if reaped else False
        _persist_cleanup(
            persist_cleanup,
            "verified_removed" if reaped and deleted else "uncertain",
            _cleanup_metadata(packet_path, packet_nonce, process.pid, group_id, started_at),
        )
        return InvocationResult(
            STATUS_TIMEOUT if reaped and deleted else STATUS_CLEANUP_FAILED,
            diagnostic=STATUS_TIMEOUT if reaped and deleted else STATUS_CLEANUP_FAILED,
            process_id=process.pid,
            process_group_id=group_id,
            process_started_at=started_at,
        )

    if not process_group_absent(group_id) and not _terminate_group(process, group_id, grace_seconds):
        _persist_cleanup(
            persist_cleanup,
            "uncertain",
            _cleanup_metadata(packet_path, packet_nonce, process.pid, group_id, started_at),
        )
        return InvocationResult(STATUS_CLEANUP_FAILED, diagnostic=STATUS_CLEANUP_FAILED)

    deleted = remove_packet(packet_path)
    _persist_cleanup(
        persist_cleanup,
        "verified_removed" if deleted else "uncertain",
        _cleanup_metadata(packet_path, packet_nonce, process.pid, group_id, started_at),
    )
    if not deleted:
        return InvocationResult(STATUS_CLEANUP_FAILED, diagnostic=STATUS_CLEANUP_FAILED)
    if process.returncode != 0:
        status, diagnostic = _classify_process_failure(stderr, stdout)
        return InvocationResult(status, diagnostic=diagnostic)
    try:
        return InvocationResult(STATUS_OK, response_body=_parse_stdout(stdout))
    except (TypeError, ValueError, json.JSONDecodeError):
        return InvocationResult(STATUS_INVALID_OUTPUT, diagnostic=STATUS_INVALID_OUTPUT)
