# Releasing

meridian-cli stable releases are automatic on every normal push to `main`.

## What you do

1. Work in a worktree branch: `meridian work start "my-feature" --worktree`
2. Add changelog entries under `CHANGELOG.md` `[Unreleased]` as you commit
3. Open a PR using the PR template
4. Merge the PR to `main`

Direct pushes to `main` follow the same release path.

## What CI does

On push to `main`, `.github/workflows/release-on-merge.yml`:

- Computes the next patch version from git tags
- Updates `src/meridian/__init__.py`
- Promotes `CHANGELOG.md` `[Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
- Creates commit `Release X.Y.Z`
- Creates and pushes tag `vX.Y.Z`
- `publish-pypi.yml` fires on tag push

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
