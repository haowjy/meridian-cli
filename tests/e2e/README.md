# Meridian E2E Tests (Manual)

Manual end-to-end tests for flows that require a working harness, network access,
or human judgment. **Most CLI behavior is now covered by automated smoke tests
in `tests/smoke/`.**

## When to Use Manual E2E Tests

Use these guides when the test:
- Requires a working harness (Claude, Codex, OpenCode) running live
- Involves network/cache freshness behavior
- Is intentionally open-ended or exploratory
- Tests harness-specific transport or lineage behavior

## Remaining Manual Guides

| Guide | What it tests |
|-------|---------------|
| `adversarial.md` | Intentionally open-ended adversarial exploration |
| `fork.md` | Real harness lineage with --fork (partially automatable via dry-run) |
| `models-cache-auto-refresh.md` | Network/cache freshness behavior |
| `project-resolution.md` | `-C`/`MERIDIAN_PROJECT_DIR` project selection with stale runtime env |
| `state-integrity.md` | Reconciliation after manual state corruption |
| `streaming-adapter-parity.md` | Cross-harness streaming behavior |
| `spawn/lifecycle.md` | Background spawn lifecycle with working harness |
| `spawn/context-from.md` | Live --from with real sessions |
| `spawn/skill-injection.md` | Harness-specific skill transport |
| `hooks/git-autosync.md` | Real git push/rebase integration |

## Migrated to Automated Smoke Tests

The following guides have been migrated to `tests/smoke/` and deleted from here:

- `quick-sanity.md` → `test_sanity.py`
- `agent-mode.md` → `test_agent_mode.py`
- `config/init-show-set.md` → `test_config.py`
- `output-formats.md` → `test_output_formats.py`
- `spawn/dry-run.md` → `test_spawn_dry_run.py`
- `spawn/error-paths.md` → `test_spawn_errors.py`
- `workspace/init-inspection.md` → `test_workspace.py`
- `work-items.md` → `test_work_items.py`
- `hooks/cli.md` → `test_hooks.py`

## How to Run

1. Pick one file from the remaining guides.
2. Run each bash block exactly as written.
3. Treat any `FAIL` line, traceback, or hang as a test failure.

## Setup

For scratch repo setup, see the individual guide preambles. Most guides expect:

```bash
export REPO_ROOT=/abs/path/to/meridian-cli
export SMOKE_REPO="$(mktemp -d)"
git -C "$SMOKE_REPO" init --quiet
for var in $(env | awk -F= '/^MERIDIAN_/ {print $1}'); do unset "$var"; done
export MERIDIAN_PROJECT_DIR="$SMOKE_REPO"
cd "$REPO_ROOT"
export RUNTIME_ROOT="$(uv run python - <<'PY'
import os
from pathlib import Path
from meridian.lib.state.paths import resolve_project_paths
from meridian.lib.state.user_paths import get_or_create_project_id, get_project_home

state_dir = resolve_project_paths(Path(os.environ["SMOKE_REPO"])).root_dir
print(get_project_home(get_or_create_project_id(state_dir)))
PY
)"
```
