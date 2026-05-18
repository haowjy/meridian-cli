# Releasing

meridian-cli releases are automatic from normal PR merges to `main` when the PR
has a `release:*` label.

## What you do

1. Work in a worktree branch: `meridian work start "my-feature" --worktree`
2. Add changelog entries under `CHANGELOG.md` `[Unreleased]` as you commit
3. Open a PR using the PR template
4. Set one release label:
   - `release:rc` for the default safe prerelease path
   - `release:patch` or `release:stable` for stable patch release
   - `release:skip` for no release
5. Merge the PR to `main`

PR merges without a `release:*` label skip auto-release. Direct pushes to `main`
also skip auto-release because there is no PR label to inspect.

Put `release:skip` in the pushed head commit message to skip auto-release even
when a release label is present.

## What CI does

On push to `main`, `.github/workflows/release-on-merge.yml`:

- Reads the selected merged PR label for the pushed commit
- Defaults unknown `release:*` labels to RC
- Skips when no `release:*` label is present or when `release:skip` is present
- Computes the next stable patch or next RC from git tags
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

## Boundaries

- Do not edit `__version__` — CI owns version bumps
- Do not create or push `v*` tags manually — CI owns tags
- The pre-push hook blocks direct `v*` tag pushes
