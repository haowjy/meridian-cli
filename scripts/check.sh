#!/usr/bin/env bash
# Pre-push gate script — runs lint, type check, and fast test suite.
# Target: < 2 minutes on typical hardware.
#
# Usage:
#   scripts/check.sh          # Full pre-push check
#   scripts/check.sh --quick  # Skip smoke tests (faster)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$REPO_ROOT"

QUICK=0
for arg in "$@"; do
    case "$arg" in
        --quick) QUICK=1 ;;
    esac
done

echo "==> Linting..."
uv run --extra dev ruff check .

echo "==> Type checking..."
uv run --extra dev python -m pyright

echo "==> Running unit tests..."
uv run --extra dev pytest tests/unit/ -q

echo "==> Running contract tests..."
uv run --extra dev pytest tests/contract/ -q

if [ "$QUICK" -eq 0 ]; then
    echo "==> Running smoke tests..."
    uv run --extra dev pytest tests/smoke/ -q
else
    echo "==> Skipping smoke tests (--quick mode)"
fi

echo ""
echo "✓ All checks passed"
