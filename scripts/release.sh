#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/src/meridian/__init__.py"

usage() {
  cat <<'USAGE'
Usage:
  scripts/release.sh prepare [patch|minor|major|rc|X.Y.Z|X.Y.Z-rc.N] [--push] [--remote origin] [--skip-ci]
  scripts/release.sh resume [--push] [--remote origin]
  scripts/release.sh status
  scripts/release.sh abort

  # Shorthand (routes through prepare):
  scripts/release.sh patch --push
  scripts/release.sh rc --push

Bump types:
  patch   0.0.33 → 0.0.34
  minor   0.0.33 → 0.1.0
  major   0.0.33 → 1.0.0
  rc      0.0.33 → 0.0.34-rc.1, or 0.0.34-rc.1 → 0.0.34-rc.2

Behavior:
  Stable: prepare pushes branch, waits for CI, then auto-resumes to tag.
  RC: prepare commits, tags, and pushes immediately (no CI gate).
  --skip-ci: bypasses CI gate on stable releases.
  Resume: validates state and pushes the tag (for manual recovery after CI failure).

Examples:
  scripts/release.sh prepare patch
  scripts/release.sh prepare rc --push
  scripts/release.sh resume --push
  scripts/release.sh status
  scripts/release.sh abort
USAGE
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

release_dir() {
  local git_dir
  git_dir="$(git -C "$ROOT_DIR" rev-parse --absolute-git-dir)" || die "failed to resolve git dir"
  printf '%s\n' "$git_dir/meridian-release"
}

active_json() {
  printf '%s\n' "$(release_dir)/active.json"
}

auth_json() {
  printf '%s\n' "$(release_dir)/auth.json"
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

json_field() {
  local file="$1"
  local field="$2"
  grep -m 1 "^[[:space:]]*\"$field\"[[:space:]]*:" "$file" \
    | sed 's/^[[:space:]]*"[^"]*"[[:space:]]*:[[:space:]]*"\(.*\)"[[:space:]]*,\{0,1\}[[:space:]]*$/\1/'
}

write_active_json() {
  local file="$1"
  local version="$2"
  local tag="$3"
  local branch="$4"
  local release_commit="$5"
  local prepared_at="$6"
  local remote="$7"
  local skip_ci="$8"
  local is_rc="$9"

  mkdir -p "$(dirname "$file")" || die "failed to create release state directory"
  printf '{\n  "version": "%s",\n  "tag": "%s",\n  "branch": "%s",\n  "release_commit": "%s",\n  "prepared_at": "%s",\n  "remote": "%s",\n  "skip_ci": "%s",\n  "is_rc": "%s"\n}\n' \
    "$(json_escape "$version")" \
    "$(json_escape "$tag")" \
    "$(json_escape "$branch")" \
    "$(json_escape "$release_commit")" \
    "$(json_escape "$prepared_at")" \
    "$(json_escape "$remote")" \
    "$(json_escape "$skip_ci")" \
    "$(json_escape "$is_rc")" > "$file" || die "failed to write active release state"
}

write_auth_json() {
  local file="$1"
  local tag="$2"
  local target_sha="$3"
  local remote="$4"

  mkdir -p "$(dirname "$file")" || die "failed to create release state directory"
  printf '{\n  "tag": "%s",\n  "target_sha": "%s",\n  "remote": "%s",\n  "action": "create"\n}\n' \
    "$(json_escape "$tag")" \
    "$(json_escape "$target_sha")" \
    "$(json_escape "$remote")" > "$file" || die "failed to write tag auth state"
}

parse_release_options() {
  push_remote=""
  remote="origin"
  skip_ci=""

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
}

stage_release_files() {
  git -C "$ROOT_DIR" add "$VERSION_FILE"
  # Stage frontend assets if they were built
  if [[ -f "$ROOT_DIR/src/meridian/web_dist/index.html" ]]; then
    git -C "$ROOT_DIR" add src/meridian/web_dist/
  fi
  # Also stage changelog if it was modified (promote [Unreleased])
  if ! git -C "$ROOT_DIR" diff --quiet CHANGELOG.md 2>/dev/null; then
    git -C "$ROOT_DIR" add CHANGELOG.md
  fi
}

cmd_prepare() {
  [[ $# -ge 1 ]] || die "prepare requires a target version or bump kind"

  local target="$1"
  shift
  local push_remote remote skip_ci
  parse_release_options "$@"

  local branch
  branch="$(require_branch)"
  require_clean_tree

  local active_file
  active_file="$(active_json)"
  if [[ -f "$active_file" ]]; then
    die "release already active: $active_file"
  fi

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
  if [[ -n "$is_rc" && -n "$skip_ci" ]]; then
    die "--skip-ci is only valid for stable releases"
  fi

  write_version "$next_version_value"
  promote_changelog "$next_version_value"

  local commit_msg
  if [[ -n "$is_rc" ]]; then
    commit_msg="Release candidate $next_version_value"
  else
    commit_msg="Release $next_version_value"
  fi

  stage_release_files
  git -C "$ROOT_DIR" commit -m "$commit_msg"

  local release_commit
  release_commit="$(git -C "$ROOT_DIR" rev-parse HEAD)" || die "failed to read release commit"

  if [[ -n "$is_rc" || -n "$skip_ci" ]]; then
    git -C "$ROOT_DIR" tag -a "$tag" "$release_commit" -m "$commit_msg"
    printf 'Released %s (tag: %s)\n' "$next_version_value" "$tag"

    if [[ -n "$push_remote" ]]; then
      git -C "$ROOT_DIR" push "$remote" "$branch"
      # Write auth marker for the pre-push hook before pushing tag.
      local auth_file
      auth_file="$(auth_json)"
      write_auth_json "$auth_file" "$tag" "$release_commit" "$remote"
      if git -C "$ROOT_DIR" push "$remote" "$tag"; then
        rm -f "$auth_file"
        printf 'Pushed branch %s and tag %s to %s\n' "$branch" "$tag" "$remote"
      else
        printf 'Branch pushed but tag push failed; auth state left for retry.\n' >&2
        exit 1
      fi
    else
      printf 'Run:\n'
      printf '  git push %s %s && git push %s %s\n' "$remote" "$branch" "$remote" "$tag"
    fi
    return 0
  fi

  local prepared_at
  prepared_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  write_active_json "$active_file" "$next_version_value" "$tag" "$branch" "$release_commit" "$prepared_at" "$remote" "$skip_ci" "$is_rc"

  printf '\nPrepared release %s on branch %s\n' "$next_version_value" "$branch"
  printf 'Created release commit %s\n' "$release_commit"
  printf 'Wrote release state: %s\n' "$active_file"

  if [[ -n "$push_remote" ]]; then
    printf 'Pushing commit to %s (CI gate before tagging)...\n' "$remote"
    git -C "$ROOT_DIR" push "$remote" "$branch"
    printf 'Waiting for CI on %s...\n' "${release_commit:0:8}"

    if ! wait_for_ci "$remote" "$release_commit"; then
      printf '\nCI failed. The release commit is on %s but NOT tagged.\n' "$branch"
      printf 'Release state was left for recovery: %s\n' "$active_file"
      printf 'Fix the issue, then run:\n'
      printf '  scripts/release.sh resume --push --remote %s\n' "$remote"
      die "CI gate failed — release aborted before tagging"
    fi

    cmd_resume --push --remote "$remote"
  else
    printf '\nNext step: when ready, run:\n'
    printf '  scripts/release.sh resume --push --remote %s\n' "$remote"
  fi
}

cmd_resume() {
  local push_remote remote skip_ci
  parse_release_options "$@"
  [[ -z "$skip_ci" ]] || die "resume does not accept --skip-ci"

  local active_file auth_file
  active_file="$(active_json)"
  auth_file="$(auth_json)"
  [[ -f "$active_file" ]] || die "no active release: $active_file"

  local version tag branch release_commit prepared_remote is_rc
  version="$(json_field "$active_file" version)"
  tag="$(json_field "$active_file" tag)"
  branch="$(json_field "$active_file" branch)"
  release_commit="$(json_field "$active_file" release_commit)"
  prepared_remote="$(json_field "$active_file" remote)"
  is_rc="$(json_field "$active_file" is_rc)"

  [[ -n "$version" ]] || die "active release missing version"
  [[ -n "$tag" ]] || die "active release missing tag"
  [[ -n "$release_commit" ]] || die "active release missing release_commit"
  [[ -z "$is_rc" ]] || die "resume is only valid for stable releases"

  if [[ "$remote" = "origin" && -n "$prepared_remote" ]]; then
    remote="$prepared_remote"
  fi

  local current_version
  current_version="$(read_current_version)"
  [[ "$current_version" = "$version" ]] || die "$VERSION_FILE version $current_version does not match prepared version $version"

  git -C "$ROOT_DIR" merge-base --is-ancestor "$release_commit" HEAD \
    || die "current branch does not contain release commit $release_commit"

  local existing_tag_sha=""
  if git -C "$ROOT_DIR" rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    existing_tag_sha="$(git -C "$ROOT_DIR" rev-list -n 1 "$tag")" || die "failed to inspect existing tag $tag"
    [[ "$existing_tag_sha" = "$release_commit" ]] \
      || die "tag $tag already exists at $existing_tag_sha, expected $release_commit"
    printf 'Tag %s already exists at expected commit; skipping tag creation.\n' "$tag"
  else
    git -C "$ROOT_DIR" tag -a "$tag" "$release_commit" -m "Release $version" \
      || die "failed to create tag $tag"
    printf 'Created tag %s at %s.\n' "$tag" "$release_commit"
  fi

  write_auth_json "$auth_file" "$tag" "$release_commit" "$remote"
  printf 'Wrote tag auth state: %s\n' "$auth_file"

  if [[ -n "$push_remote" ]]; then
    if git -C "$ROOT_DIR" push "$remote" "$tag"; then
      rm -f "$auth_file" "$active_file" || die "tag pushed, but failed to clear release state"
      printf 'Pushed tag %s to %s.\n' "$tag" "$remote"
      printf 'Cleared release state.\n'
    else
      printf 'Failed to push tag %s to %s; release state left for retry.\n' "$tag" "$remote" >&2
      exit 1
    fi
  else
    printf '\nNothing pushed. Run:\n'
    printf '  git push %s %s\n' "$remote" "$tag"
    printf 'Then clear release state with scripts/release.sh abort if no longer needed.\n'
  fi
}

cmd_status() {
  local active_file
  active_file="$(active_json)"

  if [[ ! -f "$active_file" ]]; then
    printf 'No release in progress\n'
    return 0
  fi

  printf 'Release in progress:\n'
  printf '  version: %s\n' "$(json_field "$active_file" version)"
  printf '  tag: %s\n' "$(json_field "$active_file" tag)"
  printf '  branch: %s\n' "$(json_field "$active_file" branch)"
  printf '  commit: %s\n' "$(json_field "$active_file" release_commit)"
  printf '  prepared_at: %s\n' "$(json_field "$active_file" prepared_at)"
  printf '  remote: %s\n' "$(json_field "$active_file" remote)"
  printf '  state: %s\n' "$active_file"
}

cmd_abort() {
  local active_file auth_file tag
  active_file="$(active_json)"
  auth_file="$(auth_json)"
  tag=""

  if [[ -f "$active_file" ]]; then
    tag="$(json_field "$active_file" tag)"
    rm -f "$active_file" "$auth_file" || die "failed to clear release state"
    printf 'Cleared release state. Release commit remains in history.\n'
  else
    rm -f "$auth_file" || die "failed to clear tag auth state"
    printf 'No release in progress. Cleared tag auth state if present.\n'
  fi

  if [[ -n "$tag" ]] && git -C "$ROOT_DIR" rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    printf 'Notice: local tag %s still exists. Delete manually with: git tag -d %s\n' "$tag" "$tag"
  fi
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
    prepare)
      shift
      cmd_prepare "$@"
      ;;
    resume)
      shift
      cmd_resume "$@"
      ;;
    status)
      shift
      [[ $# -eq 0 ]] || die "status does not accept arguments"
      cmd_status
      ;;
    abort)
      shift
      [[ $# -eq 0 ]] || die "abort does not accept arguments"
      cmd_abort
      ;;
    patch|minor|major|rc)
      cmd_prepare "$@"
      ;;
    *)
      validate_version "$1"
      cmd_prepare "$@"
      ;;
  esac
}

main "$@"
