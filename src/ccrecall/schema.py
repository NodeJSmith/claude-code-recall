"""
Conversation-DB schema constants (SCHEMA_CORE/FTS5/FTS4) and FTS capability
detection.

This module has no ccrecall dependencies — only the stdlib sqlite3 module.
"""

import sqlite3

# Split into core (tables/indexes) and FTS variants for compatibility
SCHEMA_CORE = """
-- Projects table (derived from directory structure)
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  key TEXT UNIQUE NOT NULL,
  name TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_projects_key ON projects(key);

-- Sessions table (ONE row per session UUID)
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  uuid TEXT UNIQUE NOT NULL,
  project_id INTEGER REFERENCES projects(id),
  parent_session_id INTEGER REFERENCES sessions(id),
  git_branch TEXT,
  cwd TEXT,
  imported_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);

-- Branches table (one row per branch per session)
CREATE TABLE IF NOT EXISTS branches (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  leaf_uuid TEXT NOT NULL,
  fork_point_uuid TEXT,
  is_active INTEGER DEFAULT 1,
  started_at DATETIME,
  ended_at DATETIME,
  exchange_count INTEGER DEFAULT 0,
  files_modified TEXT,
  commits TEXT,
  tool_counts TEXT,
  aggregated_content TEXT,
  context_summary TEXT,
  context_summary_json TEXT,
  summary_version INTEGER DEFAULT 0,
  -- kept in ALTER-append order (trailing) to preserve SELECT * and positional access:
  embedding_version INTEGER DEFAULT 0,
  embedding_model TEXT,
  summary_version_at_embed INTEGER,
  UNIQUE(session_id, leaf_uuid)
);
CREATE INDEX IF NOT EXISTS idx_branches_session ON branches(session_id);
CREATE INDEX IF NOT EXISTS idx_branches_active ON branches(is_active);
CREATE INDEX IF NOT EXISTS idx_branches_summary_version ON branches(summary_version);
CREATE INDEX IF NOT EXISTS idx_branches_embedding_version ON branches(embedding_version);

-- Messages table (ALL messages stored ONCE per session)
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  uuid TEXT,
  parent_uuid TEXT,
  timestamp DATETIME,
  role TEXT CHECK(role IN ('user', 'assistant')),
  content TEXT NOT NULL,
  tool_summary TEXT,
  has_tool_use INTEGER DEFAULT 0,
  tool_content TEXT,
  has_thinking INTEGER DEFAULT 0,
  is_notification INTEGER DEFAULT 0,
  origin TEXT,
  UNIQUE(session_id, uuid)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session_uuid ON messages(session_id, uuid);
CREATE INDEX IF NOT EXISTS idx_messages_tool_content_null ON messages(session_id) WHERE tool_content IS NULL;

-- Branch-messages mapping (many-to-many)
CREATE TABLE IF NOT EXISTS branch_messages (
  branch_id INTEGER NOT NULL REFERENCES branches(id),
  message_id INTEGER NOT NULL REFERENCES messages(id),
  PRIMARY KEY (branch_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_branch_messages_message ON branch_messages(message_id);

-- Import tracking
CREATE TABLE IF NOT EXISTS import_log (
  id INTEGER PRIMARY KEY,
  file_path TEXT UNIQUE NOT NULL,
  file_hash TEXT,
  imported_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  messages_imported INTEGER DEFAULT 0,
  file_size INTEGER,
  file_mtime REAL
);

-- Chunk metadata table (per-exchange embedding store)
-- Source of truth for which chunk rowids belong to a branch, and the carrier
-- of the Track B locator (first_message_uuid, timestamp) plus bounded display
-- text (user_text, assistant_text).
CREATE TABLE IF NOT EXISTS chunks (
  id                INTEGER PRIMARY KEY,
  branch_id         INTEGER NOT NULL REFERENCES branches(id),
  exchange_index    INTEGER NOT NULL,
  content_hash      TEXT NOT NULL,
  first_message_uuid TEXT,
  timestamp         TEXT,
  user_text         TEXT,
  assistant_text    TEXT,
  was_capped        INTEGER NOT NULL DEFAULT 0,
  embedding_version INTEGER NOT NULL DEFAULT 0,
  embedding_model   TEXT,
  UNIQUE(branch_id, exchange_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_branch ON chunks(branch_id);
CREATE INDEX IF NOT EXISTS idx_chunks_version ON chunks(embedding_version);

"""

# FTS5 schema (best: porter stemming + unicode61, BM25 ranking)
SCHEMA_FTS5 = """
CREATE VIRTUAL TABLE IF NOT EXISTS branches_fts USING fts5(
  aggregated_content,
  content=branches,
  content_rowid=id,
  tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS branches_ai AFTER INSERT ON branches BEGIN
  INSERT INTO branches_fts(rowid, aggregated_content) VALUES (new.id, new.aggregated_content);
END;
CREATE TRIGGER IF NOT EXISTS branches_ad AFTER DELETE ON branches BEGIN
  INSERT INTO branches_fts(branches_fts, rowid, aggregated_content) VALUES('delete', old.id, old.aggregated_content);
END;
CREATE TRIGGER IF NOT EXISTS branches_au AFTER UPDATE ON branches BEGIN
  INSERT INTO branches_fts(branches_fts, rowid, aggregated_content) VALUES('delete', old.id, old.aggregated_content);
  INSERT INTO branches_fts(rowid, aggregated_content) VALUES (new.id, new.aggregated_content);
END;
"""

# FTS4 schema (fallback: porter stemming, no BM25 but supports MATCH + snippet)
SCHEMA_FTS4 = """
CREATE VIRTUAL TABLE IF NOT EXISTS branches_fts USING fts4(
  aggregated_content,
  content=branches,
  tokenize=porter
);

CREATE TRIGGER IF NOT EXISTS branches_ai AFTER INSERT ON branches BEGIN
  INSERT INTO branches_fts(rowid, aggregated_content) VALUES (new.id, new.aggregated_content);
END;
CREATE TRIGGER IF NOT EXISTS branches_ad AFTER DELETE ON branches BEGIN
  INSERT INTO branches_fts(branches_fts, rowid, aggregated_content) VALUES('delete', old.id, old.aggregated_content);
END;
CREATE TRIGGER IF NOT EXISTS branches_au AFTER UPDATE ON branches BEGIN
  INSERT INTO branches_fts(branches_fts, rowid, aggregated_content) VALUES('delete', old.id, old.aggregated_content);
  INSERT INTO branches_fts(rowid, aggregated_content) VALUES (new.id, new.aggregated_content);
END;
"""

# Combined schema (core + FTS5) for test fixtures and simple single-shot setup
SCHEMA = SCHEMA_CORE + SCHEMA_FTS5


def detect_fts_support(conn: sqlite3.Connection) -> str | None:
    """Detect the best available FTS extension."""
    try:
        opts = {row[0] for row in conn.execute("PRAGMA compile_options").fetchall()}
    except sqlite3.Error:
        return None
    if "ENABLE_FTS5" in opts:
        return "fts5"
    if "ENABLE_FTS4" in opts or "ENABLE_FTS3" in opts:
        return "fts4"
    return None
