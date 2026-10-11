#!/usr/bin/env bash
# Developer check entry point. Keep it aligned with the routine preflight gate.
#
# Usage:
#   scripts/check.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/preflight.sh" fast
