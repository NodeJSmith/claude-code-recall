"""Render `ccrecall status` and `ccrecall doctor` from one set of findings.

`status` is the glance: a verdict line and a line of plain numbers. `doctor` is
the diagnosis: every finding, grouped by what the user should do, with exact
commands. Both share one `--json` shape (`verdict`, `findings`, `summary`), and
what a state means lives in findings.py, so the two can't disagree.

Exit codes: `status` exits 0 for any verdict it can produce from a database, and
1 when the database is missing or unreadable. `doctor` exits 0 for healthy or
working on it, 1 for needs attention (a missing database included), and 2 when
the database exists but can't be read at all.
"""

import dataclasses
import json
import sys
from pathlib import Path

from whenever import Instant

from ccrecall.config import DEFAULT_DB_PATH, get_db_path, load_settings_for_db
from ccrecall.findings import (
    FINDING_IDS,
    GROUP_ACTION,
    GROUP_NONE,
    GROUP_WORKING,
    LISTABLE_IDS,
    VERDICT_HEALTHY,
    VERDICT_NEEDS_ATTENTION,
    VERDICT_WORKING,
    DatabaseUnreadableError,
    Diagnosis,
    diagnose,
    parse_instant,
)

EXIT_NEEDS_ATTENTION = 1
EXIT_UNREADABLE = 2

_VERDICT_TEXT = {
    VERDICT_HEALTHY: "healthy",
    VERDICT_WORKING: "working on it",
    VERDICT_NEEDS_ATTENTION: "needs attention",
}
_GROUP_HEADINGS = (
    (GROUP_ACTION, "Needs action"),
    (GROUP_WORKING, "Working on it"),
    (GROUP_NONE, "Nothing to do"),
)


def validate_list_id(finding_id: str) -> None:
    """Reject an unknown `--list` id, naming the valid ones (a usage error, exit 2)."""
    if finding_id not in FINDING_IDS:
        raise ValueError(f"unknown finding id {finding_id!r}; valid ids: {', '.join(sorted(FINDING_IDS))}")


def run_status(*, db: Path = DEFAULT_DB_PATH, output_format: str = "markdown") -> None:
    settings = load_settings_for_db(db)
    db_path = get_db_path(settings)
    try:
        diagnosis = diagnose(db_path, settings)
    except DatabaseUnreadableError as exc:
        print(f"ccrecall status: can't read the database at {db_path} ({exc}); run `ccrecall doctor`", file=sys.stderr)
        raise SystemExit(EXIT_NEEDS_ATTENTION) from exc

    if output_format == "json":
        print(json.dumps(diagnosis_json(diagnosis), indent=2))
    else:
        print(verdict_line(diagnosis, point_to_doctor=True))
        print(summary_line(diagnosis))
    if not db_path.exists():
        raise SystemExit(EXIT_NEEDS_ATTENTION)


def run_doctor(*, db: Path = DEFAULT_DB_PATH, output_format: str = "markdown", list_id: str | None = None) -> None:
    settings = load_settings_for_db(db)
    db_path = get_db_path(settings)
    try:
        diagnosis = diagnose(db_path, settings)
    except DatabaseUnreadableError as exc:
        print(f"ccrecall doctor: can't read the database at {db_path}: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_UNREADABLE) from exc

    if list_id is not None:
        if output_format == "json":
            print(json.dumps(diagnosis_json(diagnosis, list_id=list_id), indent=2))
        else:
            print_listing(diagnosis, list_id)
        return

    if output_format == "json":
        print(json.dumps(diagnosis_json(diagnosis), indent=2))
    else:
        print_report(diagnosis)
    if diagnosis.verdict == VERDICT_NEEDS_ATTENTION:
        raise SystemExit(EXIT_NEEDS_ATTENTION)


def diagnosis_json(diagnosis: Diagnosis, *, list_id: str | None = None) -> dict:
    """The shared `--json` shape. With ``list_id``, that finding also carries a ``sessions`` array."""
    findings = []
    for finding in diagnosis.findings:
        item = dataclasses.asdict(finding)
        if finding.id == list_id and finding.listable:
            item["sessions"] = [
                {key: value for key, value in dataclasses.asdict(row).items() if key != "reason" or value is not None}
                for row in diagnosis.sessions.get(finding.id, ())
            ]
        findings.append(item)
    return {"verdict": diagnosis.verdict, "findings": findings, "summary": dict(diagnosis.summary)}


def verdict_line(diagnosis: Diagnosis, *, point_to_doctor: bool = False) -> str:
    line = f"ccrecall: {_VERDICT_TEXT[diagnosis.verdict]}"
    if point_to_doctor and diagnosis.verdict != VERDICT_HEALTHY:
        line += " (run `ccrecall doctor`)"
    return line


def summary_line(diagnosis: Diagnosis, now: Instant | None = None) -> str:
    """``7,514 sessions · 98% embedded · last sync 2 min ago``, leaving out what isn't known yet."""
    summary = diagnosis.summary
    parts = [f"{summary['sessions']:,} sessions"]
    if summary["embedded_pct"] is not None:
        parts.append(f"{summary['embedded_pct']}% embedded")
    last_sync = parse_instant(summary["last_sync"])
    if last_sync is not None:
        parts.append(f"last sync {format_ago(last_sync, now or Instant.now())}")
    return " · ".join(parts)


def print_report(diagnosis: Diagnosis) -> None:
    print(verdict_line(diagnosis))
    print(summary_line(diagnosis))
    for group, heading in _GROUP_HEADINGS:
        findings = [finding for finding in diagnosis.findings if finding.group == group]
        if not findings:
            continue
        print(f"\n{heading}")
        for finding in findings:
            print(f"  - {finding.message}")
            if finding.fix:
                print(f"    Fix: {finding.fix}")
            if finding.listable and finding.count:
                print(f"    List them: ccrecall doctor --list {finding.id}")


def print_listing(diagnosis: Diagnosis, list_id: str) -> None:
    if list_id not in LISTABLE_IDS:
        print(f"{list_id} doesn't name sessions, so there's nothing to list.")
        return
    rows = diagnosis.sessions.get(list_id, ())
    if not rows:
        print(f"Nothing to list: no {list_id} finding right now.")
        return
    for row in rows:
        fields = [row.session_id, row.project or "-", (row.date or "-")[:10]]
        if row.reason:
            fields.append(row.reason)
        print("  ".join(fields))


def format_ago(then: Instant, now: Instant) -> str:
    seconds = max(0.0, (now - then).total("seconds"))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    return f"{days} day ago" if days == 1 else f"{days} days ago"
