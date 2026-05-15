## Why

<!-- Problem or motivation. What breakage, gap, or need makes this PR worth merging? -->

## Goal

<!-- Intended outcome. What should be true after this merges? -->

## Summary

<!-- What changed? Paste the agent-generated summary, then adjust for clarity. -->

## Work Item

<!-- Meridian work item slug, for example: worktree-pr-release-workflow -->

## Changes

<!-- Notable implementation details, behavior changes, risks, and follow-ups. -->

## Verification

<!-- What was run or checked? Include tests, smoke checks, type/lint, or why verification is not applicable. -->

## Knowledge Updates

<!-- Were .context/, KB, docs, or other durable knowledge artifacts updated?
     If not, note why (for example: no new behavior, docs not applicable). -->

## Spawn Trace

<!-- Direct/top-level Meridian spawn IDs only: role and short purpose, for example:
     - p123 coder — implemented catalog filtering
     - p124 qa-lead — reviewed/updated tests
     - p125 kb-lead — updated durable knowledge
-->

## Release Label Guide

Set one `release:*` label on this PR:

- `release:patch` — create the next patch release after merge
- `release:skip` — no release for this merge

No `release:*` label means no auto-release.

## Post-Merge Automation

After merge to `main`, CI (`.github/workflows/release-on-merge.yml`) will:

1. Read the PR release label
2. Skip when no `release:*` label is present or when `release:skip` is present
3. Compute the next patch version from existing `v*` tags
4. Update `src/meridian/__init__.py` + promote `CHANGELOG.md` `[Unreleased]`
5. Commit `Release X.Y.Z`, create/push `vX.Y.Z`
6. Run `.github/workflows/publish-pypi.yml` directly

## Cleanup

After merge, clean merged worktrees with:

```bash
scripts/prune-worktrees.sh
```
