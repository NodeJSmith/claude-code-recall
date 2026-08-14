"""Result dedup and session-card hydration for conversation search."""

import sqlite3

from ccrecall.serialization import decode_json_column

# Fallback topic truncation for a recall card when no precomputed topic exists.
FALLBACK_TOPIC_MAX_CHARS = 200


def dedup_by_session(cursor: sqlite3.Cursor, ordered_branch_ids: list[int]) -> list[int]:
    """Keep the highest-ranked branch per session_id.

    Returns a new list with one branch per session, preserving relative order.
    """
    if not ordered_branch_ids:
        return []

    placeholders = ",".join("?" * len(ordered_branch_ids))
    rows = cursor.execute(
        f"SELECT id, session_id FROM branches WHERE id IN ({placeholders})",
        ordered_branch_ids,
    ).fetchall()

    branch_to_session: dict[int, int] = {row[0]: row[1] for row in rows}

    seen_sessions: set[int] = set()
    deduped: list[int] = []
    for bid in ordered_branch_ids:
        sess = branch_to_session.get(bid)
        if sess is None:
            continue
        if sess not in seen_sessions:
            seen_sessions.add(sess)
            deduped.append(bid)
    return deduped


def hydrate_cards(
    cursor: sqlite3.Cursor,
    branch_ids: list[int],
    branch_scores: dict[int, float] | None = None,
) -> list[dict]:
    """Build Track A session-summary card dicts for an ordered list of branch IDs.

    Reads context_summary_json (topic) and branch/session/project join
    columns. Does NOT call fetch_branch_messages — A renders from summary data only
    (no full transcript hydration).

    Graceful degrade: when context_summary_json is absent, topic is
    derived from the first user message via a targeted single-row LIMIT 1 query.
    tool_counts is guarded by a PRAGMA table_info check (absent on pre-column DBs).
    score_raw is taken from branch_scores when provided (ranked path), else None.
    """
    if not branch_ids:
        return []

    # Guard tool_counts column — absent on DBs created before it was added
    cursor.execute("PRAGMA table_info(branches)")
    branch_col_names = {row[1] for row in cursor.fetchall()}
    has_tool_counts = "tool_counts" in branch_col_names
    tool_counts_col = ", b.tool_counts" if has_tool_counts else ""

    placeholders = ",".join("?" * len(branch_ids))
    rows = cursor.execute(
        f"""
        SELECT b.id as _branch_db_id, s.uuid as session_uuid,
               b.started_at, b.ended_at, b.exchange_count,
               b.files_modified, b.commits, s.git_branch,
               p.name as project, b.context_summary_json{tool_counts_col}
        FROM branches b
        JOIN sessions s ON b.session_id = s.id
        JOIN projects p ON s.project_id = p.id
        WHERE b.id IN ({placeholders})
        """,
        branch_ids,
    ).fetchall()

    branch_map: dict[int, tuple] = {row[0]: row for row in rows}

    cards: list[dict] = []
    for bid in branch_ids:
        row = branch_map.get(bid)
        if row is None:
            continue

        if has_tool_counts:
            (
                _branch_db_id,
                session_uuid,
                started_at,
                ended_at,
                exchange_count,
                files_json,
                commits_json,
                git_branch,
                project,
                summary_json,
                tool_counts_json,
            ) = row
        else:
            (
                _branch_db_id,
                session_uuid,
                started_at,
                ended_at,
                exchange_count,
                files_json,
                commits_json,
                git_branch,
                project,
                summary_json,
            ) = row
            tool_counts_json = None

        # Prefer join columns for list metadata; parse summary for topic
        files_modified: list = decode_json_column(files_json, [])
        commits: list = decode_json_column(commits_json, [])
        tool_counts: dict = decode_json_column(tool_counts_json, {}) if has_tool_counts else {}

        topic: str | None = None
        summary = decode_json_column(summary_json, {})
        if summary:
            topic = summary.get("topic") or None

        # Graceful degrade: no context_summary_json → first user message as topic
        if not topic:
            msg_row = cursor.execute(
                """
                SELECT m.content FROM branch_messages bm
                JOIN messages m ON bm.message_id = m.id
                WHERE bm.branch_id = ? AND m.role = 'user'
                ORDER BY m.timestamp ASC LIMIT 1
                """,
                (bid,),
            ).fetchone()
            if msg_row and msg_row[0]:
                topic = msg_row[0][:FALLBACK_TOPIC_MAX_CHARS]

        handle = session_uuid[:8] if session_uuid else ""
        score_raw = branch_scores.get(bid) if branch_scores else None

        cards.append(
            {
                "session_uuid": session_uuid,
                "handle": handle,
                "project": project,
                "git_branch": git_branch,
                "started_at": started_at,
                "ended_at": ended_at,
                "topic": topic,
                "exchange_count": exchange_count or 0,
                "files_modified": files_modified,
                "commits": commits,
                "tool_counts": tool_counts,
                "score_raw": score_raw,
            }
        )

    return cards
