# Changelog

## [0.20.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.19.2...v0.20.0) (2026-07-24)


### Features

* ccrecall surfacing model — proactive alerts + reactive caveat ([#44](https://github.com/NodeJSmith/claude-code-recall/issues/44)) ([ce3a000](https://github.com/NodeJSmith/claude-code-recall/commit/ce3a0007f0cdfe17d31aafe4007b755f9c8eff6e))
* chunk-level embeddings — per-exchange vectors and fused scored search ([#36](https://github.com/NodeJSmith/claude-code-recall/issues/36)) ([0b305aa](https://github.com/NodeJSmith/claude-code-recall/commit/0b305aaf9a82badd1bb3da7d39c2e8b1a8a391dd))
* **cli:** improve agent ergonomics based on usage audit ([#69](https://github.com/NodeJSmith/claude-code-recall/issues/69)) ([e2d36cd](https://github.com/NodeJSmith/claude-code-recall/commit/e2d36cd40cd76b656ec04083bfccea668c7e2d41))
* **cli:** scaffold ccrecall cyclopts entry point ([effd09a](https://github.com/NodeJSmith/claude-code-recall/commit/effd09a28313fa1044c5712ddaf466555d62f33a))
* fix import OOM/perf issues, add debug logging ([#62](https://github.com/NodeJSmith/claude-code-recall/issues/62)) ([3433cbd](https://github.com/NodeJSmith/claude-code-recall/commit/3433cbd50ddf05027207bc800962fd571c95b9fc))
* honest branch-grain embedding coverage in stats and --status ([#43](https://github.com/NodeJSmith/claude-code-recall/issues/43)) ([98db6e3](https://github.com/NodeJSmith/claude-code-recall/commit/98db6e3a0054f44bff90a620e82eef3a4dd22c57))
* include session ID in supplementary context blocks ([#57](https://github.com/NodeJSmith/claude-code-recall/issues/57)) ([eb488b8](https://github.com/NodeJSmith/claude-code-recall/commit/eb488b8e92c8a2361b85f6a8763ecbcc890d77fc))
* index tool_use content for search and recall ([#77](https://github.com/NodeJSmith/claude-code-recall/issues/77)) ([fa5dd4c](https://github.com/NodeJSmith/claude-code-recall/commit/fa5dd4ca680086af1336b609d381d60df5886778))
* migrate pre-rename installs from ~/.claude-memory to ~/.ccrecall ([#25](https://github.com/NodeJSmith/claude-code-recall/issues/25)) ([90b77dd](https://github.com/NodeJSmith/claude-code-recall/commit/90b77dd88fe480d025cb7912c1f8049b1de37309))
* prepare for public release and PyPI publishing ([aa81a91](https://github.com/NodeJSmith/claude-code-recall/commit/aa81a914bf5224d60a66cdca35e682caf6581cda))
* show tool detail in tail output instead of bare tool names ([#73](https://github.com/NodeJSmith/claude-code-recall/issues/73)) ([440236c](https://github.com/NodeJSmith/claude-code-recall/commit/440236c81ff4a434ff6ac284eafba701600af3ef))
* **tail:** fall back to global search when selector not found locally ([#64](https://github.com/NodeJSmith/claude-code-recall/issues/64)) ([4cf7ac2](https://github.com/NodeJSmith/claude-code-recall/commit/4cf7ac20c0eab9ce4bf05a2ffa4bf8b1a5f76a04))
* validate untrusted JSON at ingest boundaries with pydantic ([3a8c5b5](https://github.com/NodeJSmith/claude-code-recall/commit/3a8c5b5cab86b367620d6fd076e3d7f9659ed631))
* validate untrusted JSON at ingest boundaries with pydantic ([6441e2b](https://github.com/NodeJSmith/claude-code-recall/commit/6441e2ba02a5920c8937ce849e65f3ed1b8e573f))


### Bug Fixes

* backfill resilience, batched embedding, and coverage nudge ([#85](https://github.com/NodeJSmith/claude-code-recall/issues/85)) ([4bfb9c8](https://github.com/NodeJSmith/claude-code-recall/commit/4bfb9c850b8ef6357eec5a7a8fcd77ac2f7703cf))
* clear pyright errors and enable the pyright guard ([642feab](https://github.com/NodeJSmith/claude-code-recall/commit/642feab5ff19aff50fd86e20c1e53d4d076d4cfe))
* clear pyright errors and enable the pyright guard ([097a6ca](https://github.com/NodeJSmith/claude-code-recall/commit/097a6caa8ba83297f7b1c3d31466294273e6593c))
* clear-handoff hook missing stdout and improve logging defaults ([#51](https://github.com/NodeJSmith/claude-code-recall/issues/51)) ([954fc97](https://github.com/NodeJSmith/claude-code-recall/commit/954fc972ca5c30c4a37f5e2c573f714e6a18c192))
* **cli:** decouple stats from import PID lifecycle, validate backfill ranges ([3f6f07b](https://github.com/NodeJSmith/claude-code-recall/commit/3f6f07bd32f2cbb0669082a47097f0ba8df78858))
* **cli:** restore pyright type-narrowing exposed by the argparse removal ([d2e16a3](https://github.com/NodeJSmith/claude-code-recall/commit/d2e16a3e10188b2c892e026aa72ff63852df5cf2))
* load sqlite-vec before cascade-triggering branch delete ([#60](https://github.com/NodeJSmith/claude-code-recall/issues/60)) ([30c31e4](https://github.com/NodeJSmith/claude-code-recall/commit/30c31e4a1d3bc61350561720c2a13fb85f73a7f2)), closes [#59](https://github.com/NodeJSmith/claude-code-recall/issues/59)
* narrow broad except Exception catches that hide bugs ([e9c3a75](https://github.com/NodeJSmith/claude-code-recall/commit/e9c3a7576e6029912bc4f656d9cd7cd6662dfaf8)), closes [#10](https://github.com/NodeJSmith/claude-code-recall/issues/10)
* narrow broad except Exception catches; add coverage tooling ([0235c68](https://github.com/NodeJSmith/claude-code-recall/commit/0235c684501e70a43ebe88fda3c7d7a7140965aa))
* prevent OOM on first import of large project directories ([#49](https://github.com/NodeJSmith/claude-code-recall/issues/49)) ([3f5fc9f](https://github.com/NodeJSmith/claude-code-recall/commit/3f5fc9ffcb56ba0191396adcb842e39411afabf6)), closes [#48](https://github.com/NodeJSmith/claude-code-recall/issues/48)
* price Opus 4.7/4.8 at the $5/$25 tier (issue [#37](https://github.com/NodeJSmith/claude-code-recall/issues/37)) ([#38](https://github.com/NodeJSmith/claude-code-recall/issues/38)) ([1241f91](https://github.com/NodeJSmith/claude-code-recall/commit/1241f91bb2a6f610a6e054f4496004f127eaeaa3))
* remove disposition field from session summaries ([#46](https://github.com/NodeJSmith/claude-code-recall/issues/46)) ([4020953](https://github.com/NodeJSmith/claude-code-recall/commit/4020953fa801e2476d8811d2f9f0c54608f7b1de))
* respect CLAUDE_CONFIG_DIR for transcript directory ([#75](https://github.com/NodeJSmith/claude-code-recall/issues/75)) ([ac33fe0](https://github.com/NodeJSmith/claude-code-recall/commit/ac33fe0c6a058357ed7bd923e22cd7afcc05ada9))
* stamp embedding watermark for zero-exchange branches ([#41](https://github.com/NodeJSmith/claude-code-recall/issues/41)) ([1a6f132](https://github.com/NodeJSmith/claude-code-recall/commit/1a6f1323438ce9726ce6a0b66f3d6b8f26e4d2f3))
* **tail:** scope session selection to worktree cwd ([#71](https://github.com/NodeJSmith/claude-code-recall/issues/71)) ([2d7119c](https://github.com/NodeJSmith/claude-code-recall/commit/2d7119cb57be779304c778bf2532a060942f6870))
* use structural is_error field for pending question detection ([#79](https://github.com/NodeJSmith/claude-code-recall/issues/79)) ([f9cc098](https://github.com/NodeJSmith/claude-code-recall/commit/f9cc0981a4de9219141f74d7d86a3770d4d58628))


### Refactoring

* address review findings (unify BUSY_TIMEOUT_MS, relocate escape_like) ([f43e140](https://github.com/NodeJSmith/claude-code-recall/commit/f43e14053484fe1957361742af67fa8d5a3e0f1e))
* clean up style/structure debt in search, recent, import, cli ([7448186](https://github.com/NodeJSmith/claude-code-recall/commit/744818684947251d94164e8b8affca6ada798fda))
* **cli:** address ccrecall CLI audit findings ([7740625](https://github.com/NodeJSmith/claude-code-recall/commit/7740625b13e8b03fd21e1979343f63771404e63a))
* **cli:** address review — drop backfill-embeddings argparse adapter ([ce03ab1](https://github.com/NodeJSmith/claude-code-recall/commit/ce03ab1f0976bd415d09401a022c90d17c1e31d6))
* **cli:** migrate cm-backfill-embeddings to ccrecall ([be95e7d](https://github.com/NodeJSmith/claude-code-recall/commit/be95e7daae082c6c3ca6010c048fc93c7524a9e7))
* **cli:** migrate cm-sync-current to `ccrecall sync-current` ([893493d](https://github.com/NodeJSmith/claude-code-recall/commit/893493d584a27e60a9daf5abe65de2d3aba0aafe))
* **cli:** migrate cm-write-config to ccrecall write-config ([a1ca4cc](https://github.com/NodeJSmith/claude-code-recall/commit/a1ca4cc6627b80f398c06c89948d8f7ffa09589f))
* **cli:** migrate import + backfill summaries to ccrecall ([ae4ee80](https://github.com/NodeJSmith/claude-code-recall/commit/ae4ee8030a9be00df657464589abe537825f4055))
* **cli:** migrate recent/search/tail/tokens to ccrecall ([915e33a](https://github.com/NodeJSmith/claude-code-recall/commit/915e33a625610f35accf2620706e5a1c8ae68b0a))
* **cli:** unify output format behind one global --json flag ([1a6d0d8](https://github.com/NodeJSmith/claude-code-recall/commit/1a6d0d88494bd3c50018f8b971166650353b0944))
* **cli:** unify output format behind one global --json flag ([7cbc191](https://github.com/NodeJSmith/claude-code-recall/commit/7cbc191c3c3bfbbfb322218bc71aeeb5076a9869))
* consolidate PID/temp-file handling and dedup hooks ([f8d273d](https://github.com/NodeJSmith/claude-code-recall/commit/f8d273debcebfd3ead27bb47dd3db1abdf3242db))
* dedup and extract shared helpers across core data/search ([c8f7f06](https://github.com/NodeJSmith/claude-code-recall/commit/c8f7f06c63fb72cf039c041776eff6128551769b))
* delete dead subsystems, split db.py, restructure search ([#54](https://github.com/NodeJSmith/claude-code-recall/issues/54)) ([e01e536](https://github.com/NodeJSmith/claude-code-recall/commit/e01e53603488de571ed8fec58dab51fe4fafa52f))
* extract shared session-uuid and json-column decode helpers ([d21a405](https://github.com/NodeJSmith/claude-code-recall/commit/d21a405f5b0c5d70171428d5ddf34b1ee5e7f6e3))
* extract shared session-uuid and json-column decode helpers ([2b4c75f](https://github.com/NodeJSmith/claude-code-recall/commit/2b4c75f849d14bd3d62ad67e28ad44af4fc96c7d))
* full clean-code pass across ccrecall ([#15](https://github.com/NodeJSmith/claude-code-recall/issues/15)) ([6bfb57b](https://github.com/NodeJSmith/claude-code-recall/commit/6bfb57bf3a83f74e535027b249c0f5568eb35a6f))
* name busy_timeout and clarify byte constant in migrations ([ebfec17](https://github.com/NodeJSmith/claude-code-recall/commit/ebfec178321c2f333b7a7a110eb6e188b138200e))
* name magics, dedup render loops, drop Args/Returns blocks ([d17a5f2](https://github.com/NodeJSmith/claude-code-recall/commit/d17a5f25574ccc1eab795eaed88afc2058f82a66))
* name token-subsystem magics and strip divider banners ([9e8bec8](https://github.com/NodeJSmith/claude-code-recall/commit/9e8bec8c511b8ac056f4eb71b18f80a097fbb903))
* split db.py into schema, migrations, and connection modules ([661db05](https://github.com/NodeJSmith/claude-code-recall/commit/661db0550328f3df071d766fe990dd2ab7cddce1))
* split db.py into schema, migrations, and connection modules ([7ca5d27](https://github.com/NodeJSmith/claude-code-recall/commit/7ca5d27d6198d7c4fe6b74e0cd4bd14764720979))
* split oversized modules, fix tail sort, drop dead column ([#56](https://github.com/NodeJSmith/claude-code-recall/issues/56)) ([0b2bb23](https://github.com/NodeJSmith/claude-code-recall/commit/0b2bb2334fd54d3aefa3c8b001d88e06dd35267b))
* token-subsystem, sync_session, migrations squash ([#20](https://github.com/NodeJSmith/claude-code-recall/issues/20)) ([#21](https://github.com/NodeJSmith/claude-code-recall/issues/21)) ([3c7b890](https://github.com/NodeJSmith/claude-code-recall/commit/3c7b890abe2c3a42824ba14d325fb5eb1ea455ec))


### Documentation

* **ccr-resume:** handle prose questions and reinterpret the argument ([f380584](https://github.com/NodeJSmith/claude-code-recall/commit/f38058400935dc7b2a9c8fcf8c72ed3b66b4e008))
* **cli:** point skills, README, and changelog at ccrecall subcommands ([3672076](https://github.com/NodeJSmith/claude-code-recall/commit/36720761ba6f0588c4049cd2a0af56f41dacfdb2))
* tighten redundant comments flagged in clean-code review ([4b699b9](https://github.com/NodeJSmith/claude-code-recall/commit/4b699b98a4db852d2fdf7eb2cd9a5cde715148cc))

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
