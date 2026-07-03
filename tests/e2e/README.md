# Meridian E2E Tests (Manual)

Manual end-to-end tests for flows that require a working harness, network access,
or human judgment. Quick manual CLI checklists live in `tests/smoke/`; neither
`tests/e2e/` nor `tests/smoke/` is a pytest-collected suite.

## When to Use Manual E2E Tests

Use these guides when the test:

- Requires a working harness (Claude, Codex, OpenCode) running live
- Involves network/cache freshness behavior
- Is intentionally open-ended or exploratory
- Tests harness-specific transport or lineage behavior

## Manual Guides

| Guide | What it tests |
|-------|---------------|
| `adversarial.md` | Intentionally open-ended adversarial exploration |
| `fork.md` | Real harness lineage with `--fork` |
| `models-cache-auto-refresh.md` | Network/cache freshness behavior |
| `opencode-orphan-cleanup.md` | Real OpenCode backend orphan cleanup after hard worker crash |
| `project-resolution.md` | `-C`/`MERIDIAN_PROJECT_DIR` project selection with stale runtime env |
| `state-integrity.md` | Reconciliation after manual state corruption |
| `streaming-adapter-parity.md` | Cross-harness streaming behavior |
| `spawn/bootstrap.md` | Spawn bootstrap against a live harness |
| `spawn/context-from.md` | Live `--from` with real sessions |
| `spawn/lifecycle.md` | Background spawn lifecycle with working harness |
| `spawn/routing-provenance.md` | Dry-run routing provenance and resolved harness/model display |
| `spawn/skill-injection.md` | Harness-specific skill transport |
| `hooks/git-autosync.md` | Real git push/rebase integration |

## Moved to Quick Smoke Guides

These flows are now markdown guides under `tests/smoke/`:

- `agent-mode.md`
- `config.md`
- `hooks.md`
- `output-formats.md`
- `sanity.md`
- `spawn-dry-run.md`
- `spawn-errors.md`
- `workspace.md`
- `work-items.md`

## How to Run

1. Pick one guide.
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
export RUNTIME_ROOT="$(uv run python tests/e2e/resolve-runtime-root.py)"
```
