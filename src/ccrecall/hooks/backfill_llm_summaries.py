"""Detached worker for LLM summary enrichment."""

import argparse
import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    branch_content_file_paths,
    branch_packet,
    build_prompt,
    capability_fingerprint,
    get_claude_version,
    invoke_claude,
    resolve_current_session_source_files,
    resolve_historical_source_files,
    verify_capability_sidecar,
    write_capability_sidecar,
)
from ccrecall.llm_summarizer import (
    run_capability_check as run_capability_smoke_test,
)
from ccrecall.llm_summary_db import get_connection
from ccrecall.parsing import extract_session_uuid
from ccrecall.recap_input import ELIGIBILITY_POLICY_VERSION, RECAP_INPUT_CONTRACT_VERSION, refresh_recap_input
from ccrecall.recap_state import recap_state_changed_input
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
    normalize_project_file_reference,
)

PID_KEY = "ccrecall-backfill-llm-summaries"
CAPABILITY_SIDECAR_PATH = RUNTIME_DIR / "claude-summary-capability.json"
BATCH_SIZE = 25
DIAGNOSTIC_CAP = 240
EXIT_OK = 0
EXIT_ABORT = 1
CAPABILITY_DIAGNOSTIC_PREFIX = "capability: "

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
}


@dataclass(frozen=True)
class BranchProcessResult:
    selected: bool
    enriched: bool


def _normalize_process_result(result: object) -> BranchProcessResult:
    if isinstance(result, BranchProcessResult):
        return result
    selected = getattr(result, "selected", None)
    enriched = getattr(result, "enriched", None)
    if isinstance(selected, bool) and isinstance(enriched, bool):
        return BranchProcessResult(selected=selected, enriched=enriched)
    if isinstance(result, bool):
        return BranchProcessResult(selected=result, enriched=result)
    raise TypeError(f"Unsupported branch process result: {type(result).__name__}")


def _is_capability_gated_budget_failure(row: sqlite3.Row) -> bool:
    # The branch status preserves the sidecar's provider failure, while this
    # marker distinguishes a capability-check budget threshold from a real
    # branch-enrichment budget failure during eligibility selection.
    return (
        row["summary_enrichment_status"] == STATUS_BUDGET_EXCEEDED
        and isinstance(row["summary_enrichment_error"], str)
        and row["summary_enrichment_error"].startswith(CAPABILITY_DIAGNOSTIC_PREFIX)
    )


def _print_manual_capability_guidance(status: str, diagnostic: str | None) -> None:
    detail = diagnostic or status
    print(
        "ccrecall backfill llm-summaries: capability gate blocked"
        f" ({detail}); run `ccrecall backfill llm-summaries --check-capability` first"
    )


def _cap(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:DIAGNOSTIC_CAP]


def _diagnostic_summary(status: str, diagnostic: str | None, *, capability: bool = False) -> str | None:
    prefix = CAPABILITY_DIAGNOSTIC_PREFIX if capability else ""
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


def _normalize_files_modified(value: object, project_root: Path | None) -> set[str]:
    normalized_paths: set[str] = set()
    for item in _parse_json_string_list(value):
        normalized = normalize_project_file_reference(item)
        if normalized is None and project_root is not None:
            candidate = Path(item)
            if candidate.is_absolute():
                with_relative_root = None
                try:
                    with_relative_root = candidate.relative_to(project_root)
                except ValueError:
                    with_relative_root = None
                if with_relative_root is not None:
                    normalized = normalize_project_file_reference(with_relative_root.as_posix())
        if normalized is not None:
            normalized_paths.add(normalized)
    return normalized_paths


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
                b.summary_enrichment_error, b.summary_source_hash, b.recap_input_hash,
                b.recap_input_contract_version, b.recap_eligibility_policy_version,
                b.summary_enrichment_input_hash, b.summary_enrichment_input_contract_version,
                b.summary_enrichment_policy_version,
               s.uuid AS session_uuid, s.git_branch, s.cwd, p.name AS project_name,
               p.path AS project_root
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
    current_resolved = None
    if current_session:
        current_resolved = resolve_current_session_source_files(projects_dir, session_uuid)
        if current_resolved.status == STATUS_UNSAFE_SOURCE_PATH:
            return current_resolved.status, current_resolved.files
        if current_resolved.status == STATUS_OK:
            return current_resolved.status, current_resolved.files
    if historical_rows:
        resolved = resolve_historical_source_files(session_uuid, historical_rows)
        return resolved.status, resolved.files
    if current_resolved is not None:
        return current_resolved.status, current_resolved.files
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
            or not row["recap_input_hash"]
            or row["recap_input_hash"] != row["summary_enrichment_input_hash"]
            or row["summary_enrichment_input_contract_version"] != RECAP_INPUT_CONTRACT_VERSION
            or row["summary_enrichment_policy_version"] != ELIGIBILITY_POLICY_VERSION
        )
    if _is_capability_gated_budget_failure(row):
        return capability_status != status
    if status in FORCE_ONLY_STATUSES:
        return False
    if status == STATUS_UNSAFE_SOURCE_PATH:
        return False
    if status in CAPABILITY_BLOCKED_STATUSES:
        return capability_status != status
    if status in SOURCE_STATUSES:
        return source_status == STATUS_OK
    return True


def _write_status(
    cursor: sqlite3.Cursor,
    *,
    branch_id: int,
    expected_input_hash: str,
    status: str,
    diagnostic: str | None,
) -> bool:
    cursor.execute(
        """
        UPDATE branches
        SET summary_enrichment_status = ?,
            summary_enrichment_error = ?,
            summary_enrichment_updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND recap_input_hash = ?
        """,
        (status, _cap(diagnostic), branch_id, expected_input_hash),
    )
    return cursor.rowcount > 0


def _write_success(
    cursor: sqlite3.Cursor,
    *,
    branch_id: int,
    expected_input_hash: str,
    envelope: dict[str, Any],
) -> bool:
    cursor.execute(
        """
        UPDATE branches
        SET summary_enrichment_json = ?,
             summary_enrichment_version = ?,
             summary_enrichment_input_hash = ?,
             summary_enrichment_input_contract_version = ?,
             summary_enrichment_policy_version = ?,
            summary_enrichment_status = ?,
            summary_enrichment_error = NULL,
            summary_enrichment_updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND recap_input_hash = ?
        """,
        (
            json.dumps(envelope, ensure_ascii=False),
            SUMMARY_ENRICHMENT_VERSION,
            expected_input_hash,
            RECAP_INPUT_CONTRACT_VERSION,
            ELIGIBILITY_POLICY_VERSION,
            STATUS_OK,
            branch_id,
            expected_input_hash,
        ),
    )
    return cursor.rowcount > 0


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
) -> BranchProcessResult:
    with get_connection(settings) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        row = _load_branch_row(cursor, branch_id)
        if row is None:
            return BranchProcessResult(selected=False, enriched=False)

        status = row["summary_enrichment_status"]
        if (
            not force
            and (status in CAPABILITY_BLOCKED_STATUSES or _is_capability_gated_budget_failure(row))
            and status == capability_status
        ):
            return BranchProcessResult(selected=False, enriched=False)

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
            return BranchProcessResult(selected=False, enriched=False)

        recap_input = refresh_recap_input(cursor, branch_id)
        recap_state_changed_input(cursor.connection, row["session_id"], recap_input.input_hash, None)
        if not recap_input.input_hash:
            return BranchProcessResult(selected=False, enriched=False)

        summary_json = _parse_json_object(row["context_summary_json"])
        if summary_json is None:
            return BranchProcessResult(selected=False, enriched=False)

        active_branch_uuids = _load_active_branch_uuids(cursor, branch_id)
        if not active_branch_uuids:
            return BranchProcessResult(selected=False, enriched=False)

        if capability_status != STATUS_OK:
            _write_status(
                cursor,
                branch_id=branch_id,
                expected_input_hash=recap_input.input_hash,
                status=capability_status,
                diagnostic=_diagnostic_summary(capability_status, capability_diagnostic, capability=True),
            )
            return BranchProcessResult(selected=True, enriched=False)

        if source_status != STATUS_OK:
            _write_status(
                cursor,
                branch_id=branch_id,
                expected_input_hash=recap_input.input_hash,
                status=source_status,
                diagnostic=_diagnostic_summary(source_status, None),
            )
            return BranchProcessResult(selected=True, enriched=False)

        project_root = (
            Path(row["project_root"]) if isinstance(row["project_root"], str) and row["project_root"] else None
        )
        normalized_files_modified = _normalize_files_modified(row["files_modified"], project_root)

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
            "files_modified": sorted(normalized_files_modified),
            "tool_counts": _parse_json_object(row["tool_counts"]) or {},
            "commits": _parse_json_list(row["commits"]),
            "source_transcript_paths": [str(path) for path in source_files],
        }
        valid_file_paths = normalized_files_modified | branch_content_file_paths(
            source_files,
            active_branch_uuids,
            project_root=project_root,
        )

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
                attempt_id=0,
                recap_input_hash=recap_input.input_hash,
            )
            if not _write_success(
                cursor, branch_id=branch_id, expected_input_hash=recap_input.input_hash, envelope=envelope
            ):
                logger.info("LLM summary stale for branch %s; discarding result", branch_id)
                return BranchProcessResult(selected=True, enriched=False)
            return BranchProcessResult(selected=True, enriched=True)

        if not _write_status(
            cursor,
            branch_id=branch_id,
            expected_input_hash=recap_input.input_hash,
            status=result.status,
            diagnostic=result.diagnostic,
        ):
            logger.info("LLM summary stale for branch %s; discarding status %s", branch_id, result.status)
        return BranchProcessResult(selected=True, enriched=False)


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

    if not current_session and capability_status != STATUS_OK:
        _print_manual_capability_guidance(capability_status, capability_diagnostic)

    selected_branches = 0
    total_success = 0
    last_branch_id: int | None = None

    def complete() -> int:
        logger.info("LLM summary worker complete: %s branches enriched", total_success)
        if not current_session:
            print(f"ccrecall backfill llm-summaries: complete: {total_success} branches enriched")
        return EXIT_OK

    if not current_session:
        print("ccrecall backfill llm-summaries: processing eligible branches")

    try:
        while True:
            with get_connection(settings) as conn:
                cursor = conn.cursor()
                remaining = None if limit is None else limit - selected_branches
                if remaining is not None and remaining <= 0:
                    return complete()
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
                result = _normalize_process_result(
                    _process_branch(
                        branch_id,
                        settings=settings,
                        logger=logger,
                        force=force,
                        capability_status=capability_status,
                        capability_diagnostic=capability_diagnostic,
                        projects_dir=projects_dir,
                        current_session=current_session,
                    )
                )
                if result.enriched:
                    total_success += 1
                if result.selected:
                    selected_branches += 1
                if limit is not None and selected_branches >= limit:
                    return complete()
            if current_session:
                break
    except (sqlite3.Error, OSError):
        logger.exception("LLM summary worker aborted")
        return EXIT_ABORT

    return complete()


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
    except Exception:
        logging.getLogger(__name__).exception("LLM summary worker crashed")
        return EXIT_ABORT
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
    if args.days is not None and args.days < 1:
        parser.error("--days must be >= 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
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
