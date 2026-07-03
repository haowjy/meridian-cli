#!/usr/bin/env bash
# Pre-push gate script — runs lint, type check, unit tests, and contracts.
# Target: < 2 minutes on typical hardware.
#
# Usage:
#   scripts/check.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$REPO_ROOT"

echo "==> Linting..."
uv run --extra dev ruff check .

echo "==> Type checking..."
uv run --extra dev python -m pyright

echo "==> Running unit tests..."
uv run --extra dev pytest tests/unit/ -q

echo "==> Running contract tests..."
uv run --extra dev pytest tests/contract/ -q

echo "==> Manual smoke guides live in tests/smoke/ (not pytest-collected)."

echo ""
echo "✓ All checks passed"
