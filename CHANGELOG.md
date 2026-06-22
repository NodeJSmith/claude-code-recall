# Changelog

## 0.10.0 (2026-06-22)

Initial public release. ccrecall brings conversation history and semantic search to Claude Code, shipped both as a PyPI package (the `ccrecall` CLI plus hook entry points) and as a Claude Code plugin. Highlights:

- Per-session sync of transcripts to a local SQLite database.
- Start-of-session context injection summarizing your previous session.
- Fused keyword + vector search over past conversations, via `/ccr-recall`.
- Prior-session resume that recovers intent and unresolved decisions from the transcript tail, via `/ccr-resume`.
- Token-cost analytics with an interactive HTML dashboard, via `/ccr-tokens`.
