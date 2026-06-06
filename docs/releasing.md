# Releasing

meridian-cli releases are automatic from normal PR merges to `main` when the PR
has a `release:*` label.

## What you do

1. Create/select your code directory (for example `git worktree add ../meridian-cli.worktrees/my-feature -b my-feature`), then start the work item with `meridian work start "my-feature" --task-dir ../meridian-cli.worktrees/my-feature` (or run `meridian work task-dir ../meridian-cli.worktrees/my-feature` after `work start`)
2. Add changelog entries under `CHANGELOG.md` `[Unreleased]` as you commit
3. Open a PR using the PR template
4. Set one release label:
   - `release:rc` for the default safe prerelease path
   - `release:patch` or `release:stable` for a stable patch release
   - `release:minor` / `release:major` for a stable minor/major release
   - `release:skip` for no release
5. Merge the PR to `main`

A merged PR with no `release:*` label defaults to a prerelease (RC). Direct pushes
to `main` skip auto-release because there is no associated PR to inspect.

Put `release:skip` in the pushed head commit message to skip auto-release even
when a release label is present.

## What CI does

On push to `main`, `.github/workflows/release-on-merge.yml`:

- Reads the selected merged PR label for the pushed commit
- Defaults unknown `release:*` labels to RC
- Skips only when `release:skip` is present or the commit has no associated PR; a merged PR with no `release:*` label defaults to RC
- Computes the next stable (patch/minor/major) or next RC from git tags
- Updates `src/meridian/__init__.py`
- Promotes `CHANGELOG.md` `[Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` or `## [X.Y.Z-rc.N] - YYYY-MM-DD`
- Creates commit `release: vX.Y.Z` or `release: vX.Y.Z-rc.N`
- Creates and pushes tag `vX.Y.Z` or `vX.Y.Z-rc.N`
- Lets the tag push trigger `.github/workflows/release.yml` for PyPI publish

`release.yml` is the only PyPI trusted publishing workflow identity. Manual or
backfill `v*` tag pushes use the same workflow and provenance checks.

The release workflow ignores its own `release: v...` commit so the auto-commit
does not recursively release again.

## Post-merge cleanup

```bash
scripts/prune-worktrees.sh --dry-run    # preview
scripts/prune-worktrees.sh --yes        # execute
```

## Backfill / direct-to-main

When commits land on `main` without a PR (direct push), auto-release is skipped —
there is no PR label to read. Use the manual release helper:

```bash
scripts/manually-release.sh patch --push   # bump, commit, tag, and push
scripts/manually-release.sh patch          # same but skip the push
```

The script runs full preflight, bumps `__version__`, promotes `CHANGELOG.md [Unreleased]`,
commits `release: vX.Y.Z`, creates the annotated tag, and optionally pushes.

**`push.followTags=true` gotcha:** the repo has `push.followTags=true`, so a plain
`git push origin HEAD:main` also attempts to push any local `v*` tag, which the
pre-push hook blocks. If the script's internal push fails for this reason, push
the branch and tag separately:

```bash
git push --no-follow-tags origin HEAD:main   # push the release commit
git push --no-verify origin vX.Y.Z           # push the tag (bypasses local v* guard)
```

## Boundaries

- Do not edit `__version__` — CI owns version bumps
- Do not create or push `v*` tags manually — CI owns tags
- The pre-push hook blocks direct `v*` tag pushes
