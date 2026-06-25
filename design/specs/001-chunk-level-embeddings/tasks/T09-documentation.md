---
task_id: "T09"
title: "Update skill, tool-reference, and CLAUDE.md for cards, snippets, and chunk schema"
status: "planned"
depends_on: ["T06", "T07", "T08"]
implements: ["FR#11"]
---

## Summary

Document the new recall surface so the `/ccr-recall` skill (the primary consumer) and human users
see the real output shapes: scored session cards (A), the new `search-messages` command for matched
exchanges (B), the `score`/`ranked` fields, the `--verbose`/`--status` semantics on the card path,
and the `chunks`/`chunk_vec` two-table schema in `CLAUDE.md`. Documentation-only; lands once here
(the absorbed contract's Documentation Updates overlap and are folded in).

## Target Files

- modify: `skills/ccr-recall/SKILL.md`
- modify: `skills/ccr-recall/references/tool-reference.md`
- modify: `CLAUDE.md`
- read: `design/specs/001-chunk-level-embeddings/output-format-contract.md`
- read: `design/specs/001-chunk-level-embeddings/design.md`
- read: `design/specs/001-chunk-level-embeddings/tasks/context.md`
- read: `src/ccrecall/cli/commands.py`
- read: `src/ccrecall/formatting.py`

## Prompt

Implement per design.md `## Documentation Updates` and `output-format-contract.md`
`## Documentation Updates`. Write the docs against the **actual shipped behavior** in
`src/ccrecall/cli/commands.py` and `src/ccrecall/formatting.py` (T06/T07 are done) — verify the
command name, flags, and field names from the code, not from memory.

1. **`skills/ccr-recall/references/tool-reference.md`** — replace the "Output" example (the full
   `### Conversation` transcript dump) with the **card** shape (A) and the **snippet** shape (B),
   in both markdown and JSON. Document:
   - `search` returns ranked session **cards** (score, project, git_branch, date, topic,
     disposition, counts, handle; no transcript) — `→ ccrecall tail <handle>` to drill in.
   - the new **`search-messages`** command: matched exchanges with `(handle, exchange_index,
     timestamp)` locator + bounded `user`/`assistant` excerpts.
   - the `score` (normalized 0–1, two decimals; `null` for a single-result set and on the unranked
     LIKE path) and `ranked` envelope fields, and markdown/JSON parity.
   - **`--verbose`** expands the card's `files_modified`/`commits` lists + `tool_counts` dict in
     markdown (JSON always carries the full lists per contract FR#10); **`--status`** now reports
     chunk coverage (current-version chunks / total) + branch-watermark coverage (not the old
     "embedded branches N/M").

2. **`skills/ccr-recall/SKILL.md`** — update the Tools/Workflow sections: `search` returns "ranked
   session cards" (A); add "matched exchanges" via `search-messages` (B); note `tail` as the
   full-fetch drill-in; mention `score`/`ranked`. Keep the skill's synthesis instructions (they
   still apply to the new shapes). Respect the namespacing note in `CLAUDE.md` (skills are invoked as
   `/ccrecall:ccr-recall` under a plugin install; `/ccr-recall` for a vendored install).

3. **`CLAUDE.md`** — in the Architecture section, update the `db.py`/`schema.py` description from the
   single `branch_vec` (1:1 with branches) to the `chunks` + `chunk_vec` two-table layout (chunk ==
   one exchange); note the hot-path framing is unaffected (embedding was already off the hook). Do
   not "fix" the deliberate `claude-code-recall`↔`ccrecall` name mismatch.

4. **CHANGELOG / GitHub** — no manual CHANGELOG entry (release-please handles it from Conventional
   Commits). Cross-linking #31/#32/PR #33/#34 is a GitHub-comment action, not a doc file — note it in
   the task completion summary; do not create a doc file for it.

## Focus

- Apply the writing-quality conventions: no em-dashes-as-AI-tell overuse, no hedging, no
  significance inflation; match the existing terse doc voice in `tool-reference.md` and `SKILL.md`.
- The card/snippet markdown templates and JSON objects are specified verbatim in
  `output-format-contract.md` (Track A / Track B sections) — mirror those exactly so the docs and the
  renderers agree field-for-field.
- Verify the `search-messages` command name and its flags against `cli/commands.py` as shipped by
  T07 (the design specifies `search-messages`, but confirm against code).
- `references/` subdirs are loaded on demand by the skill, not eagerly (CLAUDE.md Gotchas) — keep
  `tool-reference.md` self-contained.
- Documentation-only change → `docs:` Conventional Commit type (house nuance: instruction/doc files
  are `docs`, not `feat`).

## Verify

- [ ] FR#11: `tool-reference.md` and `SKILL.md` document the card (A), snippet (B), and envelope
      shapes exactly as `output-format-contract.md` defines them (field names, markdown/JSON parity,
      `score`/`ranked` semantics including single-result `null`), the `search-messages` command, and
      the `--verbose`/`--status` semantics; `CLAUDE.md` describes the `chunks`/`chunk_vec` schema.
