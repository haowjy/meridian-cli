#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-fast}"

run_step() {
  printf 'preflight: %s\n' "$*" >&2
  "$@"
}

case "$MODE" in
  fast)
    cd "$ROOT_DIR"
    # The Python runner owns one monotonic 60-second budget across all steps.
    run_step uv run --extra dev python -m meridian.dev.preflight
    ;;
  extended|full)
    cd "$ROOT_DIR"
    printf 'preflight: extended gate (explicit tests/ collection)\n' >&2
    # A complete gate must not inherit a last-failed or filtered selection.
    unset PYTEST_ADDOPTS PYTESTS_LAST_FAILED
    run_step uv run --extra dev ruff check .
    run_step uv run --extra dev python -m pyright
    (
      cd "$ROOT_DIR/src/meridian/pi_runtime"
      # Git hooks have no TTY; allow dependency-tree recreation without prompting.
      run_step pnpm install --frozen-lockfile --config.confirmModulesPurge=false
      run_step pnpm run build:extensions
    )
    # Explicit tests/ bypasses the fast testpaths allowlist and retains the
    # complete automated regression suite for release/manual/nightly runs.
    run_step uv run --extra dev pytest tests/
    run_step uv build --no-sources
    ;;
  *)
    printf 'Usage: preflight.sh [fast|extended|full]\n' >&2
    exit 1
    ;;
esac
