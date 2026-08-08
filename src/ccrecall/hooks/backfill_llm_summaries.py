"""Detached worker for LLM summary enrichment."""

import argparse
import json
import logging
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from whenever import Instant

from ccrecall.config import (
    DEFAULT_PROJECTS_DIR,
    RUNTIME_DIR,
    load_settings,
    remove_pid_file,
    setup_logging,
    try_acquire_pid_file,
)
from ccrecall.llm_summarizer import (
    InvocationResult,
    PacketBuildError,
    branch_packet,
    build_prompt,
    capability_fingerprint,
    discover_current_session_source_files,
    invoke_claude,
    resolve_historical_source_files,
    verify_capability_sidecar,
    write_capability_sidecar,
)
from ccrecall.llm_summarizer import (
    run_capability_check as run_capability_smoke_test,
)
from ccrecall.llm_summary_db import get_connection
from ccrecall.parsing import extract_session_uuid
from ccrecall.summarizer import SUMMARY_VERSION
from ccrecall.summary_enrichment import (
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
    SUMMARY_ENRICHMENT_VERSION,
    build_stored_enrichment_envelope,
    compute_branch_summary_source_hash,
)

PID_KEY = "ccrecall-backfill-llm-summaries"
CAPABILITY_SIDECAR_PATH = RUNTIME_DIR / "claude-summary-capability.json"
BATCH_SIZE = 25
DIAGNOSTIC_CAP = 240
EXIT_OK = 0
EXIT_ABORT = 1

FORCE_ONLY_STATUSES = {STATUS_INVALID_OUTPUT, STATUS_BUDGET_EXCEEDED}
CAPABILITY_BLOCKED_STATUSES = {
    STATUS_CAPABILITY_UNVERIFIED,
    STATUS_UNSUPPORTED_CLI,
    STATUS_CLAUDE_UNAVAILABLE,
    STATUS_AUTH_REQUIRED,
    STATUS_RATE_LIMITED,
    STATUS_TIMEOUT,
    STATUS_ERROR,
}
SOURCE_STATUSES = {
    STATUS_MISSING_SOURCE,
    STATUS_SOURCE_CHANGED,
    STATUS_SOURCE_INCOMPLETE,
    STATUS_SOURCE_UNVERIFIED,
    STATUS_UNSAFE_SOURCE_PATH,
}


def _cap(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:DIAGNOSTIC_CAP]


def _diagnostic_summary(status: str, diagnostic: str | None, *, capability: bool = False) -> str | None:
    prefix = "capability: " if capability else ""
    if diagnostic:
        return _cap(prefix + diagnostic)
    return _cap(prefix + status)


def _parse_json_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        data = json.loads(value)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _parse_json_string_list(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    try:
        data = json.loads(value)
    except ValueError:
        return set()
    if not isinstance(data, list):
        return set()
    return {item for item in data if isinstance(item, str) and item}


def _parse_json_list(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        data = json.loads(value)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, str) and item]


def _select_branch_ids(
    cursor: sqlite3.Cursor,
    *,
    days: int | None,
    limit: int | None,
    session_uuid: str | None,
    after_branch_id: int | None,
    force: bool,
    min_exchanges: int,
) -> list[int]:
    where = ["b.is_active = 1", "b.context_summary_json IS NOT NULL", "b.summary_version = ?"]
    params: list[Any] = [SUMMARY_VERSION]
    if not force:
        where.append("b.exchange_count >= ?")
        params.append(min_exchanges)
    if days is not None:
        where.append("b.ended_at > datetime('now', ?)")
        params.append(f"-{days} days")
    if session_uuid is not None:
        where.append("s.uuid = ?")
        params.append(session_uuid)
    if after_branch_id is not None:
        where.append("b.id > ?")
        params.append(after_branch_id)
    query = (
        "SELECT b.id FROM branches b JOIN sessions s ON s.id = b.session_id "
        f"WHERE {' AND '.join(where)} ORDER BY b.id LIMIT ?"
    )
    params.append(limit if limit is not None else BATCH_SIZE)
    cursor.execute(query, params)
    return [row[0] for row in cursor.fetchall()]


def _load_branch_row(cursor: sqlite3.Cursor, branch_id: int) -> sqlite3.Row | None:
    cursor.execute(
        """
        SELECT b.id, b.session_id, b.leaf_uuid, b.started_at, b.ended_at, b.exchange_count,
               b.files_modified, b.commits, b.tool_counts, b.aggregated_content,
               b.context_summary, b.context_summary_json, b.summary_version,
               b.summary_enrichment_json, b.summary_enrichment_version,
               b.summary_enrichment_source_hash, b.summary_enrichment_status,
               b.summary_enrichment_error, b.summary_source_hash,
               s.uuid AS session_uuid, s.git_branch, s.cwd, p.name AS project_name
        FROM branches b
        JOIN sessions s ON s.id = b.session_id
        JOIN projects p ON p.id = s.project_id
        WHERE b.id = ? AND b.is_active = 1
        """,
        (branch_id,),
    )
    return cursor.fetchone()


def _load_active_branch_uuids(cursor: sqlite3.Cursor, branch_id: int) -> set[str]:
    cursor.execute(
        """
        SELECT m.uuid
        FROM branch_messages bm
        JOIN messages m ON m.id = bm.message_id
        WHERE bm.branch_id = ?
        ORDER BY m.timestamp, m.id
        """,
        (branch_id,),
    )
    return {row[0] for row in cursor.fetchall() if isinstance(row[0], str) and row[0]}


def _load_import_log_rows(cursor: sqlite3.Cursor, session_uuid: str) -> list[Mapping[str, Any] | Sequence[Any]]:
    cursor.execute(
        """
        SELECT file_path, file_hash, file_size, file_mtime
        FROM import_log
        WHERE file_path LIKE ?
        ORDER BY file_path
        """,
        (f"%{session_uuid}.jsonl",),
    )
    rows = [tuple(row) for row in cursor.fetchall()]
    return [row for row in rows if isinstance(row[0], str) and extract_session_uuid(Path(row[0])) == session_uuid]


def _resolve_source_files(
    cursor: sqlite3.Cursor,
    *,
    session_uuid: str,
    projects_dir: Path,
    current_session: bool = False,
) -> tuple[str, list[Path]]:
    historical_rows = _load_import_log_rows(cursor, session_uuid)
    if current_session:
        current_files = discover_current_session_source_files(projects_dir, session_uuid)
        if current_files:
            return STATUS_OK, current_files
    if historical_rows:
        resolved = resolve_historical_source_files(session_uuid, historical_rows)
        return resolved.status, resolved.files
    current_files = discover_current_session_source_files(projects_dir, session_uuid)
    if current_files:
        return STATUS_OK, current_files
    return STATUS_MISSING_SOURCE, []


def _needs_enrichment(
    row: sqlite3.Row,
    *,
    force: bool,
    capability_status: str | None = None,
    source_status: str | None = None,
) -> bool:
    if force:
        return True
    status = row["summary_enrichment_status"]
    if status is None:
        return True
    if status == STATUS_OK:
        return (
            row["summary_enrichment_version"] != SUMMARY_ENRICHMENT_VERSION
            or not row["summary_enrichment_source_hash"]
            or row["summary_enrichment_source_hash"] != row["summary_source_hash"]
        )
    if status in FORCE_ONLY_STATUSES:
        return False
    if status == STATUS_UNSAFE_SOURCE_PATH:
        return False
    if status in CAPABILITY_BLOCKED_STATUSES:
        return capability_status == STATUS_OK
    if status in SOURCE_STATUSES:
        return source_status == STATUS_OK
    return True


def _persist_expected_source_hash(cursor: sqlite3.Cursor, branch_id: int, row: sqlite3.Row) -> str | None:
    expected_hash = row["summary_source_hash"]
    if expected_hash is not None:
        return expected_hash
    if _parse_json_object(row["context_summary_json"]) is None:
        return None
    computed = compute_branch_summary_source_hash(cursor, branch_id)
    if computed is None:
        return None
    cursor.execute(
        "UPDATE branches SET summary_source_hash = ? WHERE id = ? AND summary_source_hash IS NULL",
        (computed, branch_id),
    )
    cursor.execute("SELECT summary_source_hash FROM branches WHERE id = ?", (branch_id,))
    refreshed = cursor.fetchone()
    if refreshed is None or not isinstance(refreshed[0], str):
        return None
    return refreshed[0]


def _write_status(
    cursor: sqlite3.Cursor,
    *,
    branch_id: int,
    expected_hash: str,
    status: str,
    diagnostic: str | None,
) -> bool:
    cursor.execute(
        """
        UPDATE branches
        SET summary_enrichment_status = ?,
            summary_enrichment_error = ?,
            summary_enrichment_updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND summary_source_hash = ?
        """,
        (status, _cap(diagnostic), branch_id, expected_hash),
    )
    return cursor.rowcount > 0


def _write_success(
    cursor: sqlite3.Cursor,
    *,
    branch_id: int,
    expected_hash: str,
    envelope: dict[str, Any],
) -> bool:
    cursor.execute(
        """
        UPDATE branches
        SET summary_enrichment_json = ?,
            summary_enrichment_version = ?,
            summary_enrichment_source_hash = ?,
            summary_enrichment_status = ?,
            summary_enrichment_error = NULL,
            summary_enrichment_updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND summary_source_hash = ?
        """,
        (
            json.dumps(envelope, ensure_ascii=False),
            SUMMARY_ENRICHMENT_VERSION,
            expected_hash,
            STATUS_OK,
            branch_id,
            expected_hash,
        ),
    )
    return cursor.rowcount > 0


def get_claude_version(run: Any = subprocess.run) -> tuple[str | None, str | None]:
    try:
        completed = run(["claude", "--version"], capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return None, _cap(str(exc))
    if completed.returncode != 0:
        return None, _cap(completed.stderr.strip() or completed.stdout.strip() or "claude --version failed")
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0] if output else "unknown", None


def run_capability_check(
    *,
    verbose: bool = False,
    capability_sidecar_path: Path = CAPABILITY_SIDECAR_PATH,
    projects_dir: Path = DEFAULT_PROJECTS_DIR,
) -> int:
    settings = load_settings()
    setup_logging(settings, process_name="backfill-llm-summary", verbose=verbose)

    claude_version, version_error = get_claude_version()
    if claude_version is None:
        diagnostic = _cap(version_error or STATUS_CLAUDE_UNAVAILABLE)
        write_capability_sidecar(
            capability_sidecar_path,
            status=STATUS_CLAUDE_UNAVAILABLE,
            claude_version="unknown",
            fingerprint=capability_fingerprint(),
            diagnostic=diagnostic,
        )
        print(f"ccrecall backfill llm-summaries: capability check failed: {diagnostic}")
        return EXIT_ABORT

    result = run_capability_smoke_test(
        settings,
        sidecar_path=capability_sidecar_path,
        projects_dir=projects_dir,
        claude_version=claude_version,
    )
    if result.status == STATUS_OK:
        print("ccrecall backfill llm-summaries: capability check passed")
        return EXIT_OK

    diagnostic = result.diagnostic or result.status
    print(f"ccrecall backfill llm-summaries: capability check failed: {diagnostic}")
    return EXIT_ABORT


def _process_branch(
    branch_id: int,
    *,
    settings: dict[str, Any],
    logger: logging.Logger,
    force: bool,
    capability_status: str,
    capability_diagnostic: str | None,
    projects_dir: Path,
    current_session: bool,
) -> bool:
    with get_connection(settings) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        row = _load_branch_row(cursor, branch_id)
        if row is None:
            return False

        source_status, source_files = _resolve_source_files(
            cursor,
            session_uuid=row["session_uuid"],
            projects_dir=projects_dir,
            current_session=current_session,
        )
        if not _needs_enrichment(
            row,
            force=force,
            capability_status=capability_status,
            source_status=source_status,
        ):
            return False

        expected_hash = _persist_expected_source_hash(cursor, branch_id, row)
        if expected_hash is None:
            return False

        summary_json = _parse_json_object(row["context_summary_json"])
        if summary_json is None:
            return False

        active_branch_uuids = _load_active_branch_uuids(cursor, branch_id)
        if not active_branch_uuids:
            return False

        if capability_status != STATUS_OK:
            _write_status(
                cursor,
                branch_id=branch_id,
                expected_hash=expected_hash,
                status=capability_status,
                diagnostic=_diagnostic_summary(capability_status, capability_diagnostic, capability=True),
            )
            return False

        if source_status != STATUS_OK:
            _write_status(
                cursor,
                branch_id=branch_id,
                expected_hash=expected_hash,
                status=source_status,
                diagnostic=_diagnostic_summary(source_status, None),
            )
            return False

        branch_metadata = {
            "session_uuid": row["session_uuid"],
            "branch_id": branch_id,
            "leaf_uuid": row["leaf_uuid"],
            "project": row["project_name"],
            "cwd": row["cwd"],
            "git_branch": row["git_branch"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "exchange_count": row["exchange_count"],
            "files_modified": sorted(_parse_json_string_list(row["files_modified"])),
            "tool_counts": _parse_json_object(row["tool_counts"]) or {},
            "commits": _parse_json_list(row["commits"]),
            "source_transcript_paths": [str(path) for path in source_files],
        }
        valid_file_paths = _parse_json_string_list(row["files_modified"])

    try:
        with branch_packet(
            packet_parent=RUNTIME_DIR / "llm-summary-packets",
            session_uuid=branch_metadata["session_uuid"],
            branch_metadata=branch_metadata,
            active_branch_uuids=active_branch_uuids,
            source_files=source_files,
            deterministic_summary=summary_json,
        ) as packet_dir:
            prompt = build_prompt(packet_dir)
            result = invoke_claude(
                packet_dir,
                settings,
                prompt,
                active_branch_uuids=active_branch_uuids,
                valid_file_paths=valid_file_paths,
            )
    except PacketBuildError as exc:
        result = InvocationResult(status=exc.status, diagnostic=_cap(exc.diagnostic))
    except Exception as exc:
        result = InvocationResult(status=STATUS_ERROR, diagnostic=_cap(str(exc)))

    with get_connection(settings) as conn:
        cursor = conn.cursor()
        if result.status == STATUS_OK and result.response_body is not None:
            envelope = build_stored_enrichment_envelope(
                result.response_body,
                model=settings["llm_summary_model"],
                generated_at=Instant.now().format_iso(),
                active_branch_uuids=active_branch_uuids,
                valid_file_paths=valid_file_paths,
            )
            if not _write_success(cursor, branch_id=branch_id, expected_hash=expected_hash, envelope=envelope):
                logger.info("LLM summary stale for branch %s; discarding result", branch_id)
            return True

        if not _write_status(
            cursor,
            branch_id=branch_id,
            expected_hash=expected_hash,
            status=result.status,
            diagnostic=result.diagnostic,
        ):
            logger.info("LLM summary stale for branch %s; discarding status %s", branch_id, result.status)
        return False


def _run(
    *,
    days: int | None = None,
    limit: int | None = None,
    session: str | None = None,
    current_session: bool = False,
    force: bool = False,
    verbose: bool = False,
    capability_sidecar_path: Path = CAPABILITY_SIDECAR_PATH,
    projects_dir: Path = DEFAULT_PROJECTS_DIR,
) -> int:
    if days is not None and days < 1:
        raise ValueError("days must be >= 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    if current_session and session is None:
        raise ValueError("--current-session requires --session")

    settings = load_settings()
    logger = setup_logging(settings, process_name="backfill-llm-summary", verbose=verbose)

    claude_version, version_error = get_claude_version()
    if claude_version is None:
        capability_status = STATUS_CLAUDE_UNAVAILABLE
        capability_diagnostic = version_error
    else:
        capability_status, capability_diagnostic = verify_capability_sidecar(
            capability_sidecar_path,
            claude_version=claude_version,
            fingerprint=capability_fingerprint(),
        )

    processed = 0
    total_success = 0
    last_branch_id: int | None = None
    try:
        while True:
            with get_connection(settings) as conn:
                cursor = conn.cursor()
                remaining = None if limit is None else limit - processed
                if remaining is not None and remaining <= 0:
                    return EXIT_OK
                batch_limit = min(BATCH_SIZE, remaining) if remaining is not None else None
                if current_session:
                    batch_limit = 1 if batch_limit is None else min(batch_limit, 1)
                batch_ids = _select_branch_ids(
                    cursor,
                    days=days,
                    limit=batch_limit,
                    session_uuid=session,
                    after_branch_id=last_branch_id,
                    force=force,
                    min_exchanges=settings["llm_summary_min_exchanges"],
                )

            if not batch_ids:
                break

            last_branch_id = batch_ids[-1]

            for branch_id in batch_ids:
                if _process_branch(
                    branch_id,
                    settings=settings,
                    logger=logger,
                    force=force,
                    capability_status=capability_status,
                    capability_diagnostic=capability_diagnostic,
                    projects_dir=projects_dir,
                    current_session=current_session,
                ):
                    total_success += 1
                processed += 1
            if current_session:
                break
    except (sqlite3.Error, OSError):
        logger.exception("LLM summary worker aborted")
        return EXIT_ABORT

    logger.info("LLM summary worker complete: %s branches enriched", total_success)
    return EXIT_OK


def run(
    *,
    days: int | None = None,
    limit: int | None = None,
    session: str | None = None,
    current_session: bool = False,
    force: bool = False,
    verbose: bool = False,
    capability_sidecar_path: Path = CAPABILITY_SIDECAR_PATH,
    projects_dir: Path = DEFAULT_PROJECTS_DIR,
) -> int:
    if not try_acquire_pid_file(PID_KEY):
        return EXIT_OK
    try:
        return _run(
            days=days,
            limit=limit,
            session=session,
            current_session=current_session,
            force=force,
            verbose=verbose,
            capability_sidecar_path=capability_sidecar_path,
            projects_dir=projects_dir,
        )
    finally:
        remove_pid_file(PID_KEY)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccrecall-llm-summaries")
    parser.add_argument("--days", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--session")
    parser.add_argument("--current-session", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.current_session and args.session is None:
        parser.error("--current-session requires --session")
    return run(
        days=args.days,
        limit=args.limit,
        session=args.session,
        current_session=args.current_session,
        force=args.force,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    raise SystemExit(main())
