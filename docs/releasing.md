# Releasing

meridian-cli releases are automatic from normal PR merges to `main`. The PR's
`release:*` label picks the kind; no label defaults to a prerelease (RC).

## What you do

1. Create/select your code directory (for example `git worktree add ../meridian-cli.worktrees/my-feature -b my-feature`), then start the work item with `meridian work start "my-feature" --task-dir ../meridian-cli.worktrees/my-feature` (or run `meridian work task-dir ../meridian-cli.worktrees/my-feature` after `work start`)
2. Open a PR using the PR template
3. Write the changelog entry in the PR body's `## Changelog` section (the
   template's first section). The section needs the `## Changelog` heading plus
   the `### ...` blocks; a bare block list is not a notes file. Check it with
   `python3 scripts/changelog-notes.py validate --notes-file pr-body.md`. CI
   validates it whenever the body or branch changes and dry-runs the release
   renderer; the release job renders it. Do not edit `CHANGELOG.md` — no PR does.
4. Set one release label. The bump tracks breakage, not size: a change that
   does not break existing behaviour is a patch, however much it adds.
   - `release:rc` for the default safe prerelease path
   - `release:patch` or `release:stable` for a stable patch release (the
     normal label for features and fixes that break nothing)
   - `release:minor` / `release:major` for a release that breaks existing
     behaviour, config, or on-disk state
   - `release:skip` for no release commit from this merge; its `## Changelog`
     notes are collected by the next release, automatic or manual
5. Merge the PR to `main`

A merged PR with no `release:*` label defaults to a prerelease (RC). Direct pushes
to `main` skip auto-release because there is no associated PR to inspect.

Put `release:skip` in the pushed head commit message to skip auto-release even
when a release label is present.

### Breaking changes split across several PRs

Each PR merged to `main` with `release:minor` or `release:major` cuts its own breaking
release. When one piece of work breaks behaviour in several PRs (a stack, or lanes of one
feature), users should get one breaking release, not one per PR.

1. Create an integration branch from `main` (for example `release/<topic>`) and open its
   PR to `main` early, as a draft. That PR carries the `release:minor`/`release:major`
   label, the combined changelog and upgrade notes, and the whole-change review.
2. Point each part's PR at the integration branch, not `main`. Merging into it releases
   nothing, so those PRs need no release label. Copy their changelog sections into the
   integration PR's `## Changelog`: only commits on `main`'s first-parent history reach
   the renderer, so part PRs merged into the integration branch never render on their own.
3. When the last part lands and the combined change passes the full gate, mark the
   integration PR ready, merge it, and CI releases everything once.

Work that must ship separately (for example, a part another release depends on) gets its
own release. Decide that when the work is planned, not at merge time.

### Changes to on-disk state

A release that changes on-disk state (state or session file fields, index schema,
archive format, a one-time import or repair) must ship upgrade guidance. New state
that older builds ignore is still a patch; `release:minor` is for state changes an
older build cannot read:

- a `### Upgrade notes` block in the PR body's `## Changelog` section: what users must
  do or will notice, one line each, ending in an absolute link to the guide (the same
  text becomes a GitHub Release body, where relative links do not resolve);
- a section in [upgrading.md](upgrading.md) for that version: what changed and why,
  steps before and after installing, one-time imports and repairs and their output,
  data the harness or Meridian no longer keeps, and whether rollback works. Probe
  the rollback against a real previous release on a copy before writing it.

## Compatibility and versioning

Use these rules when changing a stored format, config key, hook name, or CLI flag.

- **Deprecation window.** A removed or renamed config key, hook name, or CLI flag
  keeps working for one minor release and warns with its replacement. It becomes
  an error in the following minor release. Track the removal in the owning
  directory's `.context/TODO`.
- **State.** A new build reads anything an older build wrote: migrate on read and
  persist the migrated form on the next write. An older build that finds newer
  data refuses it loudly with a remedy (for example, quarantine a row or refuse
  a file); it never substitutes defaults. Idle state is the exception: its files
  are short-lived and expected to be absent, and refusing one after a mid-session
  upgrade would block idle handling, so an unknown idle format warns and behaves
  as absent. Rollback is supported only where that version's
  [upgrade note](upgrading.md) says it is.
- **When to version.** Version a format only when a different build can read it:
  another machine through a synced repository, a process that outlives an
  upgrade, or a separately installed component such as Mars or a harness
  extension. Store an integer in the data and check it against the reader's
  supported set. Semver is for software people install (`meridian`, `mars`, and
  package `requires-*`). Delete a version nothing reads.
- **Rebuildable indexes.** Put both the projection version and any independently
  versioned input format in the file name, then rebuild on a mismatch. Do not
  migrate a disposable projection in place.
- **Adding a version.** Stage versioning for an unversioned format: readers learn
  the field one release before writers emit it.
- **Dependent fields.** Readers keep unknown fields through rewrites, so a new
  field whose meaning depends on another field requires bumping `v`: an older
  build that changes the field it depends on would keep a stale value.
- **Labels.** Keep the release-label rules above. A deprecation that still accepts
  the old input is not itself a breaking change.

## What CI does

On ready-for-review pull requests, `.github/workflows/pr-body.yml` validates the
`## Changelog` section whenever the body or branch changes and fails when it is
missing or malformed. Drafts are skipped; the integration PR is opened as a
draft, so it is validated when marked ready. The same step compares the branch's
`CHANGELOG.md` with the base branch's copy and fails when the PR adds content
under `## [Unreleased]` or a release heading (`## [X]`, including a second
`## [Unreleased]`); a typo fix inside a released section passes. `CHANGELOG.md`
belongs to the release job, so no PR needs to touch it.

On push to `main`, `.github/workflows/release-on-merge.yml`:

- Reads the selected merged PR label for the pushed commit
- Defaults unknown `release:*` labels to RC
- Skips the release when the triggering PR is `release:skip` or its commit has no associated PR; a merged PR with no `release:*` label defaults to RC. A `release:skip` merge's notes are not lost: the next release, automatic or manual, collects them.
- Computes the next stable (patch/minor/major) or next RC from git tags
- Collects the `## Changelog` section of every merged PR since the last release
  commit, including `release:skip` merges, deduplicated by PR number (a rebase
  merge owns several commits), and skips direct pushes; the walk is capped at 200
  commits and fails above that rather than dropping entries
- Updates `src/meridian/__init__.py`
- Renders those sections together into `CHANGELOG.md` as `## [X.Y.Z] - YYYY-MM-DD` or `## [X.Y.Z-rc.N] - YYYY-MM-DD`
- Creates commit `release: vX.Y.Z` or `release: vX.Y.Z-rc.N`
- Creates and pushes tag `vX.Y.Z` or `vX.Y.Z-rc.N`
- Lets the tag push trigger `.github/workflows/release.yml` for PyPI publish

If a `## Changelog` section is missing or malformed, the release fails before
committing: the error names the PR, so edit that merged PR body and re-run the
failed job. If the failure is content under `## [Unreleased]` (an old branch's
entry), move that text into the merged PR body, remove it from `CHANGELOG.md`
with a direct push to `main` (direct pushes release nothing), then re-run the
failed job. Re-running the failed job stays idempotent through the
`Release-Trigger` guard and the script's duplicate-version check.

A range above the 200-commit cap fails in the collect step. An intermediate
auto-release re-enters the same range, so cut it with a manual release that
carries the notes itself instead:

```bash
scripts/manually-release.sh patch --no-collect --notes-file notes.md --push
```

`release.yml` is the only PyPI trusted publishing workflow identity. Manual or
backfill `v*` tag pushes use the same workflow and provenance checks. It also
creates the GitHub Release for the tag, using the released `## [X.Y.Z]` section
as the release body. Re-running the failed job skips artifacts PyPI already has
(`skip-existing`) and edits the existing release.

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
scripts/manually-release.sh patch --notes-file notes.md --push   # bump, commit, tag, and push
scripts/manually-release.sh patch --notes-file notes.md          # same but skip the push
```

Like the release workflow, the script collects the `## Changelog` sections of
merged PRs since the last `release: v` commit, so a `release:skip` merge or a
failed release run does not lose its notes. `--notes-file` is optional and adds
its contents after the collected ones; it must contain the `## Changelog`
heading plus the blocks (or one `None. <reason>` line), the same shape a PR body
carries. `--notes-file -` reads standard input.

Pass `--no-collect` to release from `--notes-file` alone, for a direct-push-only
range or an offline machine. Nothing is collected then, so the notes file must
carry every merged PR's section since the last release, including `release:skip`
merges. Collection needs an authenticated `gh`; when it fails the script stops
and names `--no-collect` rather than releasing without those notes.

The dry run comes first: it refuses a stale `## [Unreleased]` or a duplicate
version before anything is written. The script then bumps `__version__` and
proves the bump landed (a reformatted version line stops it with a clean tree),
runs the full gate, renders the notes into `CHANGELOG.md` under
`## [X.Y.Z] - YYYY-MM-DD`, commits `release: vX.Y.Z`, creates the annotated tag,
and optionally pushes.

`scripts/manually-release.sh tag-current` recreates the tag when a release
commit exists without one.

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
- Do not edit `CHANGELOG.md` in a PR — the release job renders it from the merged PR body
- Do not create or push `v*` tags manually — CI owns tags
- The pre-push hook blocks direct `v*` tag pushes
