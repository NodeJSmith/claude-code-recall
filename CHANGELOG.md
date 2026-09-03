# Changelog

## [0.24.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.23.0...v0.24.0) (2026-09-03)


### Features

* accept positional query on search commands and raise --max-results cap ([#191](https://github.com/NodeJSmith/claude-code-recall/issues/191)) ([a5cd9d2](https://github.com/NodeJSmith/claude-code-recall/commit/a5cd9d2d5c20e78450158e918c2fafc2e4cbc7f9)), closes [#68](https://github.com/NodeJSmith/claude-code-recall/issues/68) [#67](https://github.com/NodeJSmith/claude-code-recall/issues/67)


### Bug Fixes

* contain per-file import crashes so one poison transcript cannot wedge the batch ([#194](https://github.com/NodeJSmith/claude-code-recall/issues/194)) ([9e3e45e](https://github.com/NodeJSmith/claude-code-recall/commit/9e3e45e4861210564d3e748395125798e0cb87a2)), closes [#170](https://github.com/NodeJSmith/claude-code-recall/issues/170)
* emit envelope and can't-persist alert when ~/.ccrecall is unwritable ([#187](https://github.com/NodeJSmith/claude-code-recall/issues/187)) ([6738b99](https://github.com/NodeJSmith/claude-code-recall/commit/6738b99b8703fe7ad73c3741197783cf9badfbe3)), closes [#175](https://github.com/NodeJSmith/claude-code-recall/issues/175)
* key context-summary layout off the rendered exchange count ([#189](https://github.com/NodeJSmith/claude-code-recall/issues/189)) ([5508ee3](https://github.com/NodeJSmith/claude-code-recall/commit/5508ee37340d7a2769e6b7510c869aab25d8b9ae)), closes [#174](https://github.com/NodeJSmith/claude-code-recall/issues/174)
* recent --sort-order asc returns oldest sessions instead of recent-oldest-first ([#185](https://github.com/NodeJSmith/claude-code-recall/issues/185)) ([82f2517](https://github.com/NodeJSmith/claude-code-recall/commit/82f25177986fb3084ed1a6754af7debefe7910d5)), closes [#176](https://github.com/NodeJSmith/claude-code-recall/issues/176)
* reject null message at the parse boundary and harden extract_text_content against null text ([#193](https://github.com/NodeJSmith/claude-code-recall/issues/193)) ([709fb73](https://github.com/NodeJSmith/claude-code-recall/commit/709fb73f806731590a8b8fdc94076c589613a18f)), closes [#171](https://github.com/NodeJSmith/claude-code-recall/issues/171)
* scope sync-current sidecar clear so model-unavailable alerts survive ([#186](https://github.com/NodeJSmith/claude-code-recall/issues/186)) ([b8413ef](https://github.com/NodeJSmith/claude-code-recall/commit/b8413ef8d786600fa1528d1aea0bf06d8a00ee91)), closes [#164](https://github.com/NodeJSmith/claude-code-recall/issues/164)
* surface branch_messages constraint violations instead of INSERT OR IGNORE ([#188](https://github.com/NodeJSmith/claude-code-recall/issues/188)) ([5c46b40](https://github.com/NodeJSmith/claude-code-recall/commit/5c46b400793afa6a2dc4814436be35c7a5a99755)), closes [#158](https://github.com/NodeJSmith/claude-code-recall/issues/158)
* treat a slash-command turn as moving on from a rejected question ([#192](https://github.com/NodeJSmith/claude-code-recall/issues/192)) ([9949a47](https://github.com/NodeJSmith/claude-code-recall/commit/9949a47908932358120c292bce678aaa25f97233)), closes [#180](https://github.com/NodeJSmith/claude-code-recall/issues/180)


### Performance Improvements

* gate the tool-content repair loop on the pending-NULL set ([#190](https://github.com/NodeJSmith/claude-code-recall/issues/190)) ([1706524](https://github.com/NodeJSmith/claude-code-recall/commit/1706524aed06530dc49725e6b66b560a907a2b3a)), closes [#179](https://github.com/NodeJSmith/claude-code-recall/issues/179)

## [0.23.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.22.1...v0.23.0) (2026-08-24)


### ⚠ BREAKING CHANGES

* delete orphaned LLM summary enrichment subsystem ([#134](https://github.com/NodeJSmith/claude-code-recall/issues/134))

### Bug Fixes

* add logging to silent core modules and hooks ([#154](https://github.com/NodeJSmith/claude-code-recall/issues/154)) ([f0a5d21](https://github.com/NodeJSmith/claude-code-recall/commit/f0a5d2123a6162413ef43eb5f4c624f4c740b7af)), closes [#145](https://github.com/NodeJSmith/claude-code-recall/issues/145)
* cap sync-path embedding memory to prevent OOM freezes ([#166](https://github.com/NodeJSmith/claude-code-recall/issues/166)) ([9316ab2](https://github.com/NodeJSmith/claude-code-recall/commit/9316ab2fcf55bfa0c4d021ec7056ed440516d976))
* exclude hyphenated project names on lossy fallback path ([#135](https://github.com/NodeJSmith/claude-code-recall/issues/135)) ([002b5f1](https://github.com/NodeJSmith/claude-code-recall/commit/002b5f1ac26abeda7f49d4f6658244c7725d90d2))
* salvage three independent bug fixes from the abandoned recap branch ([#132](https://github.com/NodeJSmith/claude-code-recall/issues/132)) ([574d177](https://github.com/NodeJSmith/claude-code-recall/commit/574d17762bae40d04dd8d00148591774f1d69c25))
* warn when database schema is ahead of SCHEMA_VERSION ([#167](https://github.com/NodeJSmith/claude-code-recall/issues/167)) ([f3ca77b](https://github.com/NodeJSmith/claude-code-recall/commit/f3ca77b72f12a28ad5f81802f7c87a1be1e684ce))


### Refactoring

* delete orphaned LLM summary enrichment subsystem ([#134](https://github.com/NodeJSmith/claude-code-recall/issues/134)) ([4199480](https://github.com/NodeJSmith/claude-code-recall/commit/419948085cb39db7cea8d8f3b248ba102861de3d))
* extract shared backfill batch-loop and transcript tree-walk ([#153](https://github.com/NodeJSmith/claude-code-recall/issues/153)) ([83dbe3b](https://github.com/NodeJSmith/claude-code-recall/commit/83dbe3b6b46ac4f170906bd3be776ff2208d7168))
* wave 1 logging, dead code cleanup, test consolidation ([#148](https://github.com/NodeJSmith/claude-code-recall/issues/148)) ([cbfe0ce](https://github.com/NodeJSmith/claude-code-recall/commit/cbfe0cee1ca8ae269aa5ad0e7a1721cfde1c8457))
* wave 3 structural decompositions (db.py, session_tail.py, embed_branch_chunks) ([#149](https://github.com/NodeJSmith/claude-code-recall/issues/149)) ([fba4458](https://github.com/NodeJSmith/claude-code-recall/commit/fba4458c84fe452b36820f90987e8bbd219e9afe))
* wave 4 polish — consolidate patterns, CLI flags, logging ([#150](https://github.com/NodeJSmith/claude-code-recall/issues/150)) ([b4ac7f4](https://github.com/NodeJSmith/claude-code-recall/commit/b4ac7f439ece48975c2f501e88c9d98101dbd013))


### Documentation

* codebase health audit and attack plan ([#147](https://github.com/NodeJSmith/claude-code-recall/issues/147)) ([8a577e6](https://github.com/NodeJSmith/claude-code-recall/commit/8a577e6c407d4be31bd8bf022cd98940b375f6c4))

## [0.22.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.22.0...v0.22.1) (2026-08-09)


### Bug Fixes

* scope capability check to new transcripts, not all changes ([#112](https://github.com/NodeJSmith/claude-code-recall/issues/112)) ([9a0cb50](https://github.com/NodeJSmith/claude-code-recall/commit/9a0cb507316447d94fedf7e094b08b7bc6c95483))

## [0.22.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.21.1...v0.22.0) (2026-08-09)


### Features

* add opt-in LLM branch resume briefs ([#106](https://github.com/NodeJSmith/claude-code-recall/issues/106)) ([ab096e5](https://github.com/NodeJSmith/claude-code-recall/commit/ab096e5372c153c9fa6343cbac3d0616b7ad1f67))

## [0.21.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.21.0...v0.21.1) (2026-08-04)


### Bug Fixes

* recover scoped vector matches beyond initial KNN window ([45b09c4](https://github.com/NodeJSmith/claude-code-recall/commit/45b09c46c8d75843cb562ed4ff427b0c2fb4f642))


### Performance Improvements

* cache unchanged ingestion checks ([#105](https://github.com/NodeJSmith/claude-code-recall/issues/105)) ([64e8a39](https://github.com/NodeJSmith/claude-code-recall/commit/64e8a39fd9ad1742ae2170241630bf4c1bb36264))

## 2026-08-24

- fix a memory spike in sync-current that could freeze the machine (7.5 GB observed) by capping sync-path embedding at 4096 tokens, dropping the worst-case peak to ~4 GB (#166)
- warn when a database's schema version is ahead of what the running code expects, instead of silently treating it as fully migrated (#167)

## 2026-08-19

- surface previously-silent parsing, database, and hook failures to the per-process log files instead of failing silently (#154)

## 2026-08-14

- fix a crash on upgrading pre-v4 databases caused by an index referencing a column added by a later migration (#136)

## 2026-08-09

- fix false-positive capability check failures caused by unrelated, concurrent Claude Code sessions (#112)

## 2026-08-08

- add opt-in Claude-powered Branch Resume Brief enrichment (#106)
- harden transcript discovery and capability-gated enrichment fallback (#106)

## 2026-08-04

- cache confirmed-OK ingestion checks for faster repeated status audits (#105)
- recover scoped vector matches beyond the initial KNN window (#102)

## [0.21.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.20.0...v0.21.0) (2026-08-03)


### ⚠ BREAKING CHANGES

* remove deprecated ccrecall stats command ([#97](https://github.com/NodeJSmith/claude-code-recall/issues/97))

### Features

* add --before/--after filters to search commands ([#99](https://github.com/NodeJSmith/claude-code-recall/issues/99)) ([af666bd](https://github.com/NodeJSmith/claude-code-recall/commit/af666bd4a9f8c2aac620f1cc6cb6ab8fb7374e46))


### Refactoring

* remove deprecated ccrecall stats command ([#97](https://github.com/NodeJSmith/claude-code-recall/issues/97)) ([06f10a6](https://github.com/NodeJSmith/claude-code-recall/commit/06f10a62fb3bf49a25d4cbc87b897cf5c8ca47bb))


### Documentation

* remove duplicate changelog entry for 0.20.0 ([0f40f93](https://github.com/NodeJSmith/claude-code-recall/commit/0f40f93eed6d5aec4831a0740bc8a7cd2d9ee39b))

## [0.20.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.19.4...v0.20.0) (2026-07-30)


### Features

* add consolidated ingestion status ([#93](https://github.com/NodeJSmith/claude-code-recall/issues/93)) ([b94bcd2](https://github.com/NodeJSmith/claude-code-recall/commit/b94bcd243a98467a6a15b95c3f3553c6a6eed65a))

## [0.19.4](https://github.com/NodeJSmith/claude-code-recall/compare/v0.19.3...v0.19.4) (2026-07-28)


### Bug Fixes

* bound embedding inference memory to stop machine-killing OOM ([#90](https://github.com/NodeJSmith/claude-code-recall/issues/90)) ([cd13010](https://github.com/NodeJSmith/claude-code-recall/commit/cd1301063c312214b6a6f2de20937c80a538af07))


### Refactoring

* apply mechanical tech-debt cleanup from [#90](https://github.com/NodeJSmith/claude-code-recall/issues/90) review ([#92](https://github.com/NodeJSmith/claude-code-recall/issues/92)) ([e71d6d0](https://github.com/NodeJSmith/claude-code-recall/commit/e71d6d0cd8d7cca5982118b8cb53da098b0d0405))

## [0.19.3](https://github.com/NodeJSmith/claude-code-recall/compare/v0.19.2...v0.19.3) (2026-07-26)


### Bug Fixes

* use branch-aware fallback in ccrecall tail for worktree sessions ([#88](https://github.com/NodeJSmith/claude-code-recall/issues/88)) ([725d21e](https://github.com/NodeJSmith/claude-code-recall/commit/725d21e7f45da4372003469ace0c49ac36784edc))

## [0.19.2](https://github.com/NodeJSmith/claude-code-recall/compare/v0.19.1...v0.19.2) (2026-07-24)


### Bug Fixes

* backfill resilience, batched embedding, and coverage nudge ([#85](https://github.com/NodeJSmith/claude-code-recall/issues/85)) ([4bfb9c8](https://github.com/NodeJSmith/claude-code-recall/commit/4bfb9c850b8ef6357eec5a7a8fcd77ac2f7703cf))

## [0.19.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.19.0...v0.19.1) (2026-07-23)


### Bug Fixes

* use structural is_error field for pending question detection ([#79](https://github.com/NodeJSmith/claude-code-recall/issues/79)) ([f9cc098](https://github.com/NodeJSmith/claude-code-recall/commit/f9cc0981a4de9219141f74d7d86a3770d4d58628))

## [0.19.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.18.1...v0.19.0) (2026-07-23)


### Features

* index tool_use content for search and recall ([#77](https://github.com/NodeJSmith/claude-code-recall/issues/77)) ([fa5dd4c](https://github.com/NodeJSmith/claude-code-recall/commit/fa5dd4ca680086af1336b609d381d60df5886778))

## [0.18.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.18.0...v0.18.1) (2026-07-20)


### Bug Fixes

* respect CLAUDE_CONFIG_DIR for transcript directory ([#75](https://github.com/NodeJSmith/claude-code-recall/issues/75)) ([ac33fe0](https://github.com/NodeJSmith/claude-code-recall/commit/ac33fe0c6a058357ed7bd923e22cd7afcc05ada9))

## [0.18.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.17.1...v0.18.0) (2026-07-17)


### Features

* show tool detail in tail output instead of bare tool names ([#73](https://github.com/NodeJSmith/claude-code-recall/issues/73)) ([440236c](https://github.com/NodeJSmith/claude-code-recall/commit/440236c81ff4a434ff6ac284eafba701600af3ef))

## [0.17.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.17.0...v0.17.1) (2026-07-14)


### Bug Fixes

* **tail:** scope session selection to worktree cwd ([#71](https://github.com/NodeJSmith/claude-code-recall/issues/71)) ([2d7119c](https://github.com/NodeJSmith/claude-code-recall/commit/2d7119cb57be779304c778bf2532a060942f6870))

## [0.17.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.16.0...v0.17.0) (2026-07-13)


### Features

* **cli:** improve agent ergonomics based on usage audit ([#69](https://github.com/NodeJSmith/claude-code-recall/issues/69)) ([e2d36cd](https://github.com/NodeJSmith/claude-code-recall/commit/e2d36cd40cd76b656ec04083bfccea668c7e2d41))

## [0.16.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.15.0...v0.16.0) (2026-07-11)


### Features

* **tail:** fall back to global search when selector not found locally ([#64](https://github.com/NodeJSmith/claude-code-recall/issues/64)) ([4cf7ac2](https://github.com/NodeJSmith/claude-code-recall/commit/4cf7ac20c0eab9ce4bf05a2ffa4bf8b1a5f76a04))

## [0.15.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.14.1...v0.15.0) (2026-07-10)


### Features

* fix import OOM/perf issues, add debug logging ([#62](https://github.com/NodeJSmith/claude-code-recall/issues/62)) ([3433cbd](https://github.com/NodeJSmith/claude-code-recall/commit/3433cbd50ddf05027207bc800962fd571c95b9fc))

## [0.14.1](https://github.com/NodeJSmith/claude-code-recall/compare/v0.14.0...v0.14.1) (2026-07-10)


### Bug Fixes

* load sqlite-vec before cascade-triggering branch delete ([#60](https://github.com/NodeJSmith/claude-code-recall/issues/60)) ([30c31e4](https://github.com/NodeJSmith/claude-code-recall/commit/30c31e4a1d3bc61350561720c2a13fb85f73a7f2)), closes [#59](https://github.com/NodeJSmith/claude-code-recall/issues/59)

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
