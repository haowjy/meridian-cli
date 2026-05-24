#!/usr/bin/env bash
# Pi smoke setup only — env, extension build, PATH checks. Does not run tests.
#
# Usage:
#   . tests/smoke/scripts/pi-setup.sh
#   . tests/smoke/scripts/pi-setup.sh --build-extensions
#   . tests/smoke/scripts/pi-setup.sh --isolated-state
#
# After sourcing:
#   - pi must be on PATH (real install; no fake binaries)
#   - node should be v24+ when building extensions

set -euo pipefail

_PI_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
_BUILD_EXTENSIONS=0
_ISOLATED_STATE=0

for _arg in "$@"; do
  case "$_arg" in
    --build-extensions) _BUILD_EXTENSIONS=1 ;;
    --isolated-state) _ISOLATED_STATE=1 ;;
    *)
      echo "pi-setup.sh: unknown option: $_arg" >&2
      return 2 2>/dev/null || exit 2
      ;;
  esac
done

# Prefer Node 24+ on PATH (match CI / extension toolchain).
if command -v node >/dev/null 2>&1; then
  _node_major="$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)"
  if [[ "${_node_major:-0}" -lt 24 ]]; then
    echo "WARN: node is v$(node -v); Pi extension builds expect Node 24+." >&2
  fi
else
  echo "WARN: node not on PATH; skip --build-extensions or install Node 24+." >&2
fi

if ! command -v pi >/dev/null 2>&1; then
  echo "ERROR: real 'pi' not found on PATH. Install Pi, run 'pi update', then retry." >&2
  return 1 2>/dev/null || exit 1
fi

if ! pi --version >/dev/null 2>&1; then
  echo "ERROR: 'pi --version' failed; fix the install before smoke." >&2
  return 1 2>/dev/null || exit 1
fi

# Meridian sets PI_CODING_AGENT_DIR for subprocess Pi (default ~/.pi/agent).
export PI_CODING_AGENT_DIR="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"

if [[ "$_ISOLATED_STATE" -eq 1 ]]; then
  _pi_state="$(mktemp -d)"
  export MERIDIAN_PI_STATE_DIR="$_pi_state"
  echo "Pi setup: MERIDIAN_PI_STATE_DIR=$_pi_state (isolated extension task state)"
fi

if [[ "$_BUILD_EXTENSIONS" -eq 1 ]]; then
  echo "Pi setup: building extensions in $_PI_REPO_ROOT/src/meridian/pi_runtime ..."
  (cd "$_PI_REPO_ROOT/src/meridian/pi_runtime" && npm run build:extensions)
fi

echo "Pi setup ready:"
echo "  pi=$(command -v pi) ($(pi --version 2>/dev/null | head -1 || echo unknown))"
echo "  PI_CODING_AGENT_DIR=$PI_CODING_AGENT_DIR"
echo "  spawn sessions -> \${MERIDIAN_HOME:-~/.meridian}/meridian-pi/sessions/<spawn-id>/"
echo "  extensions -> $PI_CODING_AGENT_DIR/extensions/meridian/<launch-id>/"
