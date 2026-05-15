#!/usr/bin/env bash
# List open quality/immediate GitHub issues for triage.

set -euo pipefail

REPO="haowjy/meridian-cli"
LIMIT="${QUALITY_ISSUES_LIMIT:-200}"
COMMON_SEARCH='-label:future'
ISSUE_TEMPLATE='{{if not .}}  (none){{"\n"}}{{else}}{{range .}}{{printf "  #%-5v %s\n" .number .title}}{{printf "         labels: "}}{{range $i, $l := .labels}}{{if $i}}, {{end}}{{.name}}{{end}}{{printf "\n         %s\n" .url}}{{end}}{{end}}'

usage() {
  cat <<'USAGE'
Usage:
  scripts/quality-issues.sh [--limit N]

Lists open haowjy/meridian-cli issues for the quality/immediate board.
Excludes issues labelled `future` and groups by quality priority.

Environment:
  QUALITY_ISSUES_LIMIT  Max issues per group (default: 200)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)
      [[ $# -ge 2 ]] || { echo "missing value for --limit" >&2; exit 2; }
      LIMIT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

priority_search() {
  local label="$1"
  printf '%s label:"%s"' "$COMMON_SEARCH" "$label"
}

unprioritized_search() {
  printf '%s -label:"quality:high" -label:"quality:medium" -label:"quality:low"' "$COMMON_SEARCH"
}

print_group() {
  local title="$1"
  local search="$2"

  echo
  echo "==> $title"
  gh issue list \
    --repo "$REPO" \
    --state open \
    --limit "$LIMIT" \
    --search "$search" \
    --json number,title,labels,url \
    --template "$ISSUE_TEMPLATE"
}

echo "Quality/immediate issues for $REPO"
echo "Excluding label: future"
echo "Grouped by quality priority"

print_group "High" "$(priority_search 'quality:high')"
print_group "Medium" "$(priority_search 'quality:medium')"
print_group "Low" "$(priority_search 'quality:low')"
print_group "Unprioritized" "$(unprioritized_search)"
