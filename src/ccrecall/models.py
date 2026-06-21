"""Pydantic models validating untrusted JSON at the system boundaries.

Claude Code transcripts, token-usage records, and hook stdin are all external
input. These models validate the shapes the code relies on so a malformed line
or an upstream schema change surfaces as a clean skip (logged at the boundary)
rather than a mid-import AttributeError. They stay permissive on unknown fields
(``extra="allow"``) and only enforce the types the downstream code dereferences.
"""

import logging

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# The single logger name setup_logging() configures. Shared so boundary skip-logs
# and hook exceptions reach the same rotating file as the rest of the app; a module
# logger would propagate to an unconfigured root and silently drop them. Defined here
# (the lowest-level module) so db/session_ops/hooks can import it without a cycle.
LOGGER_NAME = "claude-memory"

_LOG = logging.getLogger(LOGGER_NAME)


def is_valid(model: type[BaseModel], data: object, label: str) -> bool:
    """Validate untrusted JSON against a boundary model; log and reject on failure.

    Shared by every ingest boundary so the validate-then-skip behavior (and its
    log line) lives in one place.
    """
    try:
        model.model_validate(data)
    except ValidationError as e:
        _LOG.debug("Skipping malformed %s: %s", label, e)
        return False
    return True


class EntryMessage(BaseModel):
    """The ``message`` object on a transcript entry.

    ``content`` is the field the import path dereferences (``message.get("content")``
    then iterated as blocks), so its type is what we guard — a non-dict ``message``
    or a scalar ``content`` is what crashed compute_branch_metadata.
    """

    model_config = ConfigDict(extra="allow")

    role: str | None = None
    content: str | list | None = None


class TranscriptEntry(BaseModel):
    """One line of a Claude Code transcript JSONL file."""

    model_config = ConfigDict(extra="allow")

    uuid: str | None = None
    parent_uuid: str | None = Field(default=None, alias="parentUuid")
    type: str | None = None
    timestamp: str | None = None
    git_branch: str | None = Field(default=None, alias="gitBranch")
    cwd: str | None = None
    is_meta: bool | None = Field(default=None, alias="isMeta")
    session_id: str | None = Field(default=None, alias="sessionId")
    message: EntryMessage | None = None


class TokenUsage(BaseModel):
    """The ``usage`` block on an assistant message — feeds cost computation."""

    model_config = ConfigDict(extra="allow")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation: dict | None = None


class TokenMessage(BaseModel):
    """The ``message`` object on a token-usage JSONL line."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    model: str | None = None
    stop_reason: str | None = None
    usage: TokenUsage | None = None
    # str | list (not list[dict]): parse_session iterates content blocks but
    # guards each with isinstance(block, dict), so any list shape is acceptable —
    # we only reject a scalar that would break the iteration.
    content: str | list | None = None


class TokenLine(BaseModel):
    """One line of a token-usage JSONL file (the envelope parse_session reads)."""

    model_config = ConfigDict(extra="allow")

    type: str | None = None
    subtype: str | None = None
    message: TokenMessage | None = None


class HookInput(BaseModel):
    """Hook stdin payload from the Claude Code harness.

    String fields reject non-string types (pydantic does not coerce int→str),
    so a malformed ``session_id``/``cwd`` is caught instead of flowing into
    ``get_project_key`` or a handoff file.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str | None = None
    cwd: str | None = None
    source: str | None = None
    end_reason: str | None = None
