#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'USAGE'
Usage:
  scripts/manually-release.sh [patch|minor|major|X.Y.Z] [--push]
  scripts/manually-release.sh tag-current [--push]

Manual release helper for emergency/backfill releases.

Creates a real release commit before tagging. The normal path remains the
label-gated GitHub workflow.
USAGE
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

validate_version() {
  [[ "$1" =~ ^[0-9]+(\.[0-9]+){2}$ ]]
}

read_version() {
  grep -oP '(?<=__version__ = ")[^"]+' "$ROOT_DIR/src/meridian/__init__.py"
}

next_version() {
  local bump="$1"
  local current="$2"
  local major minor patch
  IFS='.' read -r major minor patch <<<"$current"

  case "$bump" in
    patch) patch=$((patch + 1)) ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    major) major=$((major + 1)); minor=0; patch=0 ;;
    *) die "unknown bump kind: $bump" ;;
  esac

  printf '%s.%s.%s\n' "$major" "$minor" "$patch"
}

require_clean_tree() {
  [[ -z "$(git -C "$ROOT_DIR" status --short)" ]] || die "working tree is not clean"
}

require_unreleased_entries() {
  awk '
    /^## \[Unreleased\]$/ { in_unreleased = 1; next }
    in_unreleased && /^## \[/ { in_unreleased = 0 }
    in_unreleased && /^- / { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$ROOT_DIR/CHANGELOG.md" || die "CHANGELOG.md [Unreleased] has no bullet entries"
}

promote_changelog() {
  local version="$1"
  local date="$2"
  local tmp_file
  tmp_file="$(mktemp)"
  awk -v version="$version" -v date="$date" '
    BEGIN { promoted = 0 }
    {
      if (!promoted && $0 == "## [Unreleased]") {
        print "## [Unreleased]"
        print ""
        print "## [" version "] - " date
        promoted = 1
        next
      }
      print
    }
  ' "$ROOT_DIR/CHANGELOG.md" >"$tmp_file"
  mv "$tmp_file" "$ROOT_DIR/CHANGELOG.md"
}

write_version() {
  local version="$1"
  sed -E -i "s/^__version__ = \".*\"$/__version__ = \"${version}\"/" \
    "$ROOT_DIR/src/meridian/__init__.py"
}

validate_current_release() {
  local version tag subject
  version="$(read_version)"
  validate_version "$version" || die "invalid version in src/meridian/__init__.py: $version"
  tag="v$version"
  subject="$(git -C "$ROOT_DIR" log -1 --format=%s)"

  [[ "$subject" == "Release $version" ]] || die "HEAD subject must be 'Release $version', got '$subject'"
  grep -q "^## \\[$version\\] -" "$ROOT_DIR/CHANGELOG.md" || die "CHANGELOG.md missing [$version] release section"
  printf '%s\n' "$tag"
}

create_tag() {
  local tag="$1"
  local head_sha existing_sha
  head_sha="$(git -C "$ROOT_DIR" rev-parse HEAD)"

  if git -C "$ROOT_DIR" rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    existing_sha="$(git -C "$ROOT_DIR" rev-list -n 1 "$tag")"
    [[ "$existing_sha" == "$head_sha" ]] || die "$tag already points at $existing_sha, expected $head_sha"
    printf 'Tag %s already exists at HEAD.\n' "$tag"
  else
    git -C "$ROOT_DIR" tag -a "$tag" -m "Release ${tag#v}"
    printf 'Created tag %s.\n' "$tag"
  fi
}

push_release() {
  local tag="$1"
  git -C "$ROOT_DIR" push origin HEAD:main
  git -C "$ROOT_DIR" push --no-verify origin "$tag"
}

main() {
  [[ $# -ge 1 ]] || { usage; exit 1; }

  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
  esac

  local command="$1"
  shift
  local push=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --push) push=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown argument: $1" ;;
    esac
  done

  cd "$ROOT_DIR"

  if [[ "$command" == "tag-current" ]]; then
    require_clean_tree
    tag="$(validate_current_release)"
    create_tag "$tag"
    [[ "$push" -eq 0 ]] || push_release "$tag"
    exit 0
  fi

  require_clean_tree
  require_unreleased_entries
  scripts/preflight.sh full

  local current target version today tag
  current="$(read_version)"
  case "$command" in
    patch|minor|major) version="$(next_version "$command" "$current")" ;;
    *) validate_version "$command" || die "unknown release target: $command"; version="$command" ;;
  esac

  [[ "$version" != "$current" ]] || die "target version matches current version: $version"
  tag="v$version"
  if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    die "tag already exists: $tag"
  fi

  today="$(date -u +%Y-%m-%d)"
  write_version "$version"
  promote_changelog "$version" "$today"
  git add src/meridian/__init__.py CHANGELOG.md
  git commit -m "Release $version"
  create_tag "$tag"
  [[ "$push" -eq 0 ]] || push_release "$tag"
}

main "$@"
