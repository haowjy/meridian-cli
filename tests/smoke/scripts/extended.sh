#!/usr/bin/env bash
# Explicit opt-in regression suite; normally takes several minutes.
set -euo pipefail

_REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
cd "$_REPO_ROOT"
# Do not silently inherit filtered/last-failed selection from a developer shell.
unset PYTEST_ADDOPTS PYTESTS_LAST_FAILED
printf 'extended regression (default tests/; explicit paths win): %s\n' "$*" >&2
# Let pytest parse paths/options; do not accidentally broaden a flags-first
# focused invocation or fall back to the fast testpaths on a flags-only run.
exec uv run --extra dev pytest-llm -o testpaths=tests "$@"
