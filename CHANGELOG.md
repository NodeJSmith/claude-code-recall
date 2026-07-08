# Changelog

## [0.14.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.13.4...v0.14.0) (2026-07-08)


### Features

* include session ID in supplementary context blocks ([#57](https://github.com/NodeJSmith/claude-code-recall/issues/57)) ([eb488b8](https://github.com/NodeJSmith/claude-code-recall/commit/eb488b8e92c8a2361b85f6a8763ecbcc890d77fc))

## [0.13.4](https://github.com/NodeJSmith/claude-code-recall/compare/v0.13.3...v0.13.4) (2026-07-06)


### Refactoring

* delete dead subsystems, split db.py, restructure search ([#54](https://github.com/NodeJSmith/claude-code-recall/issues/54)) ([e01e536](https://github.com/NodeJSmith/claude-code-recall/commit/e01e53603488de571ed8fec58dab51fe4fafa52f))
* split oversized modules, fix tail sort, drop dead column ([#56](https://github.com/NodeJSmith/claude-code-recall/issues/56)) ([0b2bb23](https://github.com/NodeJSmith/claude-code-recall/commit/0b2bb2334fd54d3aefa3c8b001d88e06dd35267b))

## [0.13.3](https://github.com/NodeJSmith/claude-code-recall/compare/v0.13.2...v0.13.3) (2026-06-30)


### Bug Fixes

* clear-handoff hook missing stdout and improve logging defaults ([#51](https://github.com/NodeJSmith/claude-code-recall/issues/51)) ([954fc97](https://github.com/NodeJSmith/claude-code-recall/commit/954fc972ca5c30c4a37f5e2c573f714e6a18c192))

## [0.13.2](https://github.com/NodeJSmith/claude-code-recall/compare/v0.13.1...v0.13.2) (2026-06-30)


### Bug Fixes

* prevent OOM on first import of large project directories ([#49](https://github.com/NodeJSmith/claude-code-recall/issues/49)) ([3f5fc9f](https://github.com/NodeJSmith/claude-code-recall/commit/3f5fc9ffcb56ba0191396adcb842e39411afabf6)), closes [#48](https://github.com/NodeJSmith/claude-code-recall/issues/48)

## [0.13.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.13.0...v0.13.1) (2026-06-29)


### Bug Fixes

* remove incorrect "IN_PROGRESS" status labels — 57% of sessions were falsely labeled because the heuristic defaulted to in-progress for any session not matching narrow completion patterns ([#46](https://github.com/NodeJSmith/claude-code-recall/issues/46)) ([4020953](https://github.com/NodeJSmith/claude-code-recall/commit/4020953fa801e2476d8811d2f9f0c54608f7b1de))
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
