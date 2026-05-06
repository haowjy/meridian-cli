#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -d "$ROOT_DIR/.githooks" ]]; then
  printf 'ERROR: .githooks/ directory not found.\n' >&2
  exit 1
fi
git -C "$ROOT_DIR" config --local core.hooksPath .githooks
printf 'Git hooks activated: core.hooksPath = .githooks\n'
printf 'Active hook: pre-push (full preflight + tag policy)\n'
printf 'Optional hook: pre-commit (fast ruff check) lives at .githooks/optional/pre-commit and is not active by default\n'
