#!/usr/bin/env bash
# Source once from a fresh shell for an opt-in real-Pi probe. No model is run.
# Auth is never inherited from the native store: deliberately copy auth.json
# afterward, or supply the provider's API-key environment variable.
set -euo pipefail

_PI_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
_PI_RUNTIME_ROOT=$(cd "$_PI_SCRIPT_DIR/../../../src/meridian/pi_runtime" && pwd)
_BUILD_EXTENSIONS=0
for _arg in "$@"; do
  case "$_arg" in
    --build-extensions) _BUILD_EXTENSIONS=1 ;;
    *) echo "pi-setup.sh: unknown option: $_arg" >&2; return 2 2>/dev/null || exit 2 ;;
  esac
done

# A fresh fixture is simpler and safer than trusting an inherited ownership
# marker while other state/config variables may have changed.
. "$_PI_SCRIPT_DIR/setup.sh"
export MERIDIAN_PI_EXTENSION_INSTALL_ROOT="$_PI_RUNTIME_ROOT/dist/extensions"

if ! command -v pi >/dev/null 2>&1 || ! pi --version >/dev/null 2>&1; then
  echo "ERROR: a working real Pi install is required for this opt-in probe." >&2
  return 1 2>/dev/null || exit 1
fi
if command -v node >/dev/null 2>&1; then
  _node_major=$(node -p "process.versions.node.split('.')[0]")
  if [[ "$_node_major" -lt 24 ]]; then
    echo "WARN: Pi extension builds expect Node 24+." >&2
  fi
fi
if [[ "$_BUILD_EXTENSIONS" -eq 1 ]]; then
  (cd "$_PI_RUNTIME_ROOT" && pnpm run build:extensions)
fi
for _bundle in managed-bash meridian-spawn-watch session-boundary; do
  if [[ ! -f "$MERIDIAN_PI_EXTENSION_INSTALL_ROOT/$_bundle/index.js" ]]; then
    echo "WARN: missing $_bundle; prepare dependencies and use --build-extensions." >&2
  fi
done

echo "Pi setup ready (no model launched):"
echo "  PI_CODING_AGENT_DIR=$PI_CODING_AGENT_DIR"
echo "  PI_CODING_AGENT_SESSION_DIR=$PI_CODING_AGENT_SESSION_DIR"
echo "  MERIDIAN_PI_EXTENSION_INSTALL_ROOT=$MERIDIAN_PI_EXTENSION_INSTALL_ROOT"
echo "  Managed task state is pinned by prelaunch to the project's runtime under MERIDIAN_HOME."
echo "  Auth must be supplied deliberately; never copy the entire native agent tree."

unset _PI_SCRIPT_DIR _PI_RUNTIME_ROOT _BUILD_EXTENSIONS _arg _bundle _node_major
