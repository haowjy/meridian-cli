#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'USAGE'
Usage:
  scripts/prune-worktrees.sh [--dry-run] [--yes]

Options:
  --dry-run   Show merged worktrees that would be pruned; make no changes.
  --yes       Skip confirmation prompt and prune immediately.
  -h, --help  Show this help.
USAGE
}

log() {
  printf '%s\n' "$*"
}

warn() {
  printf 'warning: %s\n' "$*" >&2
}

derive_slug() {
  local branch="$1"

  if [[ "$branch" == feature/* ]]; then
    printf '%s\n' "${branch#feature/}"
    return
  fi

  if [[ "$branch" == work/* ]]; then
    printf '%s\n' "${branch#work/}"
    return
  fi

  if [[ "$branch" == wt/* ]]; then
    printf '%s\n' "${branch#wt/}"
    return
  fi

  printf '%s\n' ""
}

DRY_RUN=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --yes)
      ASSUME_YES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      warn "unknown argument: $1"
      usage >&2
      exit 2
      ;;
  esac
done

cd "$ROOT_DIR"

git rev-parse --git-dir >/dev/null 2>&1 || {
  warn "not a git repository: $ROOT_DIR"
  exit 1
}

if ! git show-ref --verify --quiet refs/heads/main; then
  warn "missing local 'main' branch; cannot determine merged worktrees"
  exit 1
fi

declare -a worktree_paths=()
declare -a worktree_branches=()

declare current_path=""
declare current_branch=""

flush_record() {
  if [[ -n "$current_path" ]]; then
    worktree_paths+=("$current_path")
    worktree_branches+=("$current_branch")
  fi
  current_path=""
  current_branch=""
}

while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ -z "$line" ]]; then
    flush_record
    continue
  fi

  case "$line" in
    worktree\ *)
      current_path="${line#worktree }"
      ;;
    branch\ refs/heads/*)
      current_branch="${line#branch refs/heads/}"
      ;;
  esac
done < <(git worktree list --porcelain)

flush_record

if [[ ${#worktree_paths[@]} -eq 0 ]]; then
  log "No worktrees found."
  exit 0
fi

primary_worktree_path="${worktree_paths[0]}"

merged_branches="$(git branch --format='%(refname:short)' --merged main)"

is_merged_branch() {
  local branch="$1"
  grep -Fxq "$branch" <<<"$merged_branches"
}

declare -a candidate_paths=()
declare -a candidate_branches=()
declare -a candidate_slugs=()

for i in "${!worktree_paths[@]}"; do
  path="${worktree_paths[$i]}"
  branch="${worktree_branches[$i]}"

  if [[ "$path" == "$primary_worktree_path" ]]; then
    continue
  fi

  if [[ -z "$branch" ]]; then
    continue
  fi

  if [[ "$branch" == "main" ]]; then
    continue
  fi

  if is_merged_branch "$branch"; then
    candidate_paths+=("$path")
    candidate_branches+=("$branch")
    candidate_slugs+=("$(derive_slug "$branch")")
  fi
done

if [[ ${#candidate_paths[@]} -eq 0 ]]; then
  log "No merged non-main worktrees to prune."
  exit 0
fi

log "Merged worktrees eligible for pruning:"
for i in "${!candidate_paths[@]}"; do
  slug="${candidate_slugs[$i]}"
  if [[ -z "$slug" ]]; then
    slug="(n/a)"
  fi
  printf '  - worktree: %s\n    branch:   %s\n    slug:     %s\n' \
    "${candidate_paths[$i]}" \
    "${candidate_branches[$i]}" \
    "$slug"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  log ""
  log "Dry run only; no changes made."
  exit 0
fi

if [[ "$ASSUME_YES" -ne 1 ]]; then
  printf '\nProceed with pruning %d merged worktree(s)? [y/N]: ' "${#candidate_paths[@]}"
  read -r response
  case "$response" in
    y|Y|yes|YES)
      ;;
    *)
      log "Aborted. No changes made."
      exit 0
      ;;
  esac
fi

has_meridian=0
if command -v meridian >/dev/null 2>&1; then
  has_meridian=1
else
  warn "meridian CLI not found; skipping optional 'meridian work done <slug>'"
fi

failures=0
for i in "${!candidate_paths[@]}"; do
  path="${candidate_paths[$i]}"
  branch="${candidate_branches[$i]}"
  slug="${candidate_slugs[$i]}"

  log ""
  log "Pruning: $path ($branch)"

  if ! git worktree remove "$path"; then
    warn "failed to remove worktree: $path"
    failures=$((failures + 1))
    continue
  fi

  if ! git branch -d "$branch"; then
    warn "failed to delete branch safely: $branch"
    failures=$((failures + 1))
    continue
  fi

  if [[ "$has_meridian" -eq 1 ]]; then
    if [[ -n "$slug" ]]; then
      if ! meridian work done "$slug" >/dev/null 2>&1; then
        warn "meridian work done failed for slug '$slug' (continuing)"
      fi
    else
      warn "no work-item slug derived from branch '$branch'; skipping meridian work done"
    fi
  fi
done

log ""
if [[ "$failures" -eq 0 ]]; then
  log "Prune complete."
  exit 0
fi

warn "prune completed with $failures failure(s)"
exit 1
