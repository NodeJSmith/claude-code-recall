# Changelog

## [0.10.0](https://github.com/NodeJSmith/claude-code-recall/compare/v0.9.0...v0.10.0) (2026-06-22)


### Features

* **cli:** scaffold ccrecall cyclopts entry point ([effd09a](https://github.com/NodeJSmith/claude-code-recall/commit/effd09a28313fa1044c5712ddaf466555d62f33a))
* prepare for public release and PyPI publishing ([aa81a91](https://github.com/NodeJSmith/claude-code-recall/commit/aa81a914bf5224d60a66cdca35e682caf6581cda))
* validate untrusted JSON at ingest boundaries with pydantic ([3a8c5b5](https://github.com/NodeJSmith/claude-code-recall/commit/3a8c5b5cab86b367620d6fd076e3d7f9659ed631))
* validate untrusted JSON at ingest boundaries with pydantic ([6441e2b](https://github.com/NodeJSmith/claude-code-recall/commit/6441e2ba02a5920c8937ce849e65f3ed1b8e573f))


### Bug Fixes

* clear pyright errors and enable the pyright guard ([642feab](https://github.com/NodeJSmith/claude-code-recall/commit/642feab5ff19aff50fd86e20c1e53d4d076d4cfe))
* clear pyright errors and enable the pyright guard ([097a6ca](https://github.com/NodeJSmith/claude-code-recall/commit/097a6caa8ba83297f7b1c3d31466294273e6593c))
* **cli:** decouple stats from import PID lifecycle, validate backfill ranges ([3f6f07b](https://github.com/NodeJSmith/claude-code-recall/commit/3f6f07bd32f2cbb0669082a47097f0ba8df78858))
* **cli:** restore pyright type-narrowing exposed by the argparse removal ([d2e16a3](https://github.com/NodeJSmith/claude-code-recall/commit/d2e16a3e10188b2c892e026aa72ff63852df5cf2))
* narrow broad except Exception catches that hide bugs ([e9c3a75](https://github.com/NodeJSmith/claude-code-recall/commit/e9c3a7576e6029912bc4f656d9cd7cd6662dfaf8)), closes [#10](https://github.com/NodeJSmith/claude-code-recall/issues/10)
* narrow broad except Exception catches; add coverage tooling ([0235c68](https://github.com/NodeJSmith/claude-code-recall/commit/0235c684501e70a43ebe88fda3c7d7a7140965aa))


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
* extract shared session-uuid and json-column decode helpers ([d21a405](https://github.com/NodeJSmith/claude-code-recall/commit/d21a405f5b0c5d70171428d5ddf34b1ee5e7f6e3))
* extract shared session-uuid and json-column decode helpers ([2b4c75f](https://github.com/NodeJSmith/claude-code-recall/commit/2b4c75f849d14bd3d62ad67e28ad44af4fc96c7d))
* full clean-code pass across ccrecall ([#15](https://github.com/NodeJSmith/claude-code-recall/issues/15)) ([6bfb57b](https://github.com/NodeJSmith/claude-code-recall/commit/6bfb57bf3a83f74e535027b249c0f5568eb35a6f))
* name busy_timeout and clarify byte constant in migrations ([ebfec17](https://github.com/NodeJSmith/claude-code-recall/commit/ebfec178321c2f333b7a7a110eb6e188b138200e))
* name magics, dedup render loops, drop Args/Returns blocks ([d17a5f2](https://github.com/NodeJSmith/claude-code-recall/commit/d17a5f25574ccc1eab795eaed88afc2058f82a66))
* name token-subsystem magics and strip divider banners ([9e8bec8](https://github.com/NodeJSmith/claude-code-recall/commit/9e8bec8c511b8ac056f4eb71b18f80a097fbb903))
* split db.py into schema, migrations, and connection modules ([661db05](https://github.com/NodeJSmith/claude-code-recall/commit/661db0550328f3df071d766fe990dd2ab7cddce1))
* split db.py into schema, migrations, and connection modules ([7ca5d27](https://github.com/NodeJSmith/claude-code-recall/commit/7ca5d27d6198d7c4fe6b74e0cd4bd14764720979))
* token-subsystem, sync_session, migrations squash ([#20](https://github.com/NodeJSmith/claude-code-recall/issues/20)) ([#21](https://github.com/NodeJSmith/claude-code-recall/issues/21)) ([3c7b890](https://github.com/NodeJSmith/claude-code-recall/commit/3c7b890abe2c3a42824ba14d325fb5eb1ea455ec))


### Documentation

* **cli:** point skills, README, and changelog at ccrecall subcommands ([3672076](https://github.com/NodeJSmith/claude-code-recall/commit/36720761ba6f0588c4049cd2a0af56f41dacfdb2))
* tighten redundant comments flagged in clean-code review ([4b699b9](https://github.com/NodeJSmith/claude-code-recall/commit/4b699b98a4db852d2fdf7eb2cd9a5cde715148cc))

## Changelog

## Unreleased

### Added

- Initial public release. ccrecall brings conversation history and semantic search to Claude Code, shipped both as a PyPI package (the `ccrecall` CLI plus hook entry points) and as a Claude Code plugin. Highlights:
  - Per-session sync of transcripts to a local SQLite database.
  - Start-of-session context injection summarizing your previous session.
  - Fused keyword + vector search over past conversations, via `/ccr-recall`.
  - Prior-session resume that recovers intent and unresolved decisions from the transcript tail, via `/ccr-resume`.
  - Token-cost analytics with an interactive HTML dashboard, via `/ccr-tokens`.
