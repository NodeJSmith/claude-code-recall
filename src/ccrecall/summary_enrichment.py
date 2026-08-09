"""Lightweight contract and renderer for branch summary enrichment."""

import copy
import hashlib
import json
import sqlite3
import uuid
from pathlib import PurePosixPath
from typing import Any

from ccrecall.serialization import decode_json_field

SUMMARY_ENRICHMENT_VERSION = 1

STATUS_OK = "ok"
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
STATUS_INVALID_OUTPUT = "invalid_output"
STATUS_ERROR = "error"

TITLE_MAX_CHARS = 120
MODEL_MAX_CHARS = 120
GENERATED_AT_MAX_CHARS = 64
SECTION_MAX_CHARS = 600
DECISION_MAX_CHARS = 180
RATIONALE_MAX_CHARS = 240
ATTEMPTED_PATH_MAX_CHARS = 180
WHY_STOPPED_MAX_CHARS = 180
OPEN_QUESTION_MAX_CHARS = 180
FILE_PATH_MAX_CHARS = 300
FILE_REASON_MAX_CHARS = 180
CONTINUATION_HINT_MAX_CHARS = 180
MAX_SOURCE_UUIDS = 5
MAX_KEY_DECISIONS = 4
MAX_ATTEMPTED_PATHS = 3
MAX_OPEN_QUESTIONS = 4
MAX_FILES_AND_REASONS = 6
MAX_CONTINUATION_HINTS = 3
PRIMARY_RENDER_BUDGET = 2400
SUPPLEMENTARY_RENDER_BUDGET = 800

ALLOWED_CONFIDENCE_VALUES = {"high", "medium", "low"}
ALLOWED_ATTEMPTED_PATH_OUTCOMES = {"failed", "abandoned", "inconclusive"}
WORKER_OWNED_FIELDS = {"version", "model", "generated_at"}


def normalize_project_file_reference(value: str) -> str | None:
    normalized = value.strip()
    if not normalized or "\\" in normalized or normalized.startswith(("/", "./", "../", "~/")):
        return None
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    if parts[0].endswith(":"):
        return None
    return normalized


_RESPONSE_FIELDS = {
    "title",
    "where_we_left_off",
    "how_we_got_here",
    "key_decisions",
    "attempted_paths",
    "open_questions",
    "files_and_reasons",
    "continuation_hints",
    "confidence",
}
_ENVELOPE_FIELDS = _RESPONSE_FIELDS | WORKER_OWNED_FIELDS
_SUMMARY_SOURCE_HASH_FIELDS = (
    "leaf_uuid",
    "summary_version",
    "context_summary_json",
    "aggregated_content_hash",
    "exchange_count",
    "started_at",
    "ended_at",
    "files_modified",
    "tool_counts",
    "commits",
    "git_branch",
)


def _citation_schema(text_max: int) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["text", "source_uuids"],
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": text_max},
            "source_uuids": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_SOURCE_UUIDS,
                "items": {"type": "string", "minLength": 1},
            },
        },
    }


CLAUDE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(_RESPONSE_FIELDS),
    "properties": {
        "title": _citation_schema(TITLE_MAX_CHARS),
        "where_we_left_off": _citation_schema(SECTION_MAX_CHARS),
        "how_we_got_here": _citation_schema(SECTION_MAX_CHARS),
        "key_decisions": {
            "type": "array",
            "maxItems": MAX_KEY_DECISIONS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["decision", "rationale", "source_uuids"],
                "properties": {
                    "decision": {"type": "string", "minLength": 1, "maxLength": DECISION_MAX_CHARS},
                    "rationale": {"type": "string", "minLength": 1, "maxLength": RATIONALE_MAX_CHARS},
                    "source_uuids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_SOURCE_UUIDS,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        "attempted_paths": {
            "type": "array",
            "maxItems": MAX_ATTEMPTED_PATHS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "outcome", "why_stopped", "source_uuids"],
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": ATTEMPTED_PATH_MAX_CHARS},
                    "outcome": {"type": "string", "enum": sorted(ALLOWED_ATTEMPTED_PATH_OUTCOMES)},
                    "why_stopped": {"type": "string", "minLength": 1, "maxLength": WHY_STOPPED_MAX_CHARS},
                    "source_uuids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_SOURCE_UUIDS,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        "open_questions": {
            "type": "array",
            "maxItems": MAX_OPEN_QUESTIONS,
            "items": _citation_schema(OPEN_QUESTION_MAX_CHARS),
        },
        "files_and_reasons": {
            "type": "array",
            "maxItems": MAX_FILES_AND_REASONS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "reason", "source_uuids"],
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": FILE_PATH_MAX_CHARS},
                    "reason": {"type": "string", "minLength": 1, "maxLength": FILE_REASON_MAX_CHARS},
                    "source_uuids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_SOURCE_UUIDS,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        "continuation_hints": {
            "type": "array",
            "maxItems": MAX_CONTINUATION_HINTS,
            "items": _citation_schema(CONTINUATION_HINT_MAX_CHARS),
        },
        "confidence": {"type": "string", "enum": sorted(ALLOWED_CONFIDENCE_VALUES)},
    },
}


class SummaryEnrichmentValidationError(ValueError):
    """Raised when Claude output or stored enrichment violates the contract."""


def _decode_json_like(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _normalize_for_hash(value: object) -> object:
    if isinstance(value, dict):
        return {key: _normalize_for_hash(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize_for_hash(item) for item in value]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(_normalize_for_hash(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _coerce_mapping(value: object, *, label: str) -> dict[str, Any]:
    decoded = decode_json_field(value, None)
    if not isinstance(decoded, dict):
        raise SummaryEnrichmentValidationError(f"{label} must be an object")
    return decoded


def _require_exact_keys(obj: dict[str, Any], *, required: set[str], label: str) -> None:
    extras = sorted(set(obj) - required)
    if extras:
        if label == "Claude response body" and WORKER_OWNED_FIELDS & set(extras):
            raise SummaryEnrichmentValidationError("Claude response body must not include worker metadata")
        raise SummaryEnrichmentValidationError(f"{label} contains unknown field(s): {', '.join(extras)}")
    missing = sorted(required - set(obj))
    if missing:
        raise SummaryEnrichmentValidationError(f"{label} is missing required field(s): {', '.join(missing)}")


def _validate_string(value: object, *, label: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SummaryEnrichmentValidationError(f"{label} must be a non-empty string")
    if len(value) > max_chars:
        raise SummaryEnrichmentValidationError(f"{label} must be at most {max_chars} characters")
    return value


def _validate_source_uuids(value: object, *, label: str, active_branch_uuids: set[str] | None) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SummaryEnrichmentValidationError(f"{label} must include a non-empty source_uuids list")
    if len(value) > MAX_SOURCE_UUIDS:
        raise SummaryEnrichmentValidationError(f"{label} source_uuids must have at most {MAX_SOURCE_UUIDS} items")

    validated: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SummaryEnrichmentValidationError(f"{label} source_uuids entries must be uuid strings")
        try:
            uuid.UUID(item)
        except ValueError as exc:
            raise SummaryEnrichmentValidationError(f"{label} source_uuids entries must be valid uuid strings") from exc
        if active_branch_uuids is not None and item not in active_branch_uuids:
            raise SummaryEnrichmentValidationError(f"{label} source_uuids must cite the active branch")
        validated.append(item)
    return validated


def _validate_citation_object(
    value: object,
    *,
    label: str,
    text_key: str,
    text_max_chars: int,
    active_branch_uuids: set[str] | None,
) -> dict[str, Any]:
    obj = _coerce_mapping(value, label=label)
    _require_exact_keys(obj, required={text_key, "source_uuids"}, label=label)
    return {
        text_key: _validate_string(obj[text_key], label=f"{label} {text_key}", max_chars=text_max_chars),
        "source_uuids": _validate_source_uuids(
            obj["source_uuids"],
            label=label,
            active_branch_uuids=active_branch_uuids,
        ),
    }


def _validate_list(value: object, *, label: str, max_items: int) -> list[Any]:
    if not isinstance(value, list):
        raise SummaryEnrichmentValidationError(f"{label} must be a list")
    if len(value) > max_items:
        raise SummaryEnrichmentValidationError(f"{label} must have at most {max_items} items")
    return value


def _stored_dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _stored_source_uuid_strings(section: object) -> list[str]:
    if not isinstance(section, dict):
        return []
    source_uuids = section.get("source_uuids")
    if not isinstance(source_uuids, list):
        return []
    return [item for item in source_uuids if isinstance(item, str)]


def _validate_key_decisions(value: object, *, active_branch_uuids: set[str] | None) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(_validate_list(value, label="key_decisions", max_items=MAX_KEY_DECISIONS), start=1):
        label = f"key_decisions[{index}]"
        obj = _coerce_mapping(item, label=label)
        _require_exact_keys(obj, required={"decision", "rationale", "source_uuids"}, label=label)
        validated.append(
            {
                "decision": _validate_string(obj["decision"], label=f"{label} decision", max_chars=DECISION_MAX_CHARS),
                "rationale": _validate_string(
                    obj["rationale"], label=f"{label} rationale", max_chars=RATIONALE_MAX_CHARS
                ),
                "source_uuids": _validate_source_uuids(
                    obj["source_uuids"],
                    label=label,
                    active_branch_uuids=active_branch_uuids,
                ),
            }
        )
    return validated


def _validate_attempted_paths(value: object, *, active_branch_uuids: set[str] | None) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(
        _validate_list(value, label="attempted_paths", max_items=MAX_ATTEMPTED_PATHS),
        start=1,
    ):
        label = f"attempted_paths[{index}]"
        obj = _coerce_mapping(item, label=label)
        _require_exact_keys(obj, required={"text", "outcome", "why_stopped", "source_uuids"}, label=label)
        outcome = _validate_string(obj["outcome"], label=f"{label} outcome", max_chars=32)
        if outcome not in ALLOWED_ATTEMPTED_PATH_OUTCOMES:
            raise SummaryEnrichmentValidationError(f"{label} outcome must be one of failed, abandoned, inconclusive")
        validated.append(
            {
                "text": _validate_string(obj["text"], label=f"{label} text", max_chars=ATTEMPTED_PATH_MAX_CHARS),
                "outcome": outcome,
                "why_stopped": _validate_string(
                    obj["why_stopped"], label=f"{label} why_stopped", max_chars=WHY_STOPPED_MAX_CHARS
                ),
                "source_uuids": _validate_source_uuids(
                    obj["source_uuids"],
                    label=label,
                    active_branch_uuids=active_branch_uuids,
                ),
            }
        )
    return validated


def _validate_files_and_reasons(
    value: object,
    *,
    active_branch_uuids: set[str] | None,
    valid_file_paths: set[str] | None,
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(
        _validate_list(value, label="files_and_reasons", max_items=MAX_FILES_AND_REASONS),
        start=1,
    ):
        label = f"files_and_reasons[{index}]"
        obj = _coerce_mapping(item, label=label)
        _require_exact_keys(obj, required={"path", "reason", "source_uuids"}, label=label)
        path = _validate_string(obj["path"], label=f"{label} path", max_chars=FILE_PATH_MAX_CHARS)
        normalized_path = normalize_project_file_reference(path)
        if normalized_path is None:
            raise SummaryEnrichmentValidationError(f"{label} has an invalid file reference")
        if valid_file_paths is not None and normalized_path not in valid_file_paths:
            raise SummaryEnrichmentValidationError(f"{label} has an invalid file reference")
        validated.append(
            {
                "path": normalized_path,
                "reason": _validate_string(obj["reason"], label=f"{label} reason", max_chars=FILE_REASON_MAX_CHARS),
                "source_uuids": _validate_source_uuids(
                    obj["source_uuids"],
                    label=label,
                    active_branch_uuids=active_branch_uuids,
                ),
            }
        )
    return validated


def validate_claude_response_body(
    value: object,
    *,
    active_branch_uuids: set[str],
    valid_file_paths: set[str],
) -> dict[str, Any]:
    """Validate the factual Claude response body before worker metadata is added."""
    body = _coerce_mapping(value, label="Claude response body")
    _require_exact_keys(body, required=_RESPONSE_FIELDS, label="Claude response body")

    confidence = _validate_string(body["confidence"], label="confidence", max_chars=16)
    if confidence not in ALLOWED_CONFIDENCE_VALUES:
        raise SummaryEnrichmentValidationError("confidence must be one of high, medium, low")

    return {
        "title": _validate_citation_object(
            body["title"],
            label="title",
            text_key="text",
            text_max_chars=TITLE_MAX_CHARS,
            active_branch_uuids=active_branch_uuids,
        ),
        "where_we_left_off": _validate_citation_object(
            body["where_we_left_off"],
            label="where_we_left_off",
            text_key="text",
            text_max_chars=SECTION_MAX_CHARS,
            active_branch_uuids=active_branch_uuids,
        ),
        "how_we_got_here": _validate_citation_object(
            body["how_we_got_here"],
            label="how_we_got_here",
            text_key="text",
            text_max_chars=SECTION_MAX_CHARS,
            active_branch_uuids=active_branch_uuids,
        ),
        "key_decisions": _validate_key_decisions(body["key_decisions"], active_branch_uuids=active_branch_uuids),
        "attempted_paths": _validate_attempted_paths(body["attempted_paths"], active_branch_uuids=active_branch_uuids),
        "open_questions": [
            _validate_citation_object(
                item,
                label=f"open_questions[{index}]",
                text_key="text",
                text_max_chars=OPEN_QUESTION_MAX_CHARS,
                active_branch_uuids=active_branch_uuids,
            )
            for index, item in enumerate(
                _validate_list(body["open_questions"], label="open_questions", max_items=MAX_OPEN_QUESTIONS),
                start=1,
            )
        ],
        "files_and_reasons": _validate_files_and_reasons(
            body["files_and_reasons"],
            active_branch_uuids=active_branch_uuids,
            valid_file_paths=valid_file_paths,
        ),
        "continuation_hints": [
            _validate_citation_object(
                item,
                label=f"continuation_hints[{index}]",
                text_key="text",
                text_max_chars=CONTINUATION_HINT_MAX_CHARS,
                active_branch_uuids=active_branch_uuids,
            )
            for index, item in enumerate(
                _validate_list(
                    body["continuation_hints"],
                    label="continuation_hints",
                    max_items=MAX_CONTINUATION_HINTS,
                ),
                start=1,
            )
        ],
        "confidence": confidence,
    }


def validate_stored_enrichment_envelope(
    value: object,
    *,
    active_branch_uuids: set[str] | None = None,
    valid_file_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Validate persisted enrichment, including worker-owned metadata fields."""
    envelope = _coerce_mapping(value, label="Stored enrichment envelope")
    _require_exact_keys(envelope, required=_ENVELOPE_FIELDS, label="Stored enrichment envelope")

    version = envelope.get("version")
    if version != SUMMARY_ENRICHMENT_VERSION:
        raise SummaryEnrichmentValidationError("Stored enrichment envelope must carry the current version")

    factual_body = {key: copy.deepcopy(envelope[key]) for key in _RESPONSE_FIELDS}
    if active_branch_uuids is None:
        validated_body = validate_claude_response_body_without_membership(factual_body)
    else:
        validated_body = validate_claude_response_body(
            factual_body,
            active_branch_uuids=active_branch_uuids,
            valid_file_paths=valid_file_paths or set(),
        )

    return {
        "version": version,
        "model": _validate_string(envelope.get("model"), label="model", max_chars=MODEL_MAX_CHARS),
        "generated_at": _validate_string(
            envelope.get("generated_at"),
            label="generated_at",
            max_chars=GENERATED_AT_MAX_CHARS,
        ),
        **validated_body,
    }


def validate_claude_response_body_without_membership(value: object) -> dict[str, Any]:
    """Validate stored factual fields when only shape/caps are available on hot paths."""
    body = _coerce_mapping(value, label="Stored enrichment body")
    active_branch_uuids = {
        item
        for section in (body.get("title"), body.get("where_we_left_off"), body.get("how_we_got_here"))
        for item in _stored_source_uuid_strings(section)
    }
    active_branch_uuids |= {
        item
        for list_name in (
            "key_decisions",
            "attempted_paths",
            "open_questions",
            "files_and_reasons",
            "continuation_hints",
        )
        for section in _stored_dict_list(body.get(list_name))
        for item in _stored_source_uuid_strings(section)
    }
    valid_file_paths = {
        path
        for section in _stored_dict_list(body.get("files_and_reasons"))
        for path in [section.get("path")]
        if isinstance(path, str)
    }

    return validate_claude_response_body(
        body,
        active_branch_uuids=active_branch_uuids,
        valid_file_paths=valid_file_paths,
    )


def build_stored_enrichment_envelope(
    response_body: object,
    *,
    model: str,
    generated_at: str,
    active_branch_uuids: set[str],
    valid_file_paths: set[str],
) -> dict[str, Any]:
    """Build the persisted envelope after strict factual-body validation."""
    validated_body = validate_claude_response_body(
        response_body,
        active_branch_uuids=active_branch_uuids,
        valid_file_paths=valid_file_paths,
    )
    return {
        "version": SUMMARY_ENRICHMENT_VERSION,
        "model": _validate_string(model, label="model", max_chars=MODEL_MAX_CHARS),
        "generated_at": _validate_string(generated_at, label="generated_at", max_chars=GENERATED_AT_MAX_CHARS),
        **validated_body,
    }


def compute_summary_source_hash(summary_inputs: dict[str, object]) -> str:
    """Compute the canonical branch-summary source fingerprint.

    Extra provenance fields, such as original transcript paths, are intentionally
    excluded so path-only changes do not stale enrichment.
    """

    aggregated_content = summary_inputs.get("aggregated_content")
    payload = {
        "leaf_uuid": summary_inputs.get("leaf_uuid"),
        "summary_version": summary_inputs.get("summary_version"),
        "context_summary_json": _decode_json_like(summary_inputs.get("context_summary_json")),
        "aggregated_content_hash": _sha256_hex(aggregated_content) if isinstance(aggregated_content, str) else None,
        "exchange_count": summary_inputs.get("exchange_count"),
        "started_at": summary_inputs.get("started_at"),
        "ended_at": summary_inputs.get("ended_at"),
        "files_modified": _decode_json_like(summary_inputs.get("files_modified")),
        "tool_counts": _decode_json_like(summary_inputs.get("tool_counts")),
        "commits": _decode_json_like(summary_inputs.get("commits")),
        "git_branch": summary_inputs.get("git_branch"),
    }
    canonical_payload = {key: payload.get(key) for key in _SUMMARY_SOURCE_HASH_FIELDS}
    return hashlib.sha256(_canonical_json_bytes(canonical_payload)).hexdigest()


def compute_branch_summary_source_hash(cursor: sqlite3.Cursor, branch_db_id: int) -> str | None:
    """Load one branch's deterministic-summary inputs and return its source hash."""

    cursor.execute(
        """
        SELECT b.leaf_uuid, b.summary_version, b.context_summary_json, b.aggregated_content,
               b.exchange_count, b.started_at, b.ended_at, b.files_modified,
               b.tool_counts, b.commits, s.git_branch
        FROM branches b
        JOIN sessions s ON b.session_id = s.id
        WHERE b.id = ? AND b.is_active = 1
        """,
        (branch_db_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return compute_summary_source_hash(
        {
            "leaf_uuid": row[0],
            "summary_version": row[1],
            "context_summary_json": row[2],
            "aggregated_content": row[3],
            "exchange_count": row[4],
            "started_at": row[5],
            "ended_at": row[6],
            "files_modified": row[7],
            "tool_counts": row[8],
            "commits": row[9],
            "git_branch": row[10],
        }
    )


def _validated_enrichment_for_render(enrichment: object) -> dict[str, Any] | None:
    try:
        return validate_stored_enrichment_envelope(enrichment)
    except SummaryEnrichmentValidationError:
        return None


def valid_current_enrichment(
    enrichment: object | None,
    *,
    status: str | None,
    stored_source_hash: str | None,
    current_source_hash: str | None,
    stored_enrichment_version: int | None,
) -> bool:
    """Return True only when enrichment is current, successful, and renderable."""
    if status != STATUS_OK:
        return False
    if stored_enrichment_version != SUMMARY_ENRICHMENT_VERSION:
        return False
    if not stored_source_hash or not current_source_hash or stored_source_hash != current_source_hash:
        return False
    return _validated_enrichment_for_render(enrichment) is not None


def _append_if_within_budget(parts: list[str], section: str, *, char_budget: int) -> bool:
    candidate = "\n\n".join([*parts, section])
    if len(candidate) > char_budget:
        return False
    parts.append(section)
    return True


def _render_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _render_primary_sections(enrichment: dict[str, Any]) -> list[str]:
    sections = [
        f"**Title:** {enrichment['title']['text']}",
        f"**Where we left off:** {enrichment['where_we_left_off']['text']}",
    ]
    if enrichment["continuation_hints"]:
        sections.append(
            "**Continuation hints:**\n" + _render_bullets([item["text"] for item in enrichment["continuation_hints"]])
        )
    sections.append(f"**How we got here:** {enrichment['how_we_got_here']['text']}")
    if enrichment["key_decisions"]:
        sections.append(
            "**Key decisions:**\n"
            + _render_bullets([f"{item['decision']}: {item['rationale']}" for item in enrichment["key_decisions"]])
        )
    if enrichment["attempted_paths"]:
        sections.append(
            "**Attempted paths:**\n"
            + _render_bullets(
                [
                    f"{item['text']} ({item['outcome']}; stopped because {item['why_stopped']})"
                    for item in enrichment["attempted_paths"]
                ]
            )
        )
    if enrichment["open_questions"]:
        sections.append(
            "**Open questions:**\n" + _render_bullets([item["text"] for item in enrichment["open_questions"]])
        )
    if enrichment["files_and_reasons"]:
        file_bits = [f"`{item['path']}` ({item['reason']})" for item in enrichment["files_and_reasons"]]
        sections.append("**Files touched:** " + "; ".join(file_bits))
    return sections


def _render_supplementary_sections(enrichment: dict[str, Any]) -> list[str]:
    sections = [
        f"**Title:** {enrichment['title']['text']}",
        f"**Where we left off:** {enrichment['where_we_left_off']['text']}",
    ]
    if enrichment["continuation_hints"]:
        sections.append("**Continuation hints:**\n- " + enrichment["continuation_hints"][0]["text"])
    elif enrichment["open_questions"]:
        sections.append("**Open questions:**\n- " + enrichment["open_questions"][0]["text"])
    return sections


def render_llm_block(enrichment: object, *, char_budget: int, is_primary_session: bool) -> str:
    """Render a bounded Branch Resume Brief from a validated stored envelope."""
    validated = validate_stored_enrichment_envelope(enrichment)
    parts = ["### Branch Resume Brief"]
    sections = _render_primary_sections(validated) if is_primary_session else _render_supplementary_sections(validated)
    for section in sections:
        if not _append_if_within_budget(parts, section, char_budget=char_budget):
            break
    return "\n\n".join(parts)


def render_enriched_context_summary(
    base_markdown: str,
    enrichment: object | None,
    *,
    is_primary_session: bool,
    status: str | None,
    stored_source_hash: str | None,
    current_source_hash: str | None,
    stored_enrichment_version: int | None,
) -> str:
    """Compose a valid, current Branch Resume Brief above deterministic markdown."""
    if not valid_current_enrichment(
        enrichment,
        status=status,
        stored_source_hash=stored_source_hash,
        current_source_hash=current_source_hash,
        stored_enrichment_version=stored_enrichment_version,
    ):
        return base_markdown
    char_budget = PRIMARY_RENDER_BUDGET if is_primary_session else SUPPLEMENTARY_RENDER_BUDGET
    llm_block = render_llm_block(enrichment, char_budget=char_budget, is_primary_session=is_primary_session)
    if not base_markdown:
        return llm_block
    return llm_block + "\n\n" + base_markdown
