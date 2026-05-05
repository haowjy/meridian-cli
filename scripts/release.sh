#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/src/meridian/__init__.py"

usage() {
  cat <<'EOF'
Usage:
  scripts/release.sh patch [--push] [--remote origin] [--skip-ci]
  scripts/release.sh minor [--push] [--remote origin] [--skip-ci]
  scripts/release.sh major [--push] [--remote origin] [--skip-ci]
  scripts/release.sh rc [--push] [--remote origin]
  scripts/release.sh X.Y.Z [--push] [--remote origin] [--skip-ci]
  scripts/release.sh X.Y.Z-rc.N [--push] [--remote origin]

Bump types:
  patch   0.0.33 → 0.0.34
  minor   0.0.33 → 0.1.0
  major   0.0.33 → 1.0.0
  rc      0.0.33 → 0.0.34-rc.1, or 0.0.34-rc.1 → 0.0.34-rc.2

Behavior:
  - Runs local checks (ruff, pyright, tests)
  - Updates src/meridian/__init__.py
  - Creates a release commit
  - For stable releases: pushes commit, waits for CI green, then tags
  - For RCs: commits and tags immediately (no CI gate)
  - Use --skip-ci to bypass CI gate on stable releases

Examples:
  scripts/release.sh patch
  scripts/release.sh rc                    # Create release candidate
  scripts/release.sh rc --push             # Create and push RC
  scripts/release.sh 0.1.0 --push          # Explicit version (CI-gated)
  scripts/release.sh 0.1.0-rc.1            # Explicit RC version
  scripts/release.sh patch --skip-ci       # Skip CI gate
EOF
}

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

require_clean_tree() {
  local dirty_files
  dirty_files="$(git -C "$ROOT_DIR" status --short --ignore-submodules)"
  
  if [[ -z "$dirty_files" ]]; then
    return 0
  fi

  # Patterns that block release (actual code/config)
  local blocking_patterns="^.. src/|^.. tests/|^.. pyproject.toml|^.. scripts/"
  
  # Check if any dirty files match blocking patterns
  local blocking_files
  blocking_files="$(echo "$dirty_files" | grep -E "$blocking_patterns" || true)"
  
  if [[ -n "$blocking_files" ]]; then
    printf 'Uncommitted changes in release-critical paths:
%s
' "$blocking_files" >&2
    die "commit or stash these changes first"
  fi
  
  # Non-blocking dirty files (work artifacts, lock files, docs) — warn only
  printf 'Warning: uncommitted changes in non-critical paths (continuing):
%s

' "$dirty_files" >&2
}

require_branch() {
  local branch
  branch="$(git -C "$ROOT_DIR" branch --show-current)"
  if [[ -z "$branch" ]]; then
    die "release helper must run from a branch, not detached HEAD"
  fi
  printf '%s\n' "$branch"
}

read_current_version() {
  local line
  line="$(grep -E '^__version__ = "[^"]+"' "$VERSION_FILE" || true)"
  [[ -n "$line" ]] || die "could not read __version__ from $VERSION_FILE"
  printf '%s\n' "${line#*\"}" | sed 's/"$//'
}

validate_version() {
  local version="$1"
  [[ "$version" =~ ^[0-9]+(\.[0-9]+){2}(-rc\.[0-9]+)?$ ]] || \
    die "version must look like X.Y.Z or X.Y.Z-rc.N; got: $version"
}

next_version() {
  local bump="$1"
  local current="$2"
  
  # Strip any -rc.N suffix for base version parsing
  local base_version="${current%-rc.*}"
  IFS='.' read -r major minor patch <<<"$base_version"
  [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ && "$patch" =~ ^[0-9]+$ ]] || \
    die "automatic bumps require a plain semantic version base, got: $current"

  case "$bump" in
    patch) 
      patch=$((patch + 1))
      printf '%s\n' "$major.$minor.$patch"
      ;;
    minor) 
      minor=$((minor + 1)); patch=0 
      printf '%s\n' "$major.$minor.$patch"
      ;;
    major) 
      major=$((major + 1)); minor=0; patch=0 
      printf '%s\n' "$major.$minor.$patch"
      ;;
    rc)
      # If already an RC, increment RC number
      if [[ "$current" =~ -rc\.([0-9]+)$ ]]; then
        local rc_num="${BASH_REMATCH[1]}"
        rc_num=$((rc_num + 1))
        printf '%s\n' "$base_version-rc.$rc_num"
      else
        # New RC: bump patch and add -rc.1
        patch=$((patch + 1))
        printf '%s\n' "$major.$minor.$patch-rc.1"
      fi
      ;;
    *) 
      die "unknown bump kind: $bump" 
      ;;
  esac
}

write_version() {
  local version="$1"
  sed -i -E "s/^__version__ = \"[^\"]+\"/__version__ = \"$version\"/" "$VERSION_FILE"
}

promote_changelog() {
  local version="$1"
  local changelog="$ROOT_DIR/CHANGELOG.md"
  local today
  today="$(date +%Y-%m-%d)"

  if [[ ! -f "$changelog" ]]; then
    return 0
  fi

  if grep -q "^## \[Unreleased\]" "$changelog"; then
    sed -i "s/^## \[Unreleased\]/## [$version] - $today/" "$changelog"
    printf 'Promoting [Unreleased] to [%s] - %s\n' "$version" "$today"
  else
    printf 'Warning: no [Unreleased] section found in CHANGELOG.md\n'
  fi
}

wait_for_ci() {
  local remote="$1"
  local commit_sha="$2"
  local max_wait=600  # 10 minutes
  local poll_interval=15
  local elapsed=0

  if ! command -v gh >/dev/null 2>&1; then
    printf 'Warning: gh CLI not available — skipping CI gate\n'
    return 0
  fi

  # Resolve the repo from the remote URL
  local repo
  repo="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
  if [[ -z "$repo" ]]; then
    printf 'Warning: could not resolve repo from gh — skipping CI gate\n'
    return 0
  fi

  printf '  Polling %s for CI status on %s (timeout %ds)...\n' "$repo" "${commit_sha:0:8}" "$max_wait"

  while [[ $elapsed -lt $max_wait ]]; do
    local status
    status="$(gh api "repos/$repo/commits/$commit_sha/status" \
      --jq '.state' 2>/dev/null || echo "error")"

    local checks_conclusion
    checks_conclusion="$(gh api "repos/$repo/commits/$commit_sha/check-runs" \
      --jq '
        if (.check_runs | length) == 0 then "pending"
        elif [.check_runs[] | select(.conclusion != null)] | length < (.check_runs | length) then "pending"
        elif [.check_runs[] | select(.conclusion == "failure" or .conclusion == "cancelled")] | length > 0 then "failure"
        else "success"
        end
      ' 2>/dev/null || echo "error")"

    # Both commit status and check runs must pass
    if [[ "$checks_conclusion" == "success" && ("$status" == "success" || "$status" == "pending") ]]; then
      printf '\n  CI passed ✓\n'
      return 0
    fi

    if [[ "$checks_conclusion" == "failure" || "$status" == "failure" ]]; then
      printf '\n  CI failed ✗\n'
      return 1
    fi

    printf '.'
    sleep "$poll_interval"
    elapsed=$((elapsed + poll_interval))
  done

  printf '\n  CI timed out after %ds\n' "$max_wait"
  return 1
}

main() {
  [[ $# -ge 1 ]] || {
    usage
    exit 1
  }

  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
  esac

  local target="$1"
  shift

  local push_remote=""
  local remote="origin"
  local skip_ci=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --push)
        push_remote="1"
        shift
        ;;
      --remote)
        [[ $# -ge 2 ]] || die "--remote requires a value"
        remote="$2"
        shift 2
        ;;
      --skip-ci)
        skip_ci="1"
        shift
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
  done

  # No clean-tree check — concurrent agents routinely leave untracked/dirty
  # files, and this script only stages the version file explicitly.
  local branch
  branch="$(require_branch)"

  # Pre-release checks
  printf 'Running pre-release checks...\n'
  "$ROOT_DIR/scripts/preflight.sh" full
  printf '  frontend... '
  if command -v pnpm >/dev/null 2>&1 && [[ -d "$ROOT_DIR/../meridian-web" ]]; then
    make -C "$ROOT_DIR" build-frontend >/dev/null 2>&1 || die "frontend build failed"
    printf 'pass\n'
  else
    printf 'skip (pnpm or meridian-web not found)\n'
  fi

  local current_version
  current_version="$(read_current_version)"

  local next_version_value
  case "$target" in
    patch|minor|major|rc)
      next_version_value="$(next_version "$target" "$current_version")"
      ;;
    *)
      next_version_value="$target"
      ;;
  esac

  validate_version "$next_version_value"
  [[ "$next_version_value" != "$current_version" ]] || \
    die "next version matches current version ($current_version)"

  printf 'Current version: %s\n' "$current_version"
  printf 'Bumping to: %s\n' "$next_version_value"

  local tag="v$next_version_value"
  if git -C "$ROOT_DIR" rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    die "tag already exists: $tag"
  fi

  local is_rc=""
  if [[ "$next_version_value" =~ -rc\. ]]; then
    is_rc="1"
  fi

  write_version "$next_version_value"
  promote_changelog "$next_version_value"

  local commit_msg
  if [[ -n "$is_rc" ]]; then
    commit_msg="Release candidate $next_version_value"
  else
    commit_msg="Release $next_version_value"
  fi

  git -C "$ROOT_DIR" add "$VERSION_FILE"
  # Stage frontend assets if they were built
  if [[ -f "$ROOT_DIR/src/meridian/web_dist/index.html" ]]; then
    git -C "$ROOT_DIR" add src/meridian/web_dist/
  fi
  # Also stage changelog if it was modified (promote [Unreleased])
  if ! git -C "$ROOT_DIR" diff --quiet CHANGELOG.md 2>/dev/null; then
    git -C "$ROOT_DIR" add CHANGELOG.md
  fi
  git -C "$ROOT_DIR" commit -m "$commit_msg"

  # Stable releases: push commit first, wait for CI, then tag.
  # RCs: tag immediately, no CI gate.
  if [[ -n "$is_rc" || -n "$skip_ci" ]]; then
    # RC or --skip-ci: tag immediately
    git -C "$ROOT_DIR" tag -a "$tag" -m "$commit_msg"
    printf 'Released %s (tag: %s)\n' "$next_version_value" "$tag"

    if [[ -n "$push_remote" ]]; then
      git -C "$ROOT_DIR" push "$remote" "$branch"
      git -C "$ROOT_DIR" push "$remote" "$tag"
      printf 'Pushed branch %s and tag %s to %s\n' "$branch" "$tag" "$remote"
    else
      printf 'Run:\n'
      printf '  git push %s %s && git push %s %s\n' "$remote" "$branch" "$remote" "$tag"
    fi
  else
    # Stable release: CI gate before tagging
    printf 'Pushing commit to %s (CI gate before tagging)...\n' "$remote"
    git -C "$ROOT_DIR" push "$remote" "$branch"

    local commit_sha
    commit_sha="$(git -C "$ROOT_DIR" rev-parse HEAD)"
    printf 'Waiting for CI on %s...\n' "${commit_sha:0:8}"

    if ! wait_for_ci "$remote" "$commit_sha"; then
      printf '\nCI failed. The release commit is on %s but NOT tagged.\n' "$branch"
      printf 'Fix the issue, then either:\n'
      printf '  1. Push a fix and re-run: scripts/release.sh %s\n' "$next_version_value"
      printf '  2. Tag manually:  git tag -a %s -m "%s" && git push %s %s\n' \
        "$tag" "$commit_msg" "$remote" "$tag"
      die "CI gate failed — release aborted before tagging"
    fi

    git -C "$ROOT_DIR" tag -a "$tag" -m "$commit_msg"
    git -C "$ROOT_DIR" push "$remote" "$tag"
    printf 'Released %s (tag: %s)\n' "$next_version_value" "$tag"
  fi
}

main "$@"
