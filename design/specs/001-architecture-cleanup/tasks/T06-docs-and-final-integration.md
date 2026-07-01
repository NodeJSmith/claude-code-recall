---
task_id: "T06"
title: "Update docs and final integration"
status: "planned"
depends_on: ["T02", "T03", "T04", "T05"]
implements: ["FR#1", "FR#2", "FR#3", "FR#4", "FR#5", "FR#6", "FR#7", "FR#8", "FR#9", "FR#10", "FR#11", "FR#12", "AC#1", "AC#2", "AC#3", "AC#4", "AC#5", "AC#6", "AC#7", "AC#8", "AC#9", "AC#10", "AC#11", "AC#12"]
---

## Summary

Make the cleanup coherent across docs, skills, release notes, and full-suite verification. Update user-facing docs for base vs semantic installs, remove token references, document the `search-messages` caveat, and run the final integrated checks across all requirements.

## Target Files

- modify: `README.md`
- modify: `CLAUDE.md`
- modify: `CHANGELOG.md`
- modify: `skills/ccr-recall/SKILL.md`
- modify: `skills/ccr-recall/references/tool-reference.md`
- modify: `skills/ccr-resume/SKILL.md`
- modify: `pyproject.toml`
- modify: `.claude-plugin/plugin.json`
- modify: `tests/test_integration.py`
- modify: `tests/test_cli_context.py`
- modify: `tests/test_health.py`
- read: `design/specs/001-architecture-cleanup/design.md`
- read: `design/specs/001-architecture-cleanup/tasks/context.md`
- read: `hooks/hooks.json`

## Prompt

Perform the final documentation and integration pass for the approved design.

Update `README.md` to explain base install and semantic extra install, FTS-only base `ccrecall search`, semantic fused search, `search-messages` behavior without semantic support, removal of `ccrecall tokens` and `/ccr-tokens`, updated data flow for import jobs, and updated DB table list including `exchanges`, `chunks`, `chunk_vec`, and `jobs`.

Update `CLAUDE.md` to reflect the new runtime module boundaries, optional semantic invariant, canonical exchanges, DB jobs, and removed token subsystem. Preserve the hook stdout/direct-entry-point invariants.

Update recall skill docs so agents understand that base installs use session-level keyword search and that `search-messages` is semantic-only with an explicit caveat when unavailable. Remove token skill references everywhere. Check `.claude-plugin/plugin.json`; the current generic “Conversation history and semantic search” wording may remain if the project still supports semantic search through the extra, but update it if the implementation changes the product description or keywords enough to make that wording misleading.

Add or adjust integration tests that exercise the combined behavior after all previous tasks: base import/search path without semantic dependencies, semantic-enabled fused behavior, token table preservation, exchange migration, import job enqueue, and hook stdout.

Run the full project verification commands from `CLAUDE.md` where feasible: `uv run pytest` and `uvx prek run --all-files`. If semantic optionality changes the dev install command, document the updated command in README/CLAUDE.

## Focus

Docs currently mention `/ccr-tokens`, `ccrecall tokens`, mandatory semantic dependencies, stale `branch_vec`, and token analytics tables. `skills/ccr-recall/SKILL.md` explicitly recommends `ccrecall search-messages --query`; update it to handle the new no-semantic caveat without adding exchange-level FTS fallback. `CHANGELOG.md` is historical; add release-note content only in the appropriate unreleased/current section if the repo style supports it.

## Verify

- [ ] FR#1: Docs and integration tests confirm base install supports hooks, recent, tail, context injection, and keyword search.
- [ ] FR#2: Docs/tests confirm base install does not require semantic native packages.
- [ ] FR#3: Docs/tests confirm semantic installs preserve fused search.
- [ ] FR#4: Docs/tests confirm `search-messages` unavailable caveat points to `ccrecall search -q ...`.
- [ ] FR#5: Docs/plugin references no longer expose token analytics.
- [ ] FR#6: Final tests confirm old token tables are preserved and ignored.
- [ ] FR#7: Final tests confirm exchanges are created without semantic support.
- [ ] FR#8: Final tests confirm chunks/vector search derive from exchanges.
- [ ] FR#9: Final tests confirm core conversation data is preserved through upgrade.
- [ ] FR#10: Final hook tests confirm JSON-only stdout.
- [ ] FR#11: Final tests confirm import has DB job dedupe/status.
- [ ] FR#12: Final docs/tests confirm jobs use one-shot worker, not daemon.
- [ ] AC#1: Missing-semantic integration path imports CLI/hooks/DB/search successfully.
- [ ] AC#2: Semantic-enabled search returns fused keyword/vector results for a query with both FTS and chunk-vector candidates.
- [ ] AC#3: `search-messages` no-semantic markdown and JSON include the caveat.
- [ ] AC#4: `ccrecall tokens` and `skills/ccr-tokens/` are absent.
- [ ] AC#5: Old token tables/rows survive schema setup.
- [ ] AC#6: Sync/import creates exchanges without semantic support.
- [ ] AC#7: Search/backfill snippet hydration uses `exchanges` joined to `chunks`.
- [ ] AC#8: Existing chunks promote/link to exchanges without reducing core table counts.
- [ ] AC#9: SessionStart and Stop hooks print only JSON envelopes.
- [ ] AC#10: Repeated setup enqueues at most one active import job by dedupe key, and later reimport can reopen a terminal `import:all` job.
- [ ] AC#11: One-shot worker marks import jobs running/succeeded/failed with `last_error`.
- [ ] AC#12: Resync removing an exchange removes exchange and derived chunk/vector rows.
