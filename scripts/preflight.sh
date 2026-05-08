#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-full}"

run_step() {
  printf 'preflight: %s\n' "$*" >&2
  "$@"
}

case "$MODE" in
  fast)
    cd "$ROOT_DIR"
    run_step uv run ruff check .
    ;;
  full)
    cd "$ROOT_DIR"
    run_step uv run ruff check .
    run_step uv run pyright
    run_step uv run architecture-check
    run_step uv run pytest -x -q
    ;;
  *)
    printf 'Usage: preflight.sh [fast|full]\n' >&2
    exit 1
    ;;
esac
