"""Dependency-light Session Recap contract and SessionStart renderer."""

import hashlib
import json
import sqlite3
from pathlib import PurePosixPath
from typing import Any

from ccrecall.recap_input import ELIGIBILITY_POLICY_VERSION, RECAP_CONTRACT_VERSION, RECAP_INPUT_CONTRACT_VERSION
from ccrecall.serialization import decode_json_field

SUMMARY_ENRICHMENT_VERSION = RECAP_CONTRACT_VERSION

STATUS_OK = "ok"
STATUS_INVALID_OUTPUT = "invalid_output"
STATUS_ERROR = "error"
# Retained until the provider/lifecycle replacement removes v1 worker callers.
STATUS_CAPABILITY_UNVERIFIED = "capability_unverified"
STATUS_MISSING_SOURCE = "missing_source"
STATUS_UNSAFE_SOURCE_PATH = "unsafe_source_path"
STATUS_SOURCE_CHANGED = "source_changed"
STATUS_SOURCE_INCOMPLETE = "source_incomplete"
STATUS_SOURCE_UNVERIFIED = "source_unverified"
STATUS_UNSUPPORTED_CLI = "unsupported_cli"
STATUS_CLAUDE_UNAVAILABLE = "claude_unavailable"
STATUS_AUTH_REQUIRED = "auth_required"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_BUDGET_EXCEEDED = "budget_exceeded"
STATUS_TIMEOUT = "timeout"

TITLE_MAX_CHARS = 160
SUMMARY_MAX_CHARS = 1_200
OUTCOME_MAX_CHARS = 32
MODEL_MAX_CHARS = 120
GENERATED_AT_MAX_CHARS = 64
PRIMARY_RENDER_BUDGET = 1_600
SUPPLEMENTARY_RENDER_BUDGET = 800
ALLOWED_OUTCOMES = {"completed", "partial", "blocked", "unknown"}

CLAUDE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["summary"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": SUMMARY_MAX_CHARS},
        "title": {"type": "string", "maxLength": TITLE_MAX_CHARS},
        "outcome": {"type": "string", "maxLength": OUTCOME_MAX_CHARS},
    },
}


class SummaryEnrichmentValidationError(ValueError):
    """Raised when the required recap core cannot be rendered safely."""


def normalize_project_file_reference(value: str) -> str | None:
    """Keep the transitional v1 packet helper importable until T06 removes it."""
    normalized = value.strip()
    if not normalized or "\\" in normalized or normalized.startswith(("/", "./", "../", "~/")):
        return None
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in ("", ".", "..") for part in parts) or parts[0].endswith(":"):
        return None
    return normalized


def _coerce_mapping(value: object, *, label: str) -> dict[str, Any]:
    decoded = decode_json_field(value, None)
    if not isinstance(decoded, dict):
        raise SummaryEnrichmentValidationError(f"{label} must be an object")
    return decoded


def _bounded_required_text(value: object, *, label: str, max_chars: int) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        raise SummaryEnrichmentValidationError(f"{label} must be a non-empty string")
    return text[:max_chars]


def _bounded_optional_text(value: object, *, max_chars: int) -> str | None:
    if not isinstance(value, str) or not (text := value.strip()):
        return None
    return text[:max_chars]


def _normalized_outcome(value: object) -> str | None:
    text = _bounded_optional_text(value, max_chars=OUTCOME_MAX_CHARS)
    if text is None:
        return None
    normalized = "_".join(text.lower().replace("-", " ").split())
    return normalized if normalized in ALLOWED_OUTCOMES else None


def validate_claude_response_body(value: object, **_ignored: object) -> dict[str, Any]:
    """Normalize useful model output, rejecting only an unusable summary."""
    body = _coerce_mapping(value, label="Claude response body")
    normalized = {"summary": _bounded_required_text(body.get("summary"), label="summary", max_chars=SUMMARY_MAX_CHARS)}
    title = _bounded_optional_text(body.get("title"), max_chars=TITLE_MAX_CHARS)
    outcome = _normalized_outcome(body.get("outcome"))
    if title is not None:
        normalized["title"] = title
    if outcome is not None:
        normalized["outcome"] = outcome
    return normalized


def validate_stored_enrichment_envelope(value: object) -> dict[str, Any]:
    """Normalize a stored v2 envelope; v1 payloads are never interpreted."""
    envelope = _coerce_mapping(value, label="Stored enrichment envelope")
    if envelope.get("version") != SUMMARY_ENRICHMENT_VERSION:
        raise SummaryEnrichmentValidationError("Stored enrichment envelope must carry the current version")
    required_metadata = {
        "model": (MODEL_MAX_CHARS, None),
        "generated_at": (GENERATED_AT_MAX_CHARS, None),
        "attempt_id": (None, "attempt_id"),
        "recap_input_hash": (None, "recap_input_hash"),
        "input_contract_version": (None, "input_contract_version"),
        "eligibility_policy_version": (None, "eligibility_policy_version"),
    }
    metadata: dict[str, Any] = {"version": SUMMARY_ENRICHMENT_VERSION}
    for field, (max_chars, kind) in required_metadata.items():
        field_value = envelope.get(field)
        if max_chars is not None:
            metadata[field] = _bounded_required_text(field_value, label=field, max_chars=max_chars)
        elif kind in {"attempt_id", "input_contract_version", "eligibility_policy_version"}:
            if not isinstance(field_value, int) or isinstance(field_value, bool):
                raise SummaryEnrichmentValidationError(f"{field} must be an integer")
            metadata[field] = field_value
        else:
            metadata[field] = _bounded_required_text(field_value, label=field, max_chars=128)
    return {**metadata, **validate_claude_response_body(envelope)}


def build_stored_enrichment_envelope(
    response_body: object,
    *,
    model: str,
    generated_at: str,
    attempt_id: int,
    recap_input_hash: str,
    input_contract_version: int = RECAP_INPUT_CONTRACT_VERSION,
    eligibility_policy_version: int = ELIGIBILITY_POLICY_VERSION,
    **_ignored: object,
) -> dict[str, Any]:
    """Add worker-owned v2 metadata after normalizing model output."""
    return validate_stored_enrichment_envelope(
        {
            "version": SUMMARY_ENRICHMENT_VERSION,
            "model": model,
            "generated_at": generated_at,
            "attempt_id": attempt_id,
            "recap_input_hash": recap_input_hash,
            "input_contract_version": input_contract_version,
            "eligibility_policy_version": eligibility_policy_version,
            **validate_claude_response_body(response_body),
        }
    )


def _decode_json_like(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def compute_summary_source_hash(summary_inputs: dict[str, object]) -> str:
    """Compute the existing deterministic-summary fingerprint."""
    fields = (
        "leaf_uuid",
        "summary_version",
        "context_summary_json",
        "aggregated_content",
        "exchange_count",
        "started_at",
        "ended_at",
        "files_modified",
        "tool_counts",
        "commits",
        "git_branch",
    )
    payload = {key: _decode_json_like(summary_inputs.get(key)) for key in fields}
    if isinstance(payload["aggregated_content"], str):
        payload["aggregated_content"] = hashlib.sha256(payload["aggregated_content"].encode()).hexdigest()
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def compute_branch_summary_source_hash(cursor: sqlite3.Cursor, branch_db_id: int) -> str | None:
    row = cursor.execute(
        """SELECT b.leaf_uuid, b.summary_version, b.context_summary_json, b.aggregated_content,
                  b.exchange_count, b.started_at, b.ended_at, b.files_modified, b.tool_counts,
                  b.commits, s.git_branch FROM branches b JOIN sessions s ON b.session_id = s.id
           WHERE b.id = ? AND b.is_active = 1""",
        (branch_db_id,),
    ).fetchone()
    if row is None:
        return None
    keys = (
        "leaf_uuid",
        "summary_version",
        "context_summary_json",
        "aggregated_content",
        "exchange_count",
        "started_at",
        "ended_at",
        "files_modified",
        "tool_counts",
        "commits",
        "git_branch",
    )
    return compute_summary_source_hash(dict(zip(keys, row, strict=True)))


def valid_current_enrichment(
    enrichment: object | None,
    *,
    status: str | None,
    current_input_hash: str | None = None,
    materialized_input_hash: str | None = None,
    materialized_input_contract_version: int | None = None,
    materialized_policy_version: int | None = None,
    stored_enrichment_version: int | None,
    **_ignored: object,
) -> bool:
    """Return true only for a successful, current v2 recap cache entry."""
    if status != STATUS_OK or stored_enrichment_version != SUMMARY_ENRICHMENT_VERSION:
        return False
    if not current_input_hash or current_input_hash != materialized_input_hash:
        return False
    if materialized_input_contract_version != RECAP_INPUT_CONTRACT_VERSION:
        return False
    if materialized_policy_version != ELIGIBILITY_POLICY_VERSION:
        return False
    try:
        envelope = validate_stored_enrichment_envelope(enrichment)
    except SummaryEnrichmentValidationError:
        return False
    return (
        envelope["recap_input_hash"] == materialized_input_hash
        and envelope["input_contract_version"] == RECAP_INPUT_CONTRACT_VERSION
        and envelope["eligibility_policy_version"] == ELIGIBILITY_POLICY_VERSION
    )


def render_llm_block(enrichment: object, *, char_budget: int, is_primary_session: bool) -> str:
    """Render a bounded Session Recap from a validated v2 envelope."""
    del is_primary_session
    recap = validate_stored_enrichment_envelope(enrichment)
    parts = ["### Session Recap"]
    if title := recap.get("title"):
        parts.append(f"**Title:** {title}")
    prefix = "\n\n".join(parts)
    available = char_budget - len(prefix) - 2
    if available <= 0:
        return ""
    parts.append(recap["summary"][:available].rstrip())
    if outcome := recap.get("outcome"):
        candidate = "\n\n".join([*parts, f"**Outcome:** {outcome}"])
        if len(candidate) <= char_budget:
            parts.append(f"**Outcome:** {outcome}")
    return "\n\n".join(parts)


def render_enriched_context_summary(
    base_markdown: str,
    enrichment: object | None,
    *,
    is_primary_session: bool,
    status: str | None,
    current_input_hash: str | None = None,
    materialized_input_hash: str | None = None,
    materialized_input_contract_version: int | None = None,
    materialized_policy_version: int | None = None,
    stored_enrichment_version: int | None,
    **_ignored: object,
) -> str:
    """Place a current Session Recap above deterministic context, else leave it unchanged."""
    if not valid_current_enrichment(
        enrichment,
        status=status,
        current_input_hash=current_input_hash,
        materialized_input_hash=materialized_input_hash,
        materialized_input_contract_version=materialized_input_contract_version,
        materialized_policy_version=materialized_policy_version,
        stored_enrichment_version=stored_enrichment_version,
    ):
        return base_markdown
    budget = PRIMARY_RENDER_BUDGET if is_primary_session else SUPPLEMENTARY_RENDER_BUDGET
    recap = render_llm_block(enrichment, char_budget=budget, is_primary_session=is_primary_session)
    if not recap:
        return base_markdown
    return recap if not base_markdown else recap + "\n\n" + base_markdown
