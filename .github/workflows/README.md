# Workflow Release Model

## Normal CI

`meridian-ci.yml` runs on pull requests, pushes to `main`, manual dispatch and a
nightly schedule.

- PRs get a non-blocking release-label warning.
- `pr-body.yml` validates the PR body's `## Changelog` section on open, edit,
  synchronize, reopen and ready-for-review, and fails the PR when it is missing
  or malformed. Drafts are skipped (the integration PR opens as a draft). The
  same step compares `CHANGELOG.md` with the base branch's copy, so a PR that
  adds content under `## [Unreleased]` or a release heading (`## [X]`) fails
  before merge; `CHANGELOG.md` belongs to the release job.
- Routine CI and the local pre-push hook run the same `scripts/preflight.sh fast`
  gate over default pytest paths, lint and typecheck; its 60-second budget is a
  warning target and never fails or stops the gate.
- PRs, manual dispatches and nightly schedules also run
  `scripts/preflight.sh full`, which builds extensions, runs every Python test
  and builds packages; releases use it too. The compatibility job remains
  dispatch/nightly only.
- Both gates clear inherited pytest selection filters. Focused developer test
  commands remain available separately.

## Auto-Release

`release-on-merge.yml` runs on every push to `main` (including merged PR commits),
and creates a release commit/tag for the triggering merge unless that PR is
`release:skip` or has no associated PR.

- `release:patch` or `release:stable` creates the next stable patch release.
- `release:minor` / `release:major` creates the next stable minor/major release.
- `release:rc` creates the next RC release (`vX.Y.Z-rc.N`).
- `release:skip` skips the release commit for this merge; its notes are still
  collected by the next release, automatic or manual.
- Any other `release:*` label defaults to RC release (safe default) and logs a notice.
- If labels conflict (for example `release:stable` + `release:rc`, or stable + unknown `release:*`), RC wins as the safe default.
- Missing `release:*` label defaults to RC release.
- Direct pushes to `main` skip release because there is no PR label to inspect.
- `release:skip` in the pushed head commit message also skips release.
- The workflow queries associated PRs via GitHub API and fails immediately if that API call fails.
- PR selection is deterministic: prefer merged PRs targeting `main` whose `merge_commit_sha` equals the trigger SHA; if none, fall back to merged PRs targeting `main`.
- If PR selection remains ambiguous (more than one merged candidate), the workflow fails instead of guessing labels.
- Labels are read only from the selected merged PR.
- Duplicate guard is release-kind aware: RC skips when this trigger already has RC or stable tag; stable skips only when a stable tag exists (so RC can still be promoted to stable).
- Release commits include `Release-Trigger: <trigger-sha>` in the commit body for exact trigger tracking.

When release is enabled, the workflow:

1. Collects the `## Changelog` section of every merged PR since the last release
   commit (including `release:skip` merges, deduplicated by PR number, skipping
   direct pushes) and dry-runs the render, in seconds and before preflight;
   above 200 commits it fails instead of dropping entries, because the walk
   spends one API call per commit. A range that large is cut with
   `scripts/manually-release.sh <version> --no-collect --notes-file PATH`, which
   does not walk.
2. Runs `scripts/preflight.sh full`.
3. Bumps `src/meridian/__init__.py` (`X.Y.Z` stable, `X.Y.ZrcN` for RC) and
   renders the collected sections into `CHANGELOG.md` under
   `## [X.Y.Z] - YYYY-MM-DD` (or the RC heading).
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
runs the same notes walk as the release workflow by default, so merged PRs
(including `release:skip` ones) are not lost. `--notes-file PATH` is optional
and adds a PR-body-style notes file with the `## Changelog` heading, the `### ...`
blocks, or one `None. <reason>` line; `-` reads stdin. `--no-collect` releases
from `--notes-file` alone, so the file must then carry every merged PR's section
since the last release, `release:skip` merges included. It dry-runs the render,
proves the version bump landed, runs the shared preflight, renders the notes,
creates the release commit, and tags the result.

`scripts/manually-release.sh` does **not** create RC release commits. Manual RC
backfill requires a pre-existing valid RC release commit and an RC tag push
(`vX.Y.Z-rc.N`) that satisfies `release.yml` provenance checks.
