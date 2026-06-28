# Changelog

## [0.13.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.12.0...v0.13.0) (2026-06-28)


### Features

* ccrecall surfacing model — proactive alerts + reactive caveat ([#44](https://github.com/NodeJSmith/claude-code-recall/issues/44)) ([ce3a000](https://github.com/NodeJSmith/claude-code-recall/commit/ce3a0007f0cdfe17d31aafe4007b755f9c8eff6e))
* honest branch-grain embedding coverage in stats and --status ([#43](https://github.com/NodeJSmith/claude-code-recall/issues/43)) ([98db6e3](https://github.com/NodeJSmith/claude-code-recall/commit/98db6e3a0054f44bff90a620e82eef3a4dd22c57))


### Bug Fixes

* stamp embedding watermark for zero-exchange branches ([#41](https://github.com/NodeJSmith/claude-code-recall/issues/41)) ([1a6f132](https://github.com/NodeJSmith/claude-code-recall/commit/1a6f1323438ce9726ce6a0b66f3d6b8f26e4d2f3))

## [0.12.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.11.1...v0.12.0) (2026-06-26)


### Features

* chunk-level embeddings — per-exchange vectors and fused scored search ([#36](https://github.com/NodeJSmith/claude-code-recall/issues/36)) ([0b305aa](https://github.com/NodeJSmith/claude-code-recall/commit/0b305aaf9a82badd1bb3da7d39c2e8b1a8a391dd))


### Bug Fixes

* price Opus 4.7/4.8 at the $5/$25 tier (issue [#37](https://github.com/NodeJSmith/claude-code-recall/issues/37)) ([#38](https://github.com/NodeJSmith/claude-code-recall/issues/38)) ([1241f91](https://github.com/NodeJSmith/claude-code-recall/commit/1241f91bb2a6f610a6e054f4496004f127eaeaa3))

## [0.11.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.11.0...v0.11.1) (2026-06-24)


### Documentation

* **ccr-resume:** handle prose questions and reinterpret the argument ([f380584](https://github.com/NodeJSmith/claude-code-recall/commit/f38058400935dc7b2a9c8fcf8c72ed3b66b4e008))

## [0.11.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.10.0...v0.11.0) (2026-06-22)


### Features

* migrate pre-rename installs from ~/.claude-memory to ~/.ccrecall ([#25](https://github.com/NodeJSmith/claude-code-recall/issues/25)) ([90b77dd](https://github.com/NodeJSmith/claude-code-recall/commit/90b77dd88fe480d025cb7912c1f8049b1de37309))

## 0.10.0 (2026-06-22)

Initial public release. ccrecall brings conversation history and semantic search to Claude Code, shipped both as a PyPI package (the `ccrecall` CLI plus hook entry points) and as a Claude Code plugin. Highlights:

- Per-session sync of transcripts to a local SQLite database.
- Start-of-session context injection summarizing your previous session.
- Fused keyword + vector search over past conversations, via `/ccr-recall`.
- Prior-session resume that recovers intent and unresolved decisions from the transcript tail, via `/ccr-resume`.
- Token-cost analytics with an interactive HTML dashboard, via `/ccr-tokens`.
