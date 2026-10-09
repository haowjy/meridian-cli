#!/usr/bin/env bash
# Cheap, executable smoke: local CLI only, no harness launch, no network, no
# model charge.  The fixture is deleted on every exit.
set -euo pipefail

_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
_REPO_ROOT=$(cd "$_SCRIPT_DIR/../../.." && pwd)
# shellcheck source=setup.sh
. "$_SCRIPT_DIR/setup.sh" --git
trap smoke_cleanup EXIT

# This script is deliberately offline even when a caller exported a Mars mode.
export MARS_OFFLINE=1
for _key in OPENAI_API_KEY ANTHROPIC_API_KEY OPENROUTER_API_KEY CODEX_API_KEY PI_API_KEY; do
  unset "$_key" || true
done

# Meridian resolves a launch's source/task context from the checkout cwd.  Keep
# that cwd stable while MERIDIAN_PROJECT_DIR points all writes at SCRATCH; this
# is what lets the script be invoked from any caller directory.
cd "$_REPO_ROOT"
_cli() { uv run --project "$_REPO_ROOT" meridian "$@"; }
_python() { uv run --project "$_REPO_ROOT" python "$@"; }
_fail() { echo "FAIL: $*" >&2; exit 1; }
_contains() {
  local _haystack=$1 _needle=$2 _label=${3:-output}
  [[ "$_haystack" == *"$_needle"* ]] || _fail "$_label does not contain $_needle"
}

# A tiny local profile supports the config roundtrip below.
smoke_add_agent test

_help=$(_cli --help)
_contains "$_help" "spawn" "--help"
_contains "$_help" "work" "--help"
_version=$(_cli --version)
[[ "$_version" =~ [0-9]+\.[0-9]+ ]] || _fail "version is not recognizable: $_version"

echo "PASS: help/version"

# The 11 rootless helper/catalog commands are one reachability matrix, not 11
# independent pytest fixtures.  They run from a directory with no Meridian
# project and only inspect the disposable HOME/XDG stores.
_ROOTLESS="$SCRATCH/rootless"
mkdir -p "$_ROOTLESS"
printf '# smoke rootless\n' >"$_ROOTLESS/AGENTS.md"
printf '# smoke\n' >"$_ROOTLESS/readme.md"
_rootless() {
  (cd "$_ROOTLESS" && env -u MERIDIAN_PROJECT_DIR -u MERIDIAN_TASK_DIR \
    MERIDIAN_HOME="$SMOKE_ROOT/rootless-meridian" HOME="$SMOKE_ROOT/rootless-home" \
    uv run --project "$_REPO_ROOT" meridian "$@")
}
_rootless_case() {
  local _label=$1 _expected=$2
  shift 2
  local _output
  if ! _output=$(_rootless "$@" 2>&1); then
    printf '%s\n' "$_output" >&2
    _fail "rootless $_label failed"
  fi
  [[ "$_output" != *"No Meridian project found"* ]] || _fail "rootless $_label found a project"
  if [[ -n "$_expected" ]]; then
    _contains "$_output" "$_expected" "rootless $_label"
  fi
}
_rootless_case qi "" qi
_rootless_case kg-check "" kg check
_rootless_case kg-graph "" kg graph
_rootless_case qi-check "" qi check
_rootless_case qi-list $'agents\tAGENTS.md' qi list
_rootless_case qi-claude-md-fix "[DRY-RUN] would create CLAUDE.md" qi claude-md-fix --dry-run
_rootless_case mermaid-check "" mermaid check
_rootless_case config-show "project_root:" config show
_rootless_case config-get-max-depth "defaults.max_depth:" config get defaults.max_depth
_rootless_case ext-list "meridian.config" ext list
_rootless_case ext-commands "meridian.config.get:" ext commands

echo "PASS: rootless helper matrix (11 commands)"

# Empty-state reads need no installed harness, catalog refresh or credentials.
# Dry-run composition already has default automated coverage with boundary fakes.
_spawn_list=$(_cli spawn list --json)
printf '%s' "$_spawn_list" | _python -c 'import json, sys
payload = json.load(sys.stdin)
assert payload.get("spawns") == [], payload
'
echo "PASS: isolated empty-state JSON"

# Config CRUD is a single local round trip. All writes land in SCRATCH.
_cli config init >/dev/null
_cli config set primary.agent test >/dev/null
_config_get=$(_cli config get primary.agent)
_contains "$_config_get" "primary.agent: test" "config get after set"
_cli config reset primary.agent >/dev/null
_config_reset=$(_cli config get primary.agent)
_contains "$_config_reset" "primary.agent: null" "config get after reset"
echo "PASS: config set/get/reset"

# Explicit project targeting must win over inherited control/task context.
_other_project="$SMOKE_ROOT/project-b"
mkdir -p "$_other_project/.meridian"
_before_scope=$(_cli config get defaults.max_depth)
_cli -C "$_other_project" config set defaults.max_depth 2 >/dev/null
_other_config=$(_cli -C "$_other_project" config get defaults.max_depth)
_original_config=$(_cli config get defaults.max_depth)
_contains "$_other_config" "defaults.max_depth: 2" "explicit -C target"
[[ "$_original_config" == "$_before_scope" ]]
echo "PASS: explicit -C project targeting beats inherited context"

echo "Smoke CLI workflow passed (no harness/model/network calls)."

unset _SCRIPT_DIR _REPO_ROOT _key _help _version _ROOTLESS _output _label _expected \
  _config_get _config_reset _spawn_list _other_project _before_scope _other_config _original_config
