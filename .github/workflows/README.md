# Workflow Release Model

## Normal CI

`meridian-ci.yml` runs on pull requests and pushes to `main`.

- PRs get a non-blocking release-label warning.
- The shared full gate is `scripts/preflight.sh full`.
- The local pre-push hook runs the same full gate before branch pushes.

## Auto-Release

`release-on-merge.yml` runs on every push to `main` (including merged PR commits),
but it only creates a release when the pushed commit is associated with a PR
that has a `release:*` label.

- `release:patch` or `release:stable` creates the next stable patch release.
- `release:rc` creates the next RC release (`vX.Y.Z-rc.N`).
- `release:skip` skips release.
- Any other `release:*` label defaults to RC release (safe default) and logs a notice.
- If labels conflict (for example `release:stable` + `release:rc`, or stable + unknown `release:*`), RC wins as the safe default.
- Missing `release:*` label skips release.
- Direct pushes to `main` skip release because there is no PR label to inspect.
- `release:skip` in the pushed head commit message also skips release.
- The workflow queries associated PRs via GitHub API and fails immediately if that API call fails.
- PR selection is deterministic: prefer merged PRs targeting `main` whose `merge_commit_sha` equals the trigger SHA; if none, fall back to merged PRs targeting `main`.
- If PR selection remains ambiguous (more than one merged candidate), the workflow fails instead of guessing labels.
- Labels are read only from the selected merged PR.
- Duplicate guard is release-kind aware: RC skips when this trigger already has RC or stable tag; stable skips only when a stable tag exists (so RC can still be promoted to stable).
- Release commits include `Release-Trigger: <trigger-sha>` in the commit body for exact trigger tracking.

When release is enabled, the workflow:

1. Runs `scripts/preflight.sh full`.
2. Bumps `src/meridian/__init__.py` (`X.Y.Z` stable, `X.Y.ZrcN` for RC).
3. Promotes `CHANGELOG.md` `[Unreleased]`.
4. Commits `release: vX.Y.Z` or `release: vX.Y.Z-rc.N`.
5. Creates and pushes `vX.Y.Z` or `vX.Y.Z-rc.N`.
6. Lets the tag push trigger `release.yml`.

`release-on-merge.yml` only creates release commits/tags. `release.yml` is the
only publish workflow identity, for both auto-release tags and manual backfill
tags. PyPI trusted publishing should be configured for `.github/workflows/release.yml`.

Reruns are idempotent by trigger marker:
- If the release tag already exists for the trigger, rerun does not recreate it.
- If the release commit exists but the expected tag is missing (partial success), rerun recreates/pushes the tag so `release.yml` can publish it.

## Manual Backfill

`release.yml` also runs on manual `v*` tag pushes.

Manual tags must point at a valid release commit (stable or RC):

- `src/meridian/__init__.py` matches the PyPI version form (`X.Y.Z` stable, `X.Y.ZrcN` for `X.Y.Z-rc.N` tags).
- `CHANGELOG.md` has the semver-tag release section (`## [X.Y.Z] - ...` or `## [X.Y.Z-rc.N] - ...`).
- Commit subject is `release: v<tag-version>`.
- The tagged commit is reachable from the default branch.

Use `scripts/manually-release.sh` for stable manual/backfill release commits. It
runs the shared preflight, blocks empty `[Unreleased]` releases, updates version
and changelog, creates the release commit, and tags the result.

`scripts/manually-release.sh` does **not** create RC release commits. Manual RC
backfill requires a pre-existing valid RC release commit and an RC tag push
(`vX.Y.Z-rc.N`) that satisfies `release.yml` provenance checks.
