# Releasing

Use the repo release helper:

```bash
scripts/release.sh prepare patch --push   # 0.0.33 → 0.0.34, push branch, wait for CI, tag on success
scripts/release.sh resume --push          # resume after fixing a failed prepared release
scripts/release.sh prepare rc --push      # 0.0.33 → 0.0.34-rc.1, no CI gate
scripts/release.sh prepare 0.2.0 --push   # explicit version
scripts/release.sh status                 # inspect prepared release state
scripts/release.sh abort                  # abandon prepared release state
```

Default to `patch`, especially while Meridian is still in `0.0.x`.
Omit `minor` and `major` in normal release flow. Use an explicit version only when the user asks for a larger version jump.

## Release Candidates

Use `rc` when you want to test a release before cutting the final version:

```bash
scripts/release.sh prepare rc --push      # 0.0.33 → 0.0.34-rc.1
scripts/release.sh prepare rc --push      # 0.0.34-rc.1 → 0.0.34-rc.2
scripts/release.sh prepare 0.0.34 --push  # graduate RC to final release
```

RCs are tagged and can be published to PyPI for testing. The commit message says "Release candidate X.Y.Z-rc.N" to distinguish from final releases.

## What the Script Does

`prepare`:

- Runs pre-release checks: `uv run ruff check .`, `uv run pyright`, `uv run python -m pytest -x -q`
- Bumps `src/meridian/__init__.py`
- Creates release commit `Release X.Y.Z` (or `Release candidate X.Y.Z-rc.N`)
- Optionally pushes the branch with `--push --remote <name>`
- For stable releases, waits for CI before tagging
- For RC releases, tags immediately without a CI gate

`resume`:

- Re-checks prepared release state after fix-forward work
- Waits for CI when needed
- Creates and optionally pushes annotated tag `vX.Y.Z` (or `vX.Y.Z-rc.N`)

`status` shows prepared release state. `abort` abandons it.

## Recommended Flow

1. Update `CHANGELOG.md`: move the release notes out of `[Unreleased]` into `## [X.Y.Z] - YYYY-MM-DD`, then open a fresh empty `[Unreleased]` above it.
2. Stage the exact release content you want included before running the script. The script explicitly adds the version file, then commits the current index.
3. Run `scripts/release.sh prepare patch --push` for normal releases, or `scripts/release.sh prepare rc --push` for release candidates.
4. If stable-release CI fails, fix forward, push the fix, then run `scripts/release.sh resume --push`.
5. Verify the result with `git show --stat HEAD` and `git rev-parse --verify vX.Y.Z`.

Example:

```bash
git add CHANGELOG.md path/to/release-fix.py path/to/release-test.py
scripts/release.sh prepare patch --push
```
