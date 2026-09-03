# Changelog Review

Review and rewrite the release-please changelog PR before merging.

## Context

- release-please config: !`cat release-please-config.json 2>/dev/null`
- Changelog quality rule: !`cat .claude/rules/changelog-quality.md 2>/dev/null`

No `## [x.y.z]` release-please-generated section in `CHANGELOG.md` has been through this cleanup yet. Every one is still in release-please's raw, unedited format (flat `### Features` / `### Bug Fixes` with `([commit-sha])` links). The section you rewrite in Step 4 becomes the style reference for every `## [x.y.z]` review after it.

There is also a run of dated sections (`## 2026-08-24` down to `## 2026-08-04`) between v0.21.1 and v0.21.0, from before this repo adopted release-please headings. They're already short, user-facing bullets with `(#NNN)` refs and no commit SHAs: close to the target style already. Leave them alone; Step 5 only scans `## [x.y.z]` sections.

## Step 1: Find the release PR

Run `gh pr list --search "chore(main): release" --state open --json number,title,headRefName --limit 5`.

If no PR found, stop and tell the user. If multiple, ask which one.

## Step 2: Checkout and read

1. Fetch and checkout the release-please branch.
2. Read the new release section in `CHANGELOG.md` (the topmost `## [x.y.z]` heading).
3. List the commits in this release: `git log --oneline <prev-tag>..<base-commit>`.

## Step 3: Gather PR context

For each commit that produced a changelog entry, fetch its PR body via `gh pr view <number> --json title,body` using the `(#NNN)` reference in the commit subject. Focus on:

- What user-facing behavior changed
- Breaking change migration details
- Whether the change is internal-only

## Step 3.5: Check for external contributors

Run `uv run python scripts/release_contributors.py <prev-tag> <base-commit>` (using the same range from Step 3).

If external contributors are found, surface them as a finding:

> **External contributors detected. Ensure attribution in the changelog:**
>
> <script output>

When rewriting entries in Step 4, add "thanks @username!" (or "thanks Name!" if no GitHub username is available) to the relevant changelog bullet.

If no external contributors are found, continue silently.

## Step 4: Rewrite

Rewrite the release section per `.claude/rules/changelog-quality.md`:

**Remove entirely:**
- `docs:` entries for prior art research, internal design docs
- `ci:` entries (CI pipeline changes)
- `test:` entries (test infrastructure, not test utilities shipped to users)
- `chore:` entries (gitignore, changelog meta, dependency bumps)
- `refactor:` entries with no user-visible behavior change
- Internal framework plumbing that users never interact with

**Keep and rewrite:**
- `feat:` entries → describe what users can now do
- `fix:` entries → describe what was broken and is now fixed
- `perf:` entries → describe what got faster
- `docs:` entries for user-facing docs (README, `/ccr-*` skill help text, CLI `--help` output)
- `refactor:` entries that change user-facing APIs (CLI flags, hook contracts, skill behavior)

**Breaking changes:**
Each must explain (1) what changed, (2) what user code is affected, (3) what to do. Use field-by-field details when types changed. Put these in a `### Breaking Changes` section at the top.

**Grouping:**
When 5+ entries remain, group by feature area with `### Section` headers:
- `### Breaking Changes` (always first if present)
- Topic sections like `### Search`, `### Embeddings`, `### Hooks`, `### Sync`, `### CLI`
- `### Bug Fixes` (always last)
- `### Documentation` (only if user-facing docs changed)

**Format:**
- `- ` bullets with bold lead-in for breaking changes
- Issue references as `(#NNN)`, no commit SHAs
- Preserve the `## [x.y.z](compare-link) (date)` heading exactly

## Step 5: Check older releases

Scan the older `## [x.y.z]` sections in `CHANGELOG.md` (skip the dated `## YYYY-MM-DD` legacy sections; see Context above, they don't need this treatment). If any `## [x.y.z]` section still has the raw release-please format (flat `### Features` / `### Bug Fixes` with `([commit-sha])` links), ask:

```
AskUserQuestion:
  question: "Older releases (listed below) still have raw release-please formatting. Clean those up too?"
  header: "Older entries"
  multiSelect: false
  options:
    - label: "Yes, clean them all"
      description: "Apply the same rewrite to older unreviewed releases"
    - label: "No, just this release"
      description: "Only edit the new release section"
```

## Step 6: Push

1. Show a summary: entries removed, entries rewritten, breaking changes added.
2. Ask for approval:

```
AskUserQuestion:
  question: "Ready to push the rewritten changelog to the release-please branch?"
  header: "Push"
  multiSelect: false
  options:
    - label: "Push it"
      description: "Commit and push to the release-please branch"
    - label: "Show the diff"
      description: "Show the full diff first, then ask again"
```

3. Commit with `docs: rewrite changelog with user-facing descriptions` and push.
