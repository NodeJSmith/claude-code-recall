"""Reject private material and incomplete structure in recap evidence artifacts."""

import re
import sys
from pathlib import Path

ELIGIBILITY_HEADINGS = (
    "## Sample strata",
    "## Measures",
    "## Labels",
    "## Candidate-rule comparison",
    "## Selected policy",
    "## Reason codes",
)
ELIGIBILITY_TABLES = {
    "## Sample strata": ("stratum", "sample count"),
    "## Measures": ("measure", "definition"),
    "## Labels": ("label", "count"),
    "## Candidate-rule comparison": (
        "rule",
        "eligible",
        "useful-session recall",
        "precision",
    ),
    "## Selected policy": ("threshold", "value"),
    "## Reason codes": ("reason code", "meaning"),
}
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.IGNORECASE)
PATH_RE = re.compile(r"(?:[A-Za-z]:\\|~/|(?<!\d)/|(?:[A-Za-z_.-][\w.-]*/)+)[^\s|)`]*")
MARKDOWN_LINK_RE = re.compile(r"\]\s*(?:\(|\[)")
MARKDOWN_REFERENCE_DEF_RE = re.compile(r"^\s*\[[^\]\n]+\]:\s*\S+", re.MULTILINE)
EXCERPT_RE = re.compile(r"\b(?:excerpt|transcript(?:\s+excerpt)?|recap|quote|user|assistant)\s*:", re.IGNORECASE)
REVIEWER_NOTES_RE = re.compile(r"\b(?:reviewer notes?|notes)\s*:", re.IGNORECASE)
POLICY_VERSION_RE = re.compile(r"\bELIGIBILITY_POLICY_VERSION\s*=\s*\d+\b")
RECAP_HEADINGS = (
    "## Method",
    "## Aggregate input results",
    "## Aggregate model results",
    "## Decision",
    "## Privacy boundary",
)
RECAP_TABLES = {
    "## Aggregate input results": (
        "input authority",
        "completed recaps",
        "recognition",
        "work arc",
        "outcome",
        "relative packet size",
    ),
    "## Aggregate model results": ("model", "completed db-input calls", "recognition", "work arc", "outcome"),
}
PRIVATE_MAPPING_RE = re.compile(
    r"\b(?:private\s+|per[- ]sample\s+)?(?:mapping|identifier map|sample map)\s*:", re.IGNORECASE
)
RAW_OUTPUT_RE = re.compile(r"\b(?:raw output|model output|provider output|prompt)\s*:", re.IGNORECASE)


def table_in_section(text: str, heading: str) -> tuple[list[str], list[list[str]]] | None:
    """Return the single Markdown table following a required heading."""
    section = text.split(heading, 1)[1]
    next_heading = re.search(r"^## ", section, re.MULTILINE)
    if next_heading:
        section = section[: next_heading.start()]

    lines = [line for line in section.splitlines() if line.startswith("|")]
    if len(lines) < 3:
        return None

    header = [cell.strip().lower() for cell in lines[0].strip("|").split("|")]
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines[2:] if line.strip("|").strip(" -:")]
    return header, rows


def has_private_label_table(text: str) -> bool:
    """Identify Markdown tables that expose per-sample labels or verdicts."""
    lines = text.splitlines()
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("|") or not lines[index + 1].startswith("|"):
            continue
        header = [cell.strip().lower() for cell in line.strip("|").split("|")]
        has_label = any(cell in {"label", "verdict"} for cell in header)
        has_identifier = any(
            "session" in cell or "branch" in cell or cell in {"id", "identifier", "sample", "row"} for cell in header
        )
        if has_label and has_identifier:
            return True
    return False


def check_evidence(text: str) -> list[str]:
    """Return all structural and privacy violations in an audit artifact."""
    eligibility = "# Eligibility Audit" in text
    headings = ELIGIBILITY_HEADINGS if eligibility else RECAP_HEADINGS
    tables = ELIGIBILITY_TABLES if eligibility else RECAP_TABLES
    errors = [f"missing required section: {heading}" for heading in headings if heading not in text]
    if eligibility and not POLICY_VERSION_RE.search(text):
        errors.append("missing ELIGIBILITY_POLICY_VERSION")
    for heading, expected_header in tables.items():
        if heading not in text:
            continue
        table = table_in_section(text, heading)
        name = heading.removeprefix("## ").lower()
        if table is None:
            errors.append(f"missing {name} rows")
            continue
        header, rows = table
        if tuple(header) != expected_header:
            errors.append(f"invalid {name} table")
            continue
        if not rows:
            errors.append(f"missing {name} rows")
            continue
        if heading == "## Selected policy" and not any(re.search(r"\d", row[1]) for row in rows if len(row) == 2):
            errors.append("missing selected threshold values")
    # Normalize Markdown labels so emphasis cannot evade the privacy gate.
    privacy_text = text
    privacy_text = re.sub(r"(?<=\w)[*_`]+(?=\s*:)", "", privacy_text)
    if UUID_RE.search(privacy_text):
        errors.append("contains UUID-shaped identifier")
    if has_private_label_table(privacy_text):
        errors.append("contains private label row")
    if EXCERPT_RE.search(privacy_text):
        errors.append("contains transcript excerpt")
    if REVIEWER_NOTES_RE.search(privacy_text):
        errors.append("contains reviewer notes")
    if PRIVATE_MAPPING_RE.search(privacy_text) or has_private_label_table(privacy_text):
        errors.append("contains private mapping")
    if RAW_OUTPUT_RE.search(privacy_text) or "```" in text:
        errors.append("contains prompt or raw output")
    if MARKDOWN_LINK_RE.search(text) or MARKDOWN_REFERENCE_DEF_RE.search(text):
        errors.append("contains Markdown link")
    if PATH_RE.search(privacy_text):
        errors.append("contains path-like content")
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_recap_evidence.py PATH", file=sys.stderr)
        return 2
    try:
        text = Path(sys.argv[1]).read_text(encoding="utf-8")
    except OSError as exc:
        print(exc, file=sys.stderr)
        return 2

    errors = check_evidence(text)
    if errors:
        print("recap evidence check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
