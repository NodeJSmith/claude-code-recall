"""
ccrecall — conversation memory package for Claude Code.

Submodules:
  db                — Database connection, vec operations
  config            — Paths, config/settings loading, PID files, and logging
  schema            — Conversation DB schema constants (SCHEMA_*) and FTS detection
  content           — Message content extraction and tool detection
  parsing           — JSONL parsing, branch detection, metadata extraction
  formatting        — Session formatting, time/path utilities
  project_ops       — Shared project upsert logic (cwd strategy + JSONL-probe strategy)
  session_ops       — Shared session import logic (used by sync and import pipelines)
"""
