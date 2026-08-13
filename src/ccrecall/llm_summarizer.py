"""Background-only Claude summarizer boundary."""

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from whenever import Instant

from ccrecall.config import PID_FILE_MODE, atomic_write_json
from ccrecall.content import build_tool_use_marker, extract_text_content
from ccrecall.file_hashing import transcript_file_hash
from ccrecall.parsing import extract_session_uuid, parse_all_with_uuids_and_numbers
from ccrecall.summary_enrichment import (
    CLAUDE_RESPONSE_SCHEMA,
    STATUS_AUTH_REQUIRED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CAPABILITY_UNVERIFIED,
    STATUS_CLAUDE_UNAVAILABLE,
    STATUS_ERROR,
    STATUS_INVALID_OUTPUT,
    STATUS_MISSING_SOURCE,
    STATUS_OK,
    STATUS_RATE_LIMITED,
    STATUS_SOURCE_CHANGED,
    STATUS_SOURCE_INCOMPLETE,
    STATUS_SOURCE_UNVERIFIED,
    STATUS_TIMEOUT,
    STATUS_UNSAFE_SOURCE_PATH,
    STATUS_UNSUPPORTED_CLI,
    normalize_project_file_reference,
    validate_claude_response_body,
)
from ccrecall.transcript_sources import discover_importable_transcript_files, discover_session_transcript_files

SUMMARIZER_SYSTEM_PROMPT = (
    "You are ccrecall-summary-enricher. Produce a factual recap of one Claude Code conversation branch."
)
CAPABILITY_SIDECAR_VERSION = 1
DIAGNOSTIC_CAP = 240
PACKET_DIR_PREFIX = "packet-"
MANIFEST_FILENAME = "manifest.json"
COMMON_ROOT_PATH_REFERENCES = {"Dockerfile", "Makefile", "Justfile", "LICENSE", "NOTICE", ".gitignore"}
PATH_REFERENCE_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+")
PATH_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_./@-])(?:/?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+|Dockerfile|Makefile|Justfile|LICENSE|NOTICE|\.gitignore)(?![A-Za-z0-9_./@-])"
)
AUTH_LOGIN_WORD_RE = re.compile(
    r"\b(?:auth|authenticate|authentication|login|log[ -]?in|logged[ -]?in|"
    r"sign[ -]?in|reauth(?:enticate|entication)?)\b"
)
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
CLAUDE_STATIC_OUTPUT_ARGV = (
    "--output-format",
    "json",
    "--json-schema",
)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvocationResult:
    status: str
    response_body: dict[str, Any] | None = None
    diagnostic: str | None = None


@dataclass(frozen=True)
class CapabilityCheckResult:
    status: str
    diagnostic: str | None = None


@dataclass(frozen=True)
class SourceResolution:
    status: str
    files: list[Path]
    diagnostic: str | None = None


class PacketBuildError(RuntimeError):
    def __init__(self, status: str, diagnostic: str | None = None):
        super().__init__(status if diagnostic is None else f"{status}: {diagnostic}")
        self.status = status
        self.diagnostic = diagnostic


def _cap(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:DIAGNOSTIC_CAP]


def _set_owner_only_dir(path: Path) -> None:
    path.chmod(0o700)


def _cleanup_tree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except Exception as exc:
        log.warning("cleanup failed for %s (%s)", path, exc.__class__.__name__)


def _write_owner_only_text(path: Path, text: str) -> None:
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, PID_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        Path(tmp_path).replace(path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            Path(tmp_path).unlink()
        raise


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def capability_fingerprint() -> str:
    """Return a fingerprint of the flags that constrain Claude's capabilities."""
    security_shape = _build_capability_check_argv(Path("<packet-dir>"))[1:-1]
    return hashlib.sha256("\0".join(security_shape).encode("utf-8")).hexdigest()


def read_capability_sidecar(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_capability_sidecar(
    path: Path,
    *,
    status: str,
    claude_version: str,
    fingerprint: str,
    diagnostic: str | None = None,
) -> None:
    atomic_write_json(
        path,
        {
            "version": CAPABILITY_SIDECAR_VERSION,
            "status": status,
            "claude_version": claude_version,
            "fingerprint": fingerprint,
            "checked_at": Instant.now().format_iso(),
            "diagnostic": _cap(diagnostic),
        },
    )


def verify_capability_sidecar(path: Path, *, claude_version: str, fingerprint: str) -> tuple[str, str | None]:
    data = read_capability_sidecar(path)
    if data is None:
        return STATUS_CAPABILITY_UNVERIFIED, "run --check-capability"
    if data.get("version") != CAPABILITY_SIDECAR_VERSION:
        return STATUS_CAPABILITY_UNVERIFIED, "run --check-capability"
    if data.get("claude_version") != claude_version:
        return STATUS_CAPABILITY_UNVERIFIED, "run --check-capability"
    if data.get("fingerprint") != fingerprint:
        return STATUS_CAPABILITY_UNVERIFIED, "run --check-capability"
    status = data.get("status")
    if not isinstance(status, str):
        return STATUS_CAPABILITY_UNVERIFIED, "run --check-capability"
    diagnostic = data.get("diagnostic")
    return status, diagnostic if isinstance(diagnostic, str) else None


def build_prompt(packet_dir: Path) -> str:
    return "\n".join(
        [
            ("You are ccrecall-summary-enricher. Produce a factual recap for one Claude Code conversation branch."),
            "",
            f"Branch packet directory: {packet_dir}",
            f"Branch outline path: {packet_dir / 'branch-outline.json'}",
            f"Branch transcript path: {packet_dir / 'branch-transcript.jsonl'}",
            f"Branch/session metadata path: {packet_dir / 'branch-metadata.json'}",
            f"Branch message UUID allowlist path: {packet_dir / 'allowed-uuids.txt'}",
            f"Deterministic summary path: {packet_dir / 'deterministic-summary.json'}",
            "",
            (
                "This is a concise recap of one conversation branch, not a project-wide or exhaustive "
                "chronological summary."
            ),
            "Read branch-outline.json and deterministic-summary.json first.",
            ("Use the outline to locate the evidence needed for a recognizable work arc and its evidenced outcome."),
            (
                "Read the branch transcript packet files as needed to establish evidence; do not infer facts "
                "only from filenames, tool names, or metadata."
            ),
            "Summarize only messages whose uuid appears in the allowlist.",
            "summary is required: state the recognizable work arc and only its evidenced outcome.",
            "title is optional: use a short, evidence-backed label only when useful.",
            "outcome is optional and must be one of completed, partial, blocked, or unknown when supplied.",
            "Do not give handoff instructions, advice, continuation plans, next steps, or generic recommendations.",
            "Do not invent facts, decisions, outcomes, or unsupported claims about the project as a whole.",
            "Do not provide an exhaustive chronology or sections beyond the response schema.",
            "Do not emit version, model, or generated_at; the worker adds them after validation.",
            "Return only the factual brief body matching the response schema.",
        ]
    )


def build_claude_argv(packet_dir: Path, settings: dict[str, Any], prompt: str) -> list[str]:
    return [
        "claude",
        "-p",
        *CLAUDE_SECURITY_ARGV,
        "--append-system-prompt",
        SUMMARIZER_SYSTEM_PROMPT,
        "--add-dir",
        str(packet_dir),
        *CLAUDE_STATIC_OUTPUT_ARGV,
        _canonical_json(CLAUDE_RESPONSE_SCHEMA),
        "--max-budget-usd",
        str(settings["llm_summary_max_budget_usd"]),
        "--model",
        settings["llm_summary_model"],
        "--effort",
        settings["llm_summary_effort"],
        "--no-session-persistence",
        prompt,
    ]


def _classify_process_failure(stderr: str, stdout: str) -> tuple[str, str | None]:
    text = "\n".join(bit for bit in (stderr, stdout) if bit).lower()
    if "unknown option" in text or "unrecognized option" in text or "usage:" in text:
        return STATUS_UNSUPPORTED_CLI, _cap(stderr or stdout)
    if "rate limit" in text:
        return STATUS_RATE_LIMITED, _cap(stderr or stdout)
    if "budget" in text:
        return STATUS_BUDGET_EXCEEDED, _cap(stderr or stdout)
    if AUTH_LOGIN_WORD_RE.search(text):
        return STATUS_AUTH_REQUIRED, _cap(stderr or stdout)
    if stdout:
        try:
            json.loads(stdout)
        except ValueError:
            return STATUS_INVALID_OUTPUT, _cap("invalid json on stdout")
    return STATUS_ERROR, _cap(stderr or stdout or "claude failed")


def get_claude_version(run: Any = subprocess.run) -> tuple[str | None, str | None]:
    try:
        completed = run(["claude", "--version"], capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, _cap(str(exc))
    if completed.returncode != 0:
        return None, _cap(completed.stderr.strip() or completed.stdout.strip() or "claude --version failed")
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0] if output else "unknown", None


def _parse_claude_stdout(stdout: str) -> dict[str, Any]:
    data = json.loads(stdout)
    if not isinstance(data, dict):
        raise ValueError("stdout must be a json object")
    if "structured_output" in data:
        structured_output = data.get("structured_output")
        if not isinstance(structured_output, dict):
            raise ValueError("structured_output must be a json object")
        return structured_output
    return data


def invoke_claude(
    packet_dir: Path,
    settings: dict[str, Any],
    prompt: str,
    *,
    active_branch_uuids: set[str],
    valid_file_paths: set[str],
    run: Any = subprocess.run,
) -> InvocationResult:
    argv = build_claude_argv(packet_dir, settings, prompt)
    try:
        completed = run(
            argv,
            capture_output=True,
            text=True,
            timeout=settings["llm_summary_timeout_seconds"],
            cwd=packet_dir,
        )
    except FileNotFoundError as exc:
        return InvocationResult(status=STATUS_CLAUDE_UNAVAILABLE, diagnostic=_cap(str(exc)))
    except subprocess.TimeoutExpired as exc:
        return InvocationResult(status=STATUS_TIMEOUT, diagnostic=_cap(str(exc)))

    if completed.returncode != 0:
        status, diagnostic = _classify_process_failure(completed.stderr, completed.stdout)
        return InvocationResult(status=status, diagnostic=diagnostic)

    try:
        body = _parse_claude_stdout(completed.stdout)
        validated = validate_claude_response_body(
            body,
            active_branch_uuids=active_branch_uuids,
            valid_file_paths=valid_file_paths,
        )
    except Exception as exc:
        return InvocationResult(status=STATUS_INVALID_OUTPUT, diagnostic=_cap(str(exc)))
    return InvocationResult(status=STATUS_OK, response_body=validated)


def _snapshot_importable_transcripts(projects_dir: Path) -> set[Path]:
    if not projects_dir.exists():
        return set()
    found: set[Path] = set()
    for path in discover_importable_transcript_files(projects_dir).files:
        if path.is_file() and not path.is_symlink():
            with contextlib.suppress(Exception):
                extract_session_uuid(path)
                found.add(path.resolve())
    return found


def _build_capability_check_argv(packet_dir: Path) -> list[str]:
    prompt = "Read the packet and return an empty JSON object."
    return [
        "claude",
        "-p",
        *CLAUDE_SECURITY_ARGV,
        "--add-dir",
        str(packet_dir),
        *CLAUDE_STATIC_OUTPUT_ARGV,
        _canonical_json({"type": "object", "additionalProperties": False, "properties": {}}),
        "--no-session-persistence",
        prompt,
    ]


def run_capability_check(
    settings: dict[str, Any],
    *,
    sidecar_path: Path,
    projects_dir: Path,
    claude_version: str,
    run: Any = subprocess.run,
) -> CapabilityCheckResult:
    fingerprint = capability_fingerprint()
    packet_dir = Path(tempfile.mkdtemp(prefix="capability-packet-"))
    cwd_dir = Path(tempfile.mkdtemp(prefix="capability-cwd-"))
    _set_owner_only_dir(packet_dir)
    _set_owner_only_dir(cwd_dir)
    try:
        _write_owner_only_text(packet_dir / "branch-outline.json", "[]\n")
        before = _snapshot_importable_transcripts(projects_dir)
        try:
            completed = run(
                _build_capability_check_argv(packet_dir),
                capture_output=True,
                text=True,
                timeout=settings["llm_summary_timeout_seconds"],
                cwd=cwd_dir,
            )
        except FileNotFoundError as exc:
            write_capability_sidecar(
                sidecar_path,
                status=STATUS_CLAUDE_UNAVAILABLE,
                claude_version=claude_version,
                fingerprint=fingerprint,
                diagnostic=str(exc),
            )
            return CapabilityCheckResult(status=STATUS_CLAUDE_UNAVAILABLE, diagnostic=_cap(str(exc)))
        except subprocess.TimeoutExpired as exc:
            write_capability_sidecar(
                sidecar_path,
                status=STATUS_TIMEOUT,
                claude_version=claude_version,
                fingerprint=fingerprint,
                diagnostic=str(exc),
            )
            return CapabilityCheckResult(status=STATUS_TIMEOUT, diagnostic=_cap(str(exc)))

        if completed.returncode != 0:
            status, diagnostic = _classify_process_failure(completed.stderr, completed.stdout)
            write_capability_sidecar(
                sidecar_path,
                status=status,
                claude_version=claude_version,
                fingerprint=fingerprint,
                diagnostic=diagnostic,
            )
            return CapabilityCheckResult(status=status, diagnostic=diagnostic)

        after = _snapshot_importable_transcripts(projects_dir)
        # Only a *new* transcript path counts as a leak: a --no-session-persistence failure
        # always writes a fresh session file (new UUID), never mutates an existing one. Ignoring
        # in-place changes to pre-existing transcripts avoids false positives from unrelated,
        # concurrent Claude Code sessions appending to their own transcripts during this check.
        new_paths = after - before
        if new_paths:
            diagnostic = "no-session-persistence created a new importable transcript; remove it before retrying"
            write_capability_sidecar(
                sidecar_path,
                status=STATUS_CAPABILITY_UNVERIFIED,
                claude_version=claude_version,
                fingerprint=fingerprint,
                diagnostic=diagnostic,
            )
            return CapabilityCheckResult(status=STATUS_CAPABILITY_UNVERIFIED, diagnostic=diagnostic)

        write_capability_sidecar(
            sidecar_path,
            status=STATUS_OK,
            claude_version=claude_version,
            fingerprint=fingerprint,
        )
        return CapabilityCheckResult(status=STATUS_OK)
    finally:
        _cleanup_tree(packet_dir)
        _cleanup_tree(cwd_dir)


def discover_current_session_source_files(projects_dir: Path, session_uuid: str) -> list[Path]:
    return discover_session_transcript_files(projects_dir, session_uuid).files


def resolve_current_session_source_files(projects_dir: Path, session_uuid: str) -> SourceResolution:
    discovery = discover_session_transcript_files(projects_dir, session_uuid)
    if discovery.had_matching_unsafe_path:
        return SourceResolution(status=STATUS_UNSAFE_SOURCE_PATH, files=[])
    if discovery.files:
        return SourceResolution(status=STATUS_OK, files=discovery.files)
    return SourceResolution(status=STATUS_MISSING_SOURCE, files=[])


def _historical_source_row_value(row: Mapping[str, Any] | Sequence[Any], key: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)) and len(row) > index:
        return row[index]
    return None


def resolve_historical_source_files(
    session_uuid: str, rows: list[Mapping[str, Any] | Sequence[Any]]
) -> SourceResolution:
    valid: list[Path] = []
    had_existing_mismatch = False
    had_only_unverified = False
    had_unsafe_path = False
    for row in rows:
        file_path = _historical_source_row_value(row, "file_path", 0)
        if not isinstance(file_path, str):
            continue
        path = Path(file_path)
        if extract_session_uuid(path) != session_uuid or not path.exists():
            continue
        if not path.is_file() or path.is_symlink():
            had_unsafe_path = True
            continue
        file_hash = _historical_source_row_value(row, "file_hash", 1)
        file_size = _historical_source_row_value(row, "file_size", 2)
        file_mtime = _historical_source_row_value(row, "file_mtime", 3)
        current = path.stat()
        checked_any_proof = False
        matches = True

        if file_hash is not None:
            checked_any_proof = True
            matches = matches and transcript_file_hash(path) == file_hash
        if file_size is not None:
            checked_any_proof = True
            matches = matches and current.st_size == file_size
        if file_mtime is not None:
            checked_any_proof = True
            matches = matches and current.st_mtime == file_mtime

        if checked_any_proof:
            if matches:
                valid.append(path)
            else:
                had_existing_mismatch = True
            continue

        had_only_unverified = True
    if valid:
        return SourceResolution(status=STATUS_OK, files=valid)
    if had_existing_mismatch:
        return SourceResolution(status=STATUS_SOURCE_CHANGED, files=[])
    if had_unsafe_path:
        return SourceResolution(status=STATUS_UNSAFE_SOURCE_PATH, files=[])
    if had_only_unverified:
        return SourceResolution(status=STATUS_SOURCE_UNVERIFIED, files=[])
    return SourceResolution(status=STATUS_MISSING_SOURCE, files=[])


def _parse_source_entries(path: Path) -> list[tuple[int, dict[str, Any]]]:
    return [(line_number, entry) for line_number, entry in parse_all_with_uuids_and_numbers(path)]


def _preview_result_text(results: list[dict[str, Any]]) -> str:
    if not results:
        return ""
    parts = [result["text"] for result in results if isinstance(result.get("text"), str) and result["text"]]
    return "\n".join(parts)[:160]


def _tool_file_signals(invocations: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    for invocation in invocations:
        for signal in invocation.get("file_signals", []):
            if isinstance(signal, str) and signal and signal not in files:
                files.append(signal[:160])
    return files


def _tool_invocations(content: object) -> list[dict[str, Any]]:
    invocations: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return invocations
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            name = item.get("name")
            raw_input = item.get("input")
            tool_input = raw_input if isinstance(raw_input, dict) else {}
            file_signals = []
            for key, value in tool_input.items():
                if key.endswith("path") and isinstance(value, str):
                    file_signals.append(value)
                elif key.endswith("paths") and isinstance(value, list):
                    file_signals.extend(path for path in value if isinstance(path, str))
            if isinstance(name, str):
                invocations.append({"name": name, "summary": build_tool_use_marker(item), "file_signals": file_signals})
    return invocations


def _tool_results(content: object) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return results
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        text = item.get("content", "")
        if isinstance(text, list):
            text = "\n".join(part.get("text", "") for part in text if isinstance(part, dict))
        if not isinstance(text, str):
            text = ""
        results.append({"is_error": bool(item.get("is_error", False)), "text": text.strip()})
    return results


def _path_references(text: str, *, project_root: Path | None = None) -> list[str]:
    paths: list[str] = []
    for match in PATH_REFERENCE_RE.finditer(text):
        candidate = match.group(0).strip("`'\"()[]{}<>,:;.!?")
        normalized = _normalized_path_reference(candidate, project_root=project_root)
        if normalized is not None and normalized not in paths:
            paths.append(normalized)
    return paths


def _project_file_reference_exists(project_root: Path | None, normalized: str) -> bool:
    if project_root is None:
        return False
    candidate = project_root.joinpath(*PurePosixPath(normalized).parts)
    return candidate.is_file()


def _normalized_path_reference(candidate: str, *, project_root: Path | None = None) -> str | None:
    normalized = normalize_project_file_reference(candidate)
    if normalized is None:
        return None
    if "/" not in normalized:
        if normalized in COMMON_ROOT_PATH_REFERENCES:
            return normalized
        if not _project_file_reference_exists(project_root, normalized):
            return None
        return normalized
    segments = normalized.split("/")
    if not segments or any(not segment or not PATH_REFERENCE_SEGMENT_RE.fullmatch(segment) for segment in segments):
        return None
    final_segment = segments[-1]
    if "." not in final_segment and final_segment not in COMMON_ROOT_PATH_REFERENCES:
        return None
    return normalized


def branch_content_file_paths(
    source_files: list[Path], active_branch_uuids: set[str], *, project_root: Path | None = None
) -> set[str]:
    """Collect file-path evidence from active-branch tool inputs, prose, and tool results."""
    paths: set[str] = set()
    for path in source_files:
        for _line_number, entry in _parse_source_entries(path):
            if entry.get("uuid") not in active_branch_uuids:
                continue
            normalized = _normalize_entry(entry)
            for text in (normalized["request_text"], normalized["assistant_text"]):
                if isinstance(text, str):
                    paths.update(_path_references(text, project_root=project_root))
            for result in normalized["tool_results"]:
                text = result.get("text")
                if isinstance(text, str):
                    paths.update(_path_references(text, project_root=project_root))
            for invocation in _tool_invocations(entry.get("message", {}).get("content")):
                for signal in invocation.get("file_signals", []):
                    if isinstance(signal, str):
                        normalized_signal = _normalized_path_reference(signal, project_root=project_root)
                        if normalized_signal is not None:
                            paths.add(normalized_signal)
    return paths


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    role = entry.get("type") if isinstance(entry.get("type"), str) else None
    raw_message = entry.get("message")
    message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
    content = message.get("content")
    text, _has_tool_use, _has_thinking, _tool_summary, _tool_content = extract_text_content(content)
    normalized = {
        "uuid": entry.get("uuid"),
        "parent_uuid": entry.get("parentUuid"),
        "timestamp": entry.get("timestamp"),
        "role": role,
        "request_text": text if role == "user" and not _tool_results(content) else "",
        "assistant_text": text if role == "assistant" else "",
        "tool_invocations": _tool_invocations(content),
        "tool_results": _tool_results(content),
    }
    return normalized


def _entry_sort_key(item: tuple[dict[str, Any], Path, int]) -> tuple[str, str, int, str]:
    normalized, path, line_number = item
    timestamp = normalized.get("timestamp")
    uuid_value = normalized.get("uuid")
    return (
        timestamp if isinstance(timestamp, str) else "",
        str(path),
        line_number,
        uuid_value if isinstance(uuid_value, str) else "",
    )


def _build_outline(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outline: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    exchange_order = 0
    for entry in entries:
        if entry["role"] == "user" and entry["request_text"]:
            exchange_order += 1
            current = {
                "exchange_order": exchange_order,
                "timestamp": entry["timestamp"],
                "user_uuid": entry["uuid"],
                "assistant_uuids": [],
                "result_uuids": [],
                "user_preview": entry["request_text"][:160],
                "assistant_preview": "",
                "result_preview": "",
                "tool_signals": [],
                "file_signals": [],
            }
            outline.append(current)
            continue
        if current is None:
            continue
        if entry["role"] == "assistant":
            current["assistant_uuids"].append(entry["uuid"])
            if not current["assistant_preview"] and entry["assistant_text"]:
                current["assistant_preview"] = entry["assistant_text"][:160]
            current["tool_signals"].extend(tool["name"] for tool in entry["tool_invocations"])
            current["file_signals"].extend(
                signal
                for signal in _tool_file_signals(entry["tool_invocations"])
                if signal not in current["file_signals"]
            )
        elif entry["tool_results"]:
            current["result_uuids"].append(entry["uuid"])
            if not current["result_preview"]:
                current["result_preview"] = _preview_result_text(entry["tool_results"])
    return outline


@contextmanager
def branch_packet(
    *,
    packet_parent: Path,
    session_uuid: str,
    branch_metadata: dict[str, Any],
    active_branch_uuids: set[str],
    source_files: list[Path],
    deterministic_summary: dict[str, Any],
):
    packet_parent.mkdir(parents=True, exist_ok=True)
    _set_owner_only_dir(packet_parent)
    reap_stale_packets(packet_parent)
    packet_dir = Path(tempfile.mkdtemp(prefix=PACKET_DIR_PREFIX, dir=packet_parent))
    _set_owner_only_dir(packet_dir)
    try:
        atomic_write_json(
            packet_dir / MANIFEST_FILENAME,
            {"pid": os.getpid(), "created_at": Instant.now().format_iso(), "session_uuid": session_uuid},
        )
        (packet_dir / MANIFEST_FILENAME).chmod(PID_FILE_MODE)

        seen: dict[str, dict[str, Any]] = {}
        ordered: list[tuple[dict[str, Any], Path, int]] = []
        for path in source_files:
            for line_number, entry in _parse_source_entries(path):
                message_uuid = entry.get("uuid")
                if message_uuid not in active_branch_uuids:
                    continue
                normalized = _normalize_entry(entry)
                existing = seen.get(message_uuid)
                if existing is not None and _canonical_json(existing) != _canonical_json(normalized):
                    raise PacketBuildError(STATUS_SOURCE_CHANGED)
                if existing is None:
                    seen[message_uuid] = normalized
                    ordered.append((normalized, path, line_number))

        if set(seen) != active_branch_uuids:
            raise PacketBuildError(STATUS_SOURCE_INCOMPLETE)

        ordered.sort(key=_entry_sort_key)
        transcript_entries = [item[0] for item in ordered]
        outline = _build_outline(transcript_entries)

        _write_owner_only_text(
            packet_dir / "branch-transcript.jsonl",
            "\n".join(_canonical_json(entry) for entry in transcript_entries) + "\n",
        )
        _write_owner_only_text(packet_dir / "branch-outline.json", json.dumps(outline, indent=2) + "\n")
        _write_owner_only_text(packet_dir / "branch-metadata.json", json.dumps(branch_metadata, indent=2) + "\n")
        _write_owner_only_text(
            packet_dir / "deterministic-summary.json",
            json.dumps(deterministic_summary, indent=2) + "\n",
        )
        _write_owner_only_text(
            packet_dir / "allowed-uuids.txt",
            "\n".join(entry["uuid"] for entry in transcript_entries) + "\n",
        )
        yield packet_dir
    finally:
        _cleanup_tree(packet_dir)


def _pid_alive(pid: int) -> bool:
    try:
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def reap_stale_packets(packet_parent: Path, *, min_age_seconds: int = 3600) -> list[Path]:
    reaped: list[Path] = []
    if not packet_parent.exists():
        return reaped
    now = Instant.now()
    for path in packet_parent.iterdir():
        if not path.is_dir():
            continue
        manifest_path = path / MANIFEST_FILENAME
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            created_at = Instant.parse_iso(manifest["created_at"])
            pid = int(manifest["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        age_seconds = (now - created_at).total("seconds")
        if age_seconds < min_age_seconds:
            continue
        if _pid_alive(pid):
            continue
        _cleanup_tree(path)
        reaped.append(path)
    return reaped
