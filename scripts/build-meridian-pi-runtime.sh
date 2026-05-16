#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/src/meridian/pi_runtime"

if ! command -v bun >/dev/null 2>&1; then
  echo "bun is required to build the meridian-pi runtime binary." >&2
  echo "Install Bun, then rerun this script." >&2
  exit 1
fi

cd "$RUNTIME_DIR"
bun run build:binary
