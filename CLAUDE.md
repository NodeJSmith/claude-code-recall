# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

`ccrecall` is conversation history and semantic search for Claude Code, shipped two ways from one repo:

- a **Python package** (`ccrecall` on PyPI) providing the `ccrecall` CLI and the hook console scripts, and
- a **Claude Code plugin** (`.claude-plugin/plugin.json`) providing the `/ccr-*` skills and the hook wiring (`hooks/hooks.json`).

It is an independent community project — not affiliated with Anthropic.

## Names (and one deliberate mismatch)

| Surface | Name |
|---|---|
| PyPI package / CLI binary / plugin | `ccrecall` |
| GitHub repo | `claude-code-recall` |
| Skills | `/ccr-recall`, `/ccr-resume`, `/ccr-tokens` |
| Hook entry points | `ccrecall-setup`, `ccrecall-sync`, `ccrecall-context`, `ccrecall-onboarding`, `ccrecall-clear-handoff` |
| Runtime data dir | `~/.ccrecall/` |

Under a plugin install, skills are namespaced by the plugin name — invoked as `/ccrecall:ccr-recall` etc. The bare `/ccr-recall` form is what the skill folders are named and what a non-plugin (vendored) install exposes.

The **GitHub repo** is `claude-code-recall` while everything else (package, CLI, plugin, data dir) is `ccrecall` — that one mismatch is deliberate: the repo name is more discoverable/descriptive, and renaming a published repo breaks clone URLs and stars. Do not "fix" it.

## Architecture

The hard dependency is on undocumented Claude Code internals (the `~/.claude/projects/<slug>/*.jsonl` transcript layout and the hook-event protocol). Anthropic changes these at patch cadence, so the design contains the coupling rather than spreading it:

- **One parse boundary.** `models.py` (Pydantic) + `parsing.py` own JSONL decoding; downstream code consumes typed objects, not raw transcript shapes. Keep new transcript knowledge here.
- **`db.py` / `schema.py`** — connection/config and the conversation-DB schema. `schema.py` holds `SCHEMA_CORE` as a single baseline (embedding DDL folded in), applied idempotently — there is no migration-DML ladder. The token-analytics schema lives in `token_schema.py` and is version-gated (a `SCHEMA_VERSION` bump triggers a full re-import rather than in-place migration). The conversations DB is a public contract now; don't evolve it in a way that silently loses a user's synced history.
- **`cli/`** — cyclopts app. Root `App` + `backfill` sub-`App`; commands live in `cli/commands.py` and self-register on import. A single global `--json` flag is the only output-format surface (carried by a frozen `CLIContext`); commands do not define their own `--json`.
- **`hooks/`** — the SessionStart/Stop/SessionEnd hook entry points plus the helpers they spawn (`import_conversations`, `sync_current`, `backfill_*`, `write_config`).
- **token_* modules** — independent token-usage analytics powering `/ccr-tokens`.

### Two invariants to preserve

1. **Hook stdout.** The hooks print `{"continue": true}` / `{}` to stdout for the harness. Never emit anything else to their stdout.
2. **Hook hot path.** Hooks are separate console scripts, *not* `ccrecall hook …` subcommands, because routing them through the cyclopts app eager-imports the whole command surface (fastembed/numpy/onnxruntime, ~1800ms) vs ~440ms for a direct hook import. The no-lazy-imports rule (see Conventions) means you cannot dodge that, so keep hooks as direct entry points.

## Conventions

Enforced by `prek` (pre-commit) hooks + custom checks in `tools/`:

- **No `from __future__ import annotations`** and **no lazy imports** (imports inside functions) — both have dedicated checks. Use `X | None`, not `Optional[X]`.
- **`whenever`** for all date/time, not stdlib `datetime` (convert only at library boundaries).
- **setuptools** build backend (never hatchling). License is declared as an SPDX expression (`license = "MIT"` + `license-files`).
- **Conventional Commits.** Releases are automated by **release-please**; the version lives in `pyproject.toml`, is mirrored into `.claude-plugin/plugin.json` (release-please `extra-files`), and `uv.lock` is re-locked on the release PR by a `sync-lockfile` CI job so its self-version never drifts. `feat`/`fix`/`perf`/`refactor`/`docs` land in the changelog.

## Commands

```bash
uv sync                       # install package + dev dependencies
uv run pytest                 # full test suite (the CI command)
uv run pytest -q --cov        # with coverage
uvx prek run --all-files      # lint, format, type-check, custom checks
uv build                      # build sdist + wheel
```

## Gotchas

- Skills under `skills/` are bundled into the plugin; their `references/` subdirs are loaded on demand by the skill, not eagerly.
