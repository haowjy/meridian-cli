#!/usr/bin/env bash
# Source this file to get an isolated scratch environment.
#
# Usage:
#   . tests/smoke/scripts/setup.sh          # plain temp dir
#   . tests/smoke/scripts/setup.sh --git    # with git init
#
# After sourcing, SCRATCH, MERIDIAN_HOME, and MERIDIAN_PROJECT_DIR are set.
#
# Helper available after sourcing:
#   smoke_add_agent NAME   # create .mars/agents/NAME.md with "# NAME" content

set -euo pipefail

SCRATCH=$(mktemp -d)
export SCRATCH
export MERIDIAN_HOME=$(mktemp -d)
export MERIDIAN_PROJECT_DIR=$SCRATCH

for _arg in "$@"; do
  if [[ "$_arg" == "--git" ]]; then
    git -C "$SCRATCH" init --quiet
    break
  fi
done

# Helper: add a minimal agent profile
# Usage: smoke_add_agent reviewer
smoke_add_agent() {
  local name="$1"
  mkdir -p "$SCRATCH/.mars/agents"
  printf '# %s\n' "$name" > "$SCRATCH/.mars/agents/${name}.md"
}

echo "Smoke env ready: SCRATCH=$SCRATCH"
