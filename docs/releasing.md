# Releasing

meridian-cli stable releases are automatic on every normal push to `main`.

## What you do

1. Work in a worktree branch: `meridian work start "my-feature" --worktree`
2. Add changelog entries under `CHANGELOG.md` `[Unreleased]` as you commit
3. Open a PR using the PR template
4. Set one release label:
   - `release:patch`
   - `release:skip`
5. Merge the PR to `main`

PR merges without a `release:*` label skip auto-release. Direct pushes to `main`
also skip auto-release because there is no PR label to inspect.

Put `release:skip` in the pushed head commit message to skip auto-release even
when a release label is present.

## What CI does

On push to `main`, `.github/workflows/release-on-merge.yml`:

- Reads the PR label for the pushed commit
- Skips when no `release:*` label is present or when `release:skip` is present
- Computes the next patch version from git tags
- Updates `src/meridian/__init__.py`
- Promotes `CHANGELOG.md` `[Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
- Creates commit `Release X.Y.Z`
- Creates and pushes tag `vX.Y.Z`
- Calls `publish-pypi.yml` directly for PyPI publish

`publish-pypi.yml` also runs on manual/backfill `v*` tag pushes.
Manual tags must point at a valid release commit: version already bumped,
changelog promoted, and commit subject `Release X.Y.Z`.

The release workflow ignores its own `Release X.Y.Z` commit so the auto-commit
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
