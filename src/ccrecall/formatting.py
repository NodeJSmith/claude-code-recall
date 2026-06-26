"""Session formatting, time utilities, and project path helpers."""

import json
from pathlib import Path

from whenever import Instant

# Verbose output lists at most this many modified files before collapsing to a count.
MAX_FILES_DISPLAYED = 10


def format_time(ts_str: str | None, fmt: str = "%H:%M") -> str:
    """Format an offset-aware ISO timestamp (Z or +HH:MM) to the given strftime
    format in the local timezone. Default: HH:MM.

    Expects the timezone-aware ISO strings the session journal emits; a naive
    string (no offset) is rejected by the parser and returns the raw prefix.
    """
    if not ts_str:
        return "??:??"
    try:
        # whenever owns the parse/tz-convert; strftime needs a stdlib datetime,
        # so cross back at the boundary for the caller's custom format string.
        local = Instant.parse_iso(ts_str).to_system_tz()
        return local.to_stdlib().strftime(fmt)
    except ValueError:
        return ts_str[:16] if ts_str else "??:??"


def format_tool_counts(tool_counts: dict[str, int]) -> str:
    """Render tool usage as a count-descending "name: count, ..." string."""
    sorted_tools = sorted(tool_counts.items(), key=lambda kv: kv[1], reverse=True)
    return ", ".join(f"{name}: {count}" for name, count in sorted_tools)


def format_time_full(ts_str: str | None) -> str:
    """Format ISO timestamp to YYYY-MM-DD HH:MM."""
    return format_time(ts_str, "%Y-%m-%d %H:%M")


_WORKTREE_MARKER = "/.claude/worktrees/"
_WORKTREE_KEY_MARKER = "--claude-worktrees-"


def normalize_cwd(cwd: str) -> str:
    """Strip .claude/worktrees/<name> suffix from a raw path, returning the base repo path.

    Normalizes backslashes to forward slashes first so Windows paths
    (C:\\Users\\...) match the forward-slash worktree marker.
    """
    cwd = cwd.replace("\\", "/")
    idx = cwd.rfind(_WORKTREE_MARKER)
    return cwd[:idx] if idx != -1 else cwd


def get_project_key(cwd: str) -> str:
    """Convert working directory to project key format.

    Resolves .claude/worktrees/<name> paths to the base repo path
    so worktree sessions share project context with the main repo.
    """
    return normalize_cwd(cwd).replace("/", "-").replace(":", "-").replace(".", "-")


def normalize_project_key(key: str) -> str:
    """Strip worktree suffix from an already-encoded project key.

    Encoded worktree keys contain '--claude-worktrees-' (from /.claude/worktrees/).
    """
    idx = key.rfind(_WORKTREE_KEY_MARKER)
    return key[:idx] if idx != -1 else key


def parse_project_key(key: str) -> str:
    """Convert directory key back to original path (lossy — hyphens in dir names are lost).
    Prefer using session cwd metadata when available.

    Detects Windows-style keys (starting with a drive letter like 'C-') and
    reconstructs with the correct prefix. Unix keys start with '-' (from '/').
    """
    # Detect Windows drive letter: key starts with "<letter>--" (colon+slash both → hyphen)
    if len(key) >= 3 and key[0].isalpha() and key[1:3] == "--":
        parts = key[3:].replace("-", "/")
        return key[0].upper() + ":/" + parts.lstrip("/")
    parts = key.lstrip("-").replace("-", "/")
    return "/" + parts.lstrip("/")


def extract_project_name(path: str) -> str:
    """Extract short project name from path."""
    return Path(path).name


def format_markdown_session(session: dict, verbose: bool = False) -> str:
    """Format a single session as markdown."""
    lines = []

    started = format_time_full(session.get("started_at"))
    project = session.get("project", "Unknown")
    lines.append(f"## {project} | {started}")
    lines.append(f"Session: {session.get('uuid', 'unknown')[:8]}")

    if session.get("git_branch"):
        lines.append(f"Branch: {session['git_branch']}")

    if verbose:
        files = session.get("files_modified", [])
        if files:
            lines.append("\n### Files Modified")
            lines.extend(f"- `{f}`" for f in files[-MAX_FILES_DISPLAYED:])
            if len(files) > MAX_FILES_DISPLAYED:
                lines.append(f"- ...and {len(files) - MAX_FILES_DISPLAYED} more")

        commits = session.get("commits", [])
        if commits:
            lines.append("\n### Commits")
            lines.extend(f"- {c}" for c in commits)

        tool_counts = session.get("tool_counts", {})
        if tool_counts:
            lines.append("\n### Tools Used")
            lines.append(format_tool_counts(tool_counts))

    lines.append("\n### Conversation\n")

    for msg in session.get("messages", []):
        if msg.get("is_notification"):  # noqa: SIM108 — nested ternary hurts readability
            role = "Subagent Result"
        else:
            role = "User" if msg["role"] == "user" else "Assistant"
        lines.append(f"**{role}:** {msg['content']}\n")

    lines.append("---\n")
    return "\n".join(lines)


def format_json_sessions(sessions: list[dict], extra: dict | None = None) -> str:
    """Format sessions as JSON with metadata."""
    total_messages = sum(len(s.get("messages", [])) for s in sessions)
    output = {
        "sessions": sessions,
        "total_sessions": len(sessions),
        "total_messages": total_messages,
    }
    if extra:
        output.update(extra)
    return json.dumps(output, indent=2)


# ---------------------------------------------------------------------------
# Score normalization (render-time, over the bounded result set)
# ---------------------------------------------------------------------------


def normalize_scores(results: list[dict]) -> list[dict]:
    """Min-max normalize score_raw to a presented score in [0.0, 1.0] (two decimals).

    Single-result and degenerate cases (max == min) set score to None rather than
    emitting a misleading 1.00 or 0/0 value. score_raw is always preserved.
    Does not mutate the input dicts; returns a new list of new dicts.
    """
    if not results:
        return []

    if len(results) == 1:
        return [{**results[0], "score": None}]

    if any(r.get("score_raw") is None for r in results):
        return [{**r, "score": None} for r in results]

    # All score_raw values are now known non-None — re-extract as a typed float
    # list so min/max narrow without a type-checker suppression.
    raws: list[float] = [r["score_raw"] for r in results]
    min_raw = min(raws)
    max_raw = max(raws)
    denom = max_raw - min_raw

    if denom == 0.0:
        return [{**r, "score": None} for r in results]

    return [{**r, "score": round((r["score_raw"] - min_raw) / denom, 2)} for r in results]


def apply_scores(results: list[dict], ranked: bool) -> list[dict]:
    """Resolve the score fields for a result set per its ranked state.

    Ranked → render-time min-max normalization over this bounded set; unranked
    (the LIKE rung — and, as-built, the fts4 rung too; see issue #35) → score
    and score_raw both None per the contract. Single
    source of truth for this decision, shared by the JSON envelope and the
    markdown card path. Returns new dicts; does not mutate the input.
    """
    if ranked:
        return normalize_scores(results)
    return [{**r, "score": None, "score_raw": None} for r in results]


# ---------------------------------------------------------------------------
# Track A — session-summary card renderer
# ---------------------------------------------------------------------------

# Contract markdown template:
#   ## {score:.2f}  {project} · {git_branch} · {ended_date}
#   Topic:  {topic}
#   Status: {disposition} · {exchange_count} exchanges · {n_files} files · {n_commits} commits
#   Handle: {handle}   → ccrecall tail {handle}

_TOPIC_FALLBACK = "(topic unavailable)"


def format_card_markdown(card: dict, verbose: bool = False) -> str:
    """Render a Track A session-summary card as markdown.

    score=None (single-result or unranked) omits the score prefix from the heading.
    verbose=True expands files_modified, commits, and tool_counts; JSON always
    carries full lists regardless of verbose (FR#10).
    """
    score = card.get("score")
    project = card.get("project", "")
    git_branch = card.get("git_branch", "")
    ended_date = format_time(card.get("ended_at"), "%Y-%m-%d")

    if score is not None:
        heading = f"## {score:.2f}  {project} · {git_branch} · {ended_date}"
    else:
        heading = f"## {project} · {git_branch} · {ended_date}"

    topic = card.get("topic") or _TOPIC_FALLBACK

    files_modified: list = card.get("files_modified") or []
    commits: list = card.get("commits") or []
    tool_counts: dict = card.get("tool_counts") or {}
    exchange_count = card.get("exchange_count") or 0

    n_files = len(files_modified)
    n_commits = len(commits)

    disposition = card.get("disposition")
    if disposition:
        status = f"Status: {disposition} · {exchange_count} exchanges · {n_files} files · {n_commits} commits"
    else:
        status = f"Status: {exchange_count} exchanges · {n_files} files · {n_commits} commits"

    handle = card.get("handle", "")

    lines = [
        heading,
        f"Topic:  {topic}",
        status,
        f"Handle: {handle}   → ccrecall tail {handle}",
    ]

    if verbose:
        if files_modified:
            # Bound the list so a 50-file session can't blow up the verbose card.
            # Show the last N (most recent), matching format_markdown_session's
            # files[-MAX_FILES_DISPLAYED:] cap.
            shown = ", ".join(files_modified[-MAX_FILES_DISPLAYED:])
            if n_files > MAX_FILES_DISPLAYED:
                shown += f", ...and {n_files - MAX_FILES_DISPLAYED} more"
            lines.append(f"Files:  {shown}")
        if commits:
            lines.append(f"Commits: {', '.join(commits)}")
        if tool_counts:
            lines.append(f"Tools:  {format_tool_counts(tool_counts)}")

    return "\n".join(lines)


def format_card_json(card: dict) -> dict:
    """Return the Track A JSON result object for a session card.

    Produces the exact superset shape from output-format-contract.md.
    Carries full files_modified/commits/tool_counts lists regardless of
    whether the card was rendered in verbose markdown (FR#10).
    Does not include exchange/message body text (FR#2, FR#12).
    """
    return {
        "score": card.get("score"),
        "score_raw": card.get("score_raw"),
        "session_uuid": card.get("session_uuid"),
        "handle": card.get("handle"),
        "project": card.get("project"),
        "git_branch": card.get("git_branch"),
        "started_at": card.get("started_at"),
        "ended_at": card.get("ended_at"),
        "topic": card.get("topic"),
        "disposition": card.get("disposition"),
        "exchange_count": card.get("exchange_count") or 0,
        "files_modified": list(card.get("files_modified") or []),
        "commits": list(card.get("commits") or []),
        "tool_counts": dict(card.get("tool_counts") or {}),
    }


# ---------------------------------------------------------------------------
# Track B — matched-exchange snippet renderer
# ---------------------------------------------------------------------------

# Contract markdown template:
#   {score:.2f}  {project}/{git_branch} · {handle} · exchange {idx} · {time}
#     User: {user_text}
#     Asst: {assistant_text}
#     → ccrecall tail {handle}


def format_snippet_markdown(snippet: dict) -> str:
    """Render a Track B matched-exchange snippet as markdown.

    score=None (single-result or unranked) omits the score prefix from the first line.
    On the vector path matched_role is None and match_terms is [] — both are valid
    inputs; the markdown renders the user/assistant text without highlighting.
    """
    score = snippet.get("score")
    project = snippet.get("project", "")
    git_branch = snippet.get("git_branch", "")
    handle = snippet.get("handle", "")
    exchange_index = snippet.get("exchange_index", 0)
    time_str = format_time(snippet.get("timestamp"))

    locator = f"{project}/{git_branch} · {handle} · exchange {exchange_index} · {time_str}"
    first_line = f"{score:.2f}  {locator}" if score is not None else locator

    user_text = snippet.get("user") or ""
    assistant_text = snippet.get("assistant") or ""

    lines = [first_line]
    if user_text:
        lines.append(f"  User: {user_text}")
    if assistant_text:
        lines.append(f"  Asst: {assistant_text}")
    lines.append(f"  → ccrecall tail {handle}")

    return "\n".join(lines)


def format_snippet_json(snippet: dict) -> dict:
    """Return the Track B JSON result object for a matched-exchange snippet.

    matched_role=None and match_terms=[] are valid on the vector path (no discrete
    term hits on KNN; matched_role is undefined when the whole exchange is the unit).
    """
    return {
        "score": snippet.get("score"),
        "score_raw": snippet.get("score_raw"),
        "session_uuid": snippet.get("session_uuid"),
        "handle": snippet.get("handle"),
        "project": snippet.get("project"),
        "git_branch": snippet.get("git_branch"),
        "exchange_index": snippet.get("exchange_index"),
        "matched_role": snippet.get("matched_role"),
        "timestamp": snippet.get("timestamp"),
        "user": snippet.get("user"),
        "assistant": snippet.get("assistant"),
        "match_terms": list(snippet.get("match_terms") or []),
    }


# ---------------------------------------------------------------------------
# Shared envelope builder (JSON) and markdown result-list renderer
# ---------------------------------------------------------------------------


def build_envelope(query: str, ranked: bool, results: list[dict]) -> dict:
    """Build the shared JSON result envelope for either track.

    Applies render-time min-max score normalization when ranked=True (the
    normalization window is this bounded result set, not the full ranked list).
    On the unranked path (ranked=False — the LIKE rung, and as-built the fts4
    rung; see issue #35), sets all score and score_raw to None per the contract.

    Returns {query, ranked, count, results} with score fields populated.
    """
    processed = apply_scores(results, ranked)
    return {
        "query": query,
        "ranked": ranked,
        "count": len(processed),
        "results": processed,
    }


def format_result_list_markdown(ranked: bool, result_markdowns: list[str]) -> str:
    """Format a list of pre-rendered card/snippet markdown strings.

    When ranked=False (any unranked keyword rung — LIKE, and as-built fts4),
    prepends the unranked marker line so consumers know no relevance score was
    available.
    """
    if not ranked:
        parts = ["(keyword fallback — unranked, ordered by recency)", ""]
        parts.extend(result_markdowns)
        return "\n".join(parts)
    return "\n".join(result_markdowns)
