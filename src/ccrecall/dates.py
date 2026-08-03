"""Validation for --before/--after CLI date-range boundaries.

Boundary values are compared lexicographically against the stored ISO-8601
UTC timestamp strings (branches.started_at), matching Claude Code's own
transcript `timestamp` format. This module's job is to reject, before it
reaches SQL, anything that would silently corrupt that comparison: an
unpadded month ("2026-8-1"), a missing timezone, a non-ISO format — and to
normalize a full instant to the exact fixed-width form the comparison
depends on. A bare date is used as-is (it has no timezone to normalize and
is already a valid prefix). A full instant is re-rendered to UTC with
millisecond precision (see parse_date_boundary), because real transcript
timestamps always carry milliseconds and a boundary missing them would sort
incorrectly against same-second stored values (e.g. "...10Z" would compare
less than "...10.635Z" purely because "." < "Z", even though ...10Z is
chronologically earlier). Both normalized forms share a zero-padded,
fixed-width prefix with the stored strings, so a validated value always
compares correctly.
"""

from whenever import Date, Instant

from ccrecall.errors import emit_error


def parse_date_boundary(value: str, flag: str) -> str:
    """Validate a --before/--after value; return the string to use in SQL.

    Accepts a bare ISO date (YYYY-MM-DD) or a full timezone-aware ISO-8601
    instant (e.g. 2026-08-03T12:00:00Z or with a +HH:MM offset). Raises
    ValueError with a user-facing message on anything else, so a malformed
    date fails loudly at the CLI boundary instead of silently matching the
    wrong rows (or none).

    A bare date is returned unchanged — it has no timezone to normalize, and
    compares correctly as a prefix against the stored UTC strings. A full
    instant is re-rendered via format_iso(unit="millisecond") into the exact
    UTC "Z" form the DB stores, with a fixed 3-digit fractional field: a
    non-UTC offset (e.g. "+02:00") is a *different string* than its UTC
    equivalent, and an instant with no fractional seconds (e.g. "...10Z")
    sorts lexicographically *before* a stored millisecond timestamp in the
    same second (e.g. "...10.635Z", since "." < "Z") even though it is
    chronologically earlier — both would silently break the lexicographic
    comparison against started_at even though the instant itself parsed
    correctly. Forcing millisecond precision keeps every normalized boundary
    the same fixed width as the stored strings, which real Claude Code
    transcript timestamps always carry.
    """
    try:
        Date.parse_iso(value)
        return value
    except ValueError:
        pass
    try:
        return Instant.parse_iso(value).format_iso(unit="millisecond")
    except ValueError:
        raise ValueError(
            f"{flag} must be an ISO-8601 date (YYYY-MM-DD) or a timezone-aware "
            f"timestamp (e.g. 2026-08-03T12:00:00Z); got {value!r}"
        ) from None


def validate_date_boundaries(before: str | None, after: str | None) -> tuple[str | None, str | None]:
    """Validate --before/--after and return the normalized pair to use in SQL.

    Raises ValueError (via parse_date_boundary) on anything unparseable.
    Callers must use the returned values, not the originals — a full instant
    with a non-UTC offset is normalized to a different string here.
    """
    normalized_before = parse_date_boundary(before, "--before") if before is not None else None
    normalized_after = parse_date_boundary(after, "--after") if after is not None else None
    return normalized_before, normalized_after


def validate_or_exit(before: str | None, after: str | None) -> tuple[str | None, str | None]:
    """validate_date_boundaries, but exit 2 with a structured error instead of raising.

    Every read command (recent-chats, search, search-messages) needs the same
    validate-or-exit behavior at the same point in its run() — before the
    db.exists() check — so this is the one place that pairs
    validate_date_boundaries with emit_error, rather than each command
    repeating the try/except.
    """
    try:
        return validate_date_boundaries(before, after)
    except ValueError as e:
        emit_error(
            str(e),
            code="invalid_date",
            exit_code=2,
            remediation="Use YYYY-MM-DD or a full ISO-8601 timestamp like 2026-08-03T12:00:00Z.",
        )
        raise AssertionError("unreachable — emit_error always raises SystemExit") from e
