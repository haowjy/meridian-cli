#!/usr/bin/env bash
# Source this file from a fresh shell to prepare an isolated smoke fixture.
#
# Usage:
#   . tests/smoke/scripts/setup.sh          # plain disposable fixture
#   . tests/smoke/scripts/setup.sh --git    # fixture with a safe local git config
#
# The caller's cwd is deliberately unchanged: existing guides can continue to
# use `uv run ...` from the checkout.  The shell that sourced this file owns
# SMOKE_ROOT and should call `smoke_cleanup` when it is done.  For a disposable
# one-shot run, source it in a subshell: `( . tests/.../setup.sh; ... )`.
#
# Paths are disposable and never point at the caller's Meridian, Pi, or git
# stores.  SMOKE_ORIGINAL_HOME is retained (but not printed) so a human who
# deliberately opts into a live probe can copy credentials explicitly.

set -euo pipefail
umask 077

_smoke_tmp_base=${SMOKE_TMPDIR:-${TMPDIR:-/tmp}}
SMOKE_ROOT=$(mktemp -d "${_smoke_tmp_base%/}/meridian-smoke.XXXXXX")
printf 'owned by smoke setup\n' >"$SMOKE_ROOT/.owned"
export SMOKE_ROOT

SMOKE_ORIGINAL_HOME=${SMOKE_ORIGINAL_HOME:-${HOME:-}}
SMOKE_ORIGINAL_CWD=$(pwd -P)
export SMOKE_ORIGINAL_HOME SMOKE_ORIGINAL_CWD

# Do not let a Meridian spawn/work context, including private plumbing, leak
# into a supposedly fresh smoke.  This intentionally removes dynamic prefixes
# such as MERIDIAN_CONTEXT_* and MERIDIAN_SECRET_* as well.
while IFS='=' read -r _smoke_key _smoke_value; do
  case "$_smoke_key" in
    MERIDIAN_*|_MERIDIAN_*|GIT_*|MARS_*) unset "$_smoke_key" ;;
  esac
done < <(env)

# Native harness stores and config overrides are just as dangerous as Meridian
# state.  Clear inherited values before assigning disposable locations below.
for _smoke_key in \
  CLAUDE_CONFIG_DIR CODEX_HOME OPENCODE_DB OPENCODE_CONFIG OPENCODE_CONFIG_DIR OPENCODE_CONFIG_CONTENT \
  PI_CODING_AGENT_DIR PI_CODING_AGENT_SESSION_DIR PI_CONFIG_DIR \
  PI_CODING_AGENT AI_AGENT CODEX_SESSION_ID CODEX_THREAD_ID \
  MARS_HOME MARS_CONFIG MARS_CACHE_DIR; do
  unset "$_smoke_key" || true
done

SCRATCH="$SMOKE_ROOT/project"
export SCRATCH
mkdir -p "$SCRATCH" "$SMOKE_ROOT/home" "$SMOKE_ROOT/xdg/config" \
  "$SMOKE_ROOT/xdg/data" "$SMOKE_ROOT/xdg/state" "$SMOKE_ROOT/xdg/cache"

# Keep all user-facing and native stores under the owned fixture.
export HOME="$SMOKE_ROOT/home"
export XDG_CONFIG_HOME="$SMOKE_ROOT/xdg/config"
export XDG_DATA_HOME="$SMOKE_ROOT/xdg/data"
export XDG_STATE_HOME="$SMOKE_ROOT/xdg/state"
export XDG_CACHE_HOME="$SMOKE_ROOT/xdg/cache"
export MERIDIAN_HOME="$SMOKE_ROOT/meridian"
export MERIDIAN_PROJECT_DIR="$SCRATCH"
export MERIDIAN_TASK_DIR="$SCRATCH"
export PI_CODING_AGENT_DIR="$SMOKE_ROOT/pi-agent"
export PI_CODING_AGENT_SESSION_DIR="$SMOKE_ROOT/pi-sessions"
export CLAUDE_CONFIG_DIR="$SMOKE_ROOT/claude"
export CODEX_HOME="$SMOKE_ROOT/codex"
# Let OpenCode derive its DB/config files from the isolated XDG roots.
export OPENCODE_CONFIG_DIR="$SMOKE_ROOT/opencode"
export MARS_HOME="$SMOKE_ROOT/mars"
mkdir -p "$MERIDIAN_HOME" "$PI_CODING_AGENT_DIR" "$PI_CODING_AGENT_SESSION_DIR" \
  "$CLAUDE_CONFIG_DIR" "$CODEX_HOME" "$OPENCODE_CONFIG_DIR" "$MARS_HOME"

# A fixture must not read a developer's global/system git config or invoke a
# signing helper.  The local config still makes commits deterministic for
# guides that exercise git hooks/autosync.
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
: >"$GIT_CONFIG_GLOBAL"

for _arg in "$@"; do
  if [[ "$_arg" == "--git" ]]; then
    git -C "$SCRATCH" init --quiet
    git -C "$SCRATCH" config --local user.name "Meridian Smoke"
    git -C "$SCRATCH" config --local user.email "smoke@example.invalid"
    git -C "$SCRATCH" config --local commit.gpgsign false
    git -C "$SCRATCH" config --local tag.gpgsign false
    break
  fi
done

# Helper: add a minimal local agent profile for dry-run spawn checks.
# Usage: smoke_add_agent reviewer
smoke_add_agent() {
  local _smoke_name=${1:?agent name required}
  mkdir -p "$SCRATCH/.mars/agents"
  printf '# %s\n' "$_smoke_name" >"$SCRATCH/.mars/agents/${_smoke_name}.md"
}

smoke_cleanup() {
  if [[ -n "${SMOKE_ROOT:-}" && -f "$SMOKE_ROOT/.owned" ]]; then
    rm -rf -- "$SMOKE_ROOT"
  fi
}

echo "Smoke env ready (disposable):"
echo "  SCRATCH=$SCRATCH"
echo "  MERIDIAN_HOME=$MERIDIAN_HOME"
echo "  HOME=$HOME"
echo "  XDG_CONFIG_HOME=$XDG_CONFIG_HOME"
echo "  native stores under $SMOKE_ROOT"
echo "  call smoke_cleanup when finished (original cwd preserved: $SMOKE_ORIGINAL_CWD)"

unset _smoke_tmp_base _smoke_key _smoke_value _arg
