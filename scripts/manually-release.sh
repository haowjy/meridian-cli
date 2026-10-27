#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'USAGE'
Usage:
  scripts/manually-release.sh <patch|minor|major|X.Y.Z> --notes-file PATH [--push]
  scripts/manually-release.sh tag-current [--push]

Manual release helper for emergency/backfill releases.

Creates a real release commit before tagging. The normal path remains the
label-gated GitHub workflow.

--notes-file PATH is a PR-body-style notes file: the "## Changelog" heading
plus the ### Upgrade notes/Added/Changed/... blocks, or one "None. <reason>"
line. '-' reads standard input. The script renders the blocks under the new
version heading; it never promotes "## [Unreleased]", which must stay empty.
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

render_changelog() {
  local version="$1"
  local date="$2"
  local notes_file="$3"
  "$ROOT_DIR/scripts/changelog-notes.py" render \
    --notes-file "$notes_file" \
    --version "$version" \
    --date "$date" \
    --changelog "$ROOT_DIR/CHANGELOG.md"
}

check_render() {
  local version="$1"
  local date="$2"
  local notes_file="$3"
  "$ROOT_DIR/scripts/changelog-notes.py" render \
    --notes-file "$notes_file" \
    --version "$version" \
    --date "$date" \
    --changelog "$ROOT_DIR/CHANGELOG.md" \
    --dry-run >/dev/null
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

  [[ "$subject" == "release: v$version" ]] || die "HEAD subject must be 'release: v$version', got '$subject'"
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
    -*)
      die "the release command must come first: scripts/manually-release.sh patch --notes-file PATH"
      ;;
  esac

  local command="$1"
  shift
  local push=0
  local notes_file=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --push) push=1; shift ;;
      --notes-file)
        [[ $# -ge 2 ]] || die "--notes-file requires a path ('-' reads stdin)"
        notes_file="$2"
        shift 2
        ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown argument: $1" ;;
    esac
  done

  if [[ "$command" == "tag-current" ]]; then
    [[ -z "$notes_file" ]] || die "tag-current does not take --notes-file"
    cd "$ROOT_DIR"
    require_clean_tree
    tag="$(validate_current_release)"
    create_tag "$tag"
    [[ "$push" -eq 0 ]] || push_release "$tag"
    exit 0
  fi

  [[ -n "$notes_file" ]] || die "release commands require --notes-file PATH ('-' reads stdin)"
  if [[ "$notes_file" == "-" ]]; then
    stdin_notes="$(mktemp)"
    trap 'rm -f "$stdin_notes"' EXIT
    cat >"$stdin_notes"
    notes_file="$stdin_notes"
  else
    [[ -f "$notes_file" ]] || die "notes file not found: $notes_file"
    notes_file="$(cd "$(dirname "$notes_file")" && pwd)/$(basename "$notes_file")"
  fi

  cd "$ROOT_DIR"
  require_clean_tree
  "$ROOT_DIR/scripts/changelog-notes.py" validate --notes-file "$notes_file"

  local current version today tag
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
  check_render "$version" "$today" "$notes_file"
  scripts/preflight.sh full

  render_changelog "$version" "$today" "$notes_file"
  write_version "$version"
  git add src/meridian/__init__.py CHANGELOG.md
  git commit -m "release: v$version"
  create_tag "$tag"
  [[ "$push" -eq 0 ]] || push_release "$tag"
}

main "$@"
