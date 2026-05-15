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

## Post-Merge Automation

After merge to `main`, CI (`.github/workflows/release-on-merge.yml`) will:

1. Compute the next patch version from existing `v*` tags
2. Update `src/meridian/__init__.py` + promote `CHANGELOG.md` `[Unreleased]`
3. Commit `Release X.Y.Z`, create/push `vX.Y.Z`
4. Trigger `.github/workflows/publish-pypi.yml` from the tag push

## Cleanup

After merge, clean merged worktrees with:

```bash
scripts/prune-worktrees.sh
```
