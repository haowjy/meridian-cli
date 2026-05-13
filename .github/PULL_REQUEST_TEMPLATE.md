## Summary

<!-- What changed? Paste the agent-generated summary, then adjust for clarity. -->

## Work Item

<!-- Meridian work item slug, for example: worktree-pr-release-workflow -->

## Changes

<!-- Notable implementation details, behavior changes, risks, and follow-ups. -->

## Release Label Guide

Set one `release:*` label on this PR:

- `release:patch` — default release path (used automatically when no `release:*` label is present)
- `release:minor` — minor release bump
- `release:major` — major release bump
- `release:skip` — no release for this merge (docs/CI/meta-only)

## Post-Merge Automation

After merge to `main`, CI (`.github/workflows/release-on-merge.yml`) will:

1. Read the PR release label (default `release:patch` if unlabeled)
2. Compute next version from existing `v*` tags
3. Update `src/meridian/__init__.py` + promote `CHANGELOG.md` `[Unreleased]`
4. Commit `Release X.Y.Z`, create/push `vX.Y.Z`
5. Trigger `.github/workflows/publish-pypi.yml` from the tag push

## Cleanup

After merge, clean merged worktrees with:

```bash
scripts/prune-worktrees.sh
```
