"""Keyword (FTS5/FTS4/LIKE) query building for conversation search.

Owns the scope filter clause shared by every search rung (FTS, LIKE, chunk-KNN)
and the keyword branch-id lookup that ranks or recency-orders on it.
"""

import re
import sqlite3

from ccrecall.db import escape_like


def sanitize_fts_term(term: str) -> str:
    """Remove FTS special characters from search term.

    Strips characters that are FTS operators or special syntax:
    quotes, parentheses, asterisks, and FTS keywords.
    Hyphens are replaced with spaces so hyphenated identifiers
    (e.g. 'pytest-mock') match their FTS tokens correctly — the
    unicode61 tokenizer splits on hyphens, so 'pytest-mock' indexes
    as two tokens ('pytest', 'mock'). Stripping hyphens entirely
    would produce 'pytestmock', which matches nothing.
    Leading hyphens (FTS NOT shorthand) become harmless whitespace.
    """
    # Replace hyphens with spaces (handles both identifier separators
    # and leading NOT-operator hyphens)
    sanitized = term.replace("-", " ")
    # Remove remaining FTS operators: quotes, parens, asterisk, caret
    sanitized = re.sub(r'["\(\)*^]', "", sanitized)
    # Remove FTS keywords: NEAR, AND, OR, NOT (case-insensitive)
    sanitized = re.sub(r"\b(NEAR|AND|OR|NOT)\b", "", sanitized, flags=re.IGNORECASE)
    # Collapse whitespace and strip
    return re.sub(r"\s+", " ", sanitized).strip()


def scope_filter_clause(
    *,
    projects: list[str] | None,
    session_id: str | None,
    path: str | None,
) -> tuple[str, list]:
    """Return (sql_fragment, params) for the project/session/path WHERE filters.

    The fragment assumes the query's FROM aliases sessions as ``s`` and projects
    as ``p``. The fragment leads with a space when non-empty; params are ordered
    projects -> session -> path to match the clause order. Single source of truth
    for the scope predicates shared by the FTS, LIKE, and chunk-KNN queries.
    """
    clauses: list[str] = []
    params: list = []
    if projects:
        placeholders = ",".join("?" * len(projects))
        clauses.append(f"p.name IN ({placeholders})")
        params.extend(projects)
    if session_id:
        clauses.append("s.uuid LIKE ? ESCAPE '\\'")
        params.append(f"{escape_like(session_id)}%")
    if path:
        clauses.append("s.cwd LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like(path)}%")
    fragment = "".join(f" AND {clause}" for clause in clauses)
    return fragment, params


def get_fts_branch_ids(
    cursor: sqlite3.Cursor,
    query: str,
    fts_level: str | None,
    top_k: int,
    projects: list[str] | None = None,
    session_id: str | None = None,
    path: str | None = None,
) -> list[tuple[int, float | None]]:
    """Return ordered (branch_id, score_raw) pairs from FTS or LIKE search.

    score_raw is the negated SQLite bm25 (higher = better) on the fts5 rung — the
    only keyword rung with a relevance signal today. The LIKE fallback has no
    relevance signal (unranked, recency order), so its score_raw is None.
    The fts4 rung also lacks a relevance score in this landing — it is ordered by
    recency, not matchinfo, so its score_raw is None too and the caller surfaces
    it as unranked. Ranking fts4 by matchinfo is a deferred Track A gap (issue
    #35), not the contract's intended end state. Ordered by relevance (fts5) or
    recency (fts4/LIKE) descending. Empty when the query is empty or has no hits.
    """
    terms = query.split()
    if not terms:
        return []

    params: list = []

    if fts_level in ("fts5", "fts4"):
        sanitized_terms = [sanitize_fts_term(term) for term in terms]
        sanitized_terms = [t for t in sanitized_terms if t]
        if not sanitized_terms:
            return []
        fts_query = " OR ".join(f'"{term}"' for term in sanitized_terms)

        # fts5 carries a bm25 relevance score; fts4 has none (recency-ordered).
        score_col = ", bm25(branches_fts)" if fts_level == "fts5" else ", NULL"
        sql = f"""
            SELECT b.id{score_col}
            FROM branches_fts
            JOIN branches b ON branches_fts.rowid = b.id
            JOIN sessions s ON b.session_id = s.id
            JOIN projects p ON s.project_id = p.id
            WHERE b.is_active = 1
              AND branches_fts MATCH ?
        """
        params.append(fts_query)

        scope_sql, scope_params = scope_filter_clause(projects=projects, session_id=session_id, path=path)
        sql += scope_sql
        params.extend(scope_params)

        if fts_level == "fts5":
            sql += " ORDER BY bm25(branches_fts) LIMIT ?"
        else:
            sql += " ORDER BY b.ended_at DESC LIMIT ?"
        params.append(top_k)

    else:
        # LIKE fallback — no relevance signal, recency order, null score.
        like_clauses = " AND ".join("b.aggregated_content LIKE ?" for _ in terms)
        sql = f"""
            SELECT b.id, NULL
            FROM branches b
            JOIN sessions s ON b.session_id = s.id
            JOIN projects p ON s.project_id = p.id
            WHERE b.is_active = 1
              AND {like_clauses}
        """
        params.extend(f"%{term}%" for term in terms)

        scope_sql, scope_params = scope_filter_clause(projects=projects, session_id=session_id, path=path)
        sql += scope_sql
        params.extend(scope_params)

        sql += " ORDER BY b.ended_at DESC LIMIT ?"
        params.append(top_k)

    cursor.execute(sql, params)
    # bm25 is more-negative = better; negate so higher = better. NULL stays None.
    return [(row[0], -row[1] if row[1] is not None else None) for row in cursor.fetchall()]
