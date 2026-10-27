#!/usr/bin/env bash
# Collect the merged PR bodies a release must render, as one JSON array.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/collect-release-notes.sh OUT.json [--trigger-sha SHA]

Write [{"number": N, "body": "..."}] for every merged PR targeting main on the
first-parent commits since the last "release: v" commit, oldest first.

A rebase-merged PR owns several first-parent commits; entries are deduplicated
by PR number, so its notes appear once. The trigger commit's PR is appended when
the walk does not already carry it, which is what a stable promotion after an RC
needs. A commit with no merged PR targeting main is skipped with a notice naming
why. More than 200 commits fails instead of dropping notes: the walk spends one
API call per commit, so the cap bounds the release job's run time. An
intermediate auto-release re-enters the same range, so the escape is a manual
release that carries its notes itself:

  scripts/manually-release.sh patch --no-collect --notes-file PATH

Needs an authenticated `gh` (GH_TOKEN in CI). Both the release workflow and
scripts/manually-release.sh call this, so the walk exists once.
USAGE
}

# Bounds the walk's cost: one `gh` API call per commit in the range.
CAP=200

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

output=""
trigger_sha=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --trigger-sha)
      [[ $# -ge 2 ]] || die "--trigger-sha requires a commit SHA"
      trigger_sha="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      die "unknown argument: $1"
      ;;
    *)
      [[ -z "${output}" ]] || die "unexpected argument: $1"
      output="$1"
      shift
      ;;
  esac
done
[[ -n "${output}" ]] || { usage >&2; exit 1; }

if ! git rev-parse --verify --quiet HEAD >/dev/null; then
  die "cannot read git history; run this script inside a repository with commits"
fi
# Match the subject, not the whole message: a merged PR body can contain a line
# that starts with "release: v", and `git log --grep` would anchor on it.
last_release=""
while IFS=$'\t' read -r commit_hash commit_subject; do
  if [[ "${commit_subject}" == "release: v"* ]]; then
    last_release="${commit_hash}"
    break
  fi
done < <(git log --first-parent --format=$'%H\t%s' HEAD)
if [[ -n "${last_release}" ]]; then
  walk_range="${last_release}..HEAD"
else
  walk_range="HEAD"
fi

# Oldest first, so the renderer's order is the merge order.
walk_commits=()
while IFS=$'\t' read -r commit_hash commit_subject; do
  [[ -n "${commit_hash}" ]] || continue
  if [[ "${commit_subject}" == "release: v"* ]]; then
    printf 'skipped %s: release commit, excluded\n' "${commit_hash}"
    continue
  fi
  walk_commits+=("${commit_hash}")
done < <(git log --first-parent --reverse --format=$'%H\t%s' "${walk_range}")

if (( ${#walk_commits[@]} > CAP )); then
  die "${#walk_commits[@]} commits since the last release (${last_release:-repository start}) exceed the ${CAP}-commit cap; the walk fails rather than drop notes. The cap bounds the walk's one API call per commit. Cut the range with a manual release that carries the notes itself: scripts/manually-release.sh patch --no-collect --notes-file PATH"
fi

# Set `resolved` to the selected PR as {"number": N, "body": "..."} and
# `skip_reason` to why a commit has no PR to render. Return 1 so the caller can
# skip the commit without an API error.
resolved=""
skip_reason=""
resolve_pr() {
  local commit_sha="$1"
  local prs_json candidates candidate_count other_bases

  if ! prs_json="$(gh api -H "Accept: application/vnd.github+json" \
    "repos/{owner}/{repo}/commits/${commit_sha}/pulls" 2>&1)"; then
    die "gh api /commits/${commit_sha}/pulls failed: ${prs_json}"
  fi
  if ! candidates="$(jq --arg commit_sha "${commit_sha}" '
    [ .[] | select(.merged_at != null and .base.ref == "main") ] as $merged
    | ($merged | map(select(.merge_commit_sha == $commit_sha))) as $exact
    | if ($exact | length) > 0 then $exact else $merged end
  ' <<<"${prs_json}")"; then
    die "cannot parse the API reply for commit ${commit_sha}"
  fi

  candidate_count="$(jq 'length' <<<"${candidates}")"
  if [[ "${candidate_count}" -gt 1 ]]; then
    die "commit ${commit_sha} resolves to ${candidate_count} merged PRs targeting main; refusing to guess: $(jq -r 'map(.number | tostring) | join(", ")' <<<"${candidates}")"
  fi
  if [[ "${candidate_count}" -eq 0 ]]; then
    other_bases="$(jq -r '[ .[] | select(.merged_at != null) | .base.ref ] | unique | join(", ")' <<<"${prs_json}")"
    if [[ -n "${other_bases}" ]]; then
      skip_reason="its merged PR targets ${other_bases}, not main"
    else
      skip_reason="no merged PR (direct push)"
    fi
    return 1
  fi
  if ! resolved="$(jq -c '.[0] | {number, body: (.body // "")}' <<<"${candidates}")"; then
    die "cannot read the selected PR for commit ${commit_sha}"
  fi
  return 0
}

work="$(mktemp "${TMPDIR:-/tmp}/collect-release-notes.XXXXXX")"
trap 'rm -f "${work}" "${work}.out"' EXIT

collected=0
seen=""
append_pr() { # pr-json context
  local number
  number="$(jq -r '.number' <<<"$1")"
  case "${seen}" in
    *" ${number} "*) return 0 ;;
  esac
  seen="${seen} ${number} "
  printf '%s\n' "$1" >>"${work}"
  collected=$((collected + 1))
  printf 'collected PR #%s %s\n' "${number}" "$2"
}

for (( index = 0; index < ${#walk_commits[@]}; index++ )); do
  commit_sha="${walk_commits[index]}"
  if resolve_pr "${commit_sha}"; then
    append_pr "${resolved}" "from commit ${commit_sha}"
  else
    printf 'skipped %s: %s\n' "${commit_sha}" "${skip_reason}"
  fi
done

if [[ -n "${trigger_sha}" ]]; then
  if ! git cat-file -e "${trigger_sha}^{commit}" 2>/dev/null; then
    die "--trigger-sha ${trigger_sha} is not a commit in this repository"
  fi
  if resolve_pr "${trigger_sha}"; then
    append_pr "${resolved}" "from trigger commit ${trigger_sha}"
  else
    printf 'trigger commit %s: %s; nothing to append\n' "${trigger_sha}" "${skip_reason}"
  fi
fi

if (( collected == 0 )); then
  die "no merged PRs between ${last_release:-the repository start} and HEAD${trigger_sha:+, nor for trigger ${trigger_sha}}; nothing to render"
fi

if ! jq -s '.' "${work}" >"${work}.out"; then
  die "cannot build the notes JSON"
fi
mv "${work}.out" "${output}"
printf 'collected %s PR(s) into %s\n' "${collected}" "${output}"
