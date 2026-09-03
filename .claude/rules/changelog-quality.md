# Changelog Quality (release-please)

This project uses [release-please](https://github.com/googleapis/release-please) to generate changelog entries from conventional commit messages. Every PR that lands on `main` becomes a changelog line item unless its type is excluded. Write commit messages (and therefore PR titles) with this in mind.

## Which types appear in the changelog

Only these types generate changelog entries (configured in `release-please-config.json`):

| Type | Changelog section | Use for |
|---|---|---|
| `feat` | Features | New user-facing functionality |
| `fix` | Bug Fixes | Something was broken, now it works |
| `perf` | Performance Improvements | Measurable performance gains |
| `refactor` | Refactoring | Structural changes notable enough for users to know about |
| `docs` | Documentation | User-facing documentation (README, public CLI/skill help text) |

These types are **excluded from the changelog**. Use them for internal work:

| Type | Use for |
|---|---|
| `chore` | Internal work: `design/`, `research/`, CLAUDE.md, deps, tooling, internal scripts |
| `ci` | CI/CD pipeline changes |
| `test` | Test infrastructure, coverage improvements |

**Key distinction:** `docs:` is for documentation that users read (README, skill help text, public docstrings on the `ccrecall` CLI or the `/ccr-*` skills). Work in `design/`, `.claude/`, research briefs, or internal tooling docs should use `chore:` so it stays out of the changelog.

## How release-please reads commits

| Source | What it becomes |
|---|---|
| Commit subject line | Changelog bullet point |
| `BREAKING CHANGE:` footer in commit body | Breaking change description |
| `feat!:` / `fix!:` bang in subject | Triggers "Breaking Changes" section header, but **only uses the subject line** if no `BREAKING CHANGE:` footer exists |

GitHub squash-merge uses the PR title as the commit subject and the PR body as the commit body. This repo is squash-merge-only (`mergeCommitAllowed`/`rebaseMergeAllowed` are both off), so the PR title is always what release-please parses.

## PR titles are changelog entries

The PR title becomes the one-line changelog entry that users read. Write it as a **user-facing description**, not a developer-facing one.

**Good** examples that tell a user what changed for them:
- `fix: cap sync-path embedding memory to prevent OOM freezes`
- `feat: add opt-in LLM branch resume briefs`
- `fix!: rename embedding-status.json reason codes to match health.py's mapping`

**Bad** examples with internal jargon, implementation details, or vague bundling:
- `refactor: session_ops.py decomposition into four modules`
- `fix: bundle five small-scope issue fixes`
- `chore: wave 3 structural decompositions (db.py, session_tail.py, embed_branch_chunks)` (fine as `chore`/internal, but would read as noise if ever typed `feat`/`fix`)

### Rules

1. **Imperative mood, lowercase.** `add X`, `fix Y`, not `Added X` or `Adds Y`.
2. **Describe the user-visible outcome.** What can the user now do, or what broke that's now fixed?
3. **No bundle PRs in the title.** If a PR bundles N fixes, the title should describe the theme, and individual items belong in the PR body or commit body where release-please won't pick them up.
4. **No internal-only entries.** If the PR is purely internal (CI, test infra, prior art research, design docs), use `chore:`, `ci:`, or `test:` type; these are excluded from the changelog entirely. Do not use `docs:` for internal documents (`design/`, `.claude/`, research briefs); `docs:` appears in the changelog and is reserved for user-facing documentation.

## Breaking changes MUST have a footer

When a PR contains a breaking change (`feat!:`, `fix!:`, `refactor!:`), the **PR body must end with a `BREAKING CHANGE:` footer** that explains:
1. What changed
2. What user code is affected
3. What the user needs to do

The footer goes at the very end of the PR body (after a blank line), and becomes the breaking change description in the changelog.

### Example PR body structure

```markdown
## Summary

<description of what the PR does>

## Breaking Changes

<detailed explanation for the PR reviewer>

BREAKING CHANGE: `ccrecall search` no longer accepts `--legacy-fts`; it now
always uses the hybrid FTS+vector path. Scripts passing that flag must drop it.
```

The `BREAKING CHANGE:` footer is a [conventional commit trailer](https://www.conventionalcommits.org/en/v1.0.0/#specification). It must be:
- Preceded by a blank line
- On its own line starting with `BREAKING CHANGE: ` (with the colon and space)
- Can span multiple lines (continuation lines are indented or just flow naturally)

### Multiple breaking changes

Do **not** use multiple `BREAKING CHANGE:` footers. Verified directly against release-please's
own commit parser (`src/commit.ts`, `@conventional-commits/parser`): only the *first* `BREAKING
CHANGE:` footer in a commit becomes a note; every subsequent one is silently dropped, not merged
and not appended. A PR with four separate footers will surface exactly one breaking-change bullet
in the changelog.

Use **one** `BREAKING CHANGE:` footer, with a `####` header + `- ` bulleted list for each
additional item on the lines that follow. This is release-please's documented extended-context
format (see `hasExtendedContext` in `commit.ts`) and is confirmed to survive parsing intact,
including all bullets. Real example, from this repo's own #134 (which predates this rule and used
a looser `**Breaking:**` inline note rather than a proper footer; a footer-conformant version would
have read):

```
BREAKING CHANGE: This release removes the deleted LLM summary enrichment subsystem's surface area.
#### Removed JSON fields
- `display_title` and `summary_preview` no longer appear in `ccrecall --json search` /
  `search-messages` card output; consumers reading either key will find it absent, not null.
#### Removed commands and config keys
- The `ccrecall backfill llm-summaries` command and `ccrecall-llm-summaries` console script are
  gone, as are the `llm_summaries_enabled`, `llm_summary_model`, `llm_summary_effort`,
  `llm_summary_timeout_seconds`, `llm_summary_max_budget_usd`, and `llm_summary_min_exchanges`
  config keys. Unknown keys in `config.json` are ignored, so stale entries are harmless.
```

## Pre-release changelog review

Before merging a release-please PR, review the generated changelog and manually edit the **CHANGELOG.md file** on the release-please branch to:

1. **Remove internal entries.** Prior art research, CI changes, test infrastructure, refactors with no user-visible behavior change.
2. **Expand vague entries.** If a commit subject is too terse, add context from the PR body.
3. **Group by feature area.** Reorganize flat lists into topic-grouped sections when a release has 5+ entries.
4. **Verify breaking change descriptions.** Ensure they tell the user what to do, not just what changed internally.

Use `/changelog-review` to drive this process; see `.claude/commands/changelog-review.md`.

### Do NOT edit the PR body (CRITICAL)

Only edit the `CHANGELOG.md` file on the release-please branch. **Never rewrite the PR description body on GitHub.**

Release-please uses its own PR body format (the `:robot: I have created a release *beep* *boop*` block) to recognize merged release PRs. After a release PR is squash-merged, release-please runs again, finds the PR by title, and parses the body to confirm it's a release PR. If the body doesn't match the expected format, release-please treats the merge as a normal commit: no tag, no GitHub Release, no publish.

This is a documented failure mode in projects that use this same command (hassette's v0.34.0: the PR body was rewritten to match the curated changelog, release-please couldn't parse it, and the release silently failed). Recovery required manually creating the tag, GitHub Release, and triggering the publish workflows.

**What to edit:** `CHANGELOG.md` on the branch (commit and push to the release-please branch)
**What to leave alone:** The PR description on GitHub; release-please owns that

### Recovery: manual release

If a release-please PR is merged but no tag/release appears:

1. Check the post-merge workflow run. Look for `✖ Pull request body did not match`.
2. Create the tag: `git tag v<version> <merge-commit-sha> && git push origin v<version>`
3. Create the GitHub Release: `gh release create v<version> --target <sha> --notes-file <changelog-excerpt>`
4. Trigger publish manually: re-run the "Release Please" workflow, or the equivalent publish job, for that tag
5. Close any spurious release-please PR that was opened for the next version
