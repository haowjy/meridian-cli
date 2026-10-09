# Meridian E2E workflows (opt-in)

These guides exercise runtime seams that require a real subprocess/harness or
operator judgment. They are not pytest-collected or part of the default gate.
Live cases require deliberate cost approval.

## Choose a tier

| Tier | Guides | Requirements |
|---|---|---|
| Local, no model request | `project-resolution.md`, `spawn/routing-provenance.md`, `spawn/skill-injection.md` | Disposable setup; some dry-runs probe installed harnesses/catalogs. Prefer `tests/smoke/scripts/cli.sh` for the maintained cheap path. |
| Local opt-in integration | `spawn/bootstrap.md`, `hooks/git-autosync.md` | Disposable project and (for autosync) a local bare remote. Keep destructive fixtures under the scratch path. |
| Live/credentialed | `fork.md`, `spawn/context-from.md`, `opencode-orphan-cleanup.md` | Explicit human opt-in, eligible cheap model/harness, isolated native stores, and a timeout. These may spend money or terminate a worker. |
| Network/cache | `models-cache-auto-refresh.md` | Disposable fixture only; explicit network approval and prerequisites. Never run in a default gate. |

`tests/smoke/README.md` explains default automatic versus executable smoke,
complete automated regression, local manual, and live/manual tiers. The complete
pytest wrapper is `tests/smoke/scripts/extended.sh`.

## Isolation

From a fresh shell, source the shared helper rather than copying partial setup:

```bash
. tests/smoke/scripts/setup.sh       # add --git only for git scenarios
# work in "$SCRATCH"; the helper leaves the caller's cwd unchanged
smoke_cleanup
```

The helper clears inherited `MERIDIAN_*` and `_MERIDIAN_*` context, isolates
HOME/XDG/Meridian/native harness stores, and disables global/system git config
and signing for `--git`. Both control and task dirs are pinned to `SCRATCH`;
rebind both under `SMOKE_ROOT` when creating fixture subprojects. The caller's
cwd remains unchanged for source-package commands. `SMOKE_ORIGINAL_HOME` is retained only so a human can
copy selected auth into an isolated store deliberately. It is never an implicit
credential source. Do not run an e2e block against a real project or native
store.

## Guide notes

- `fork.md` and `spawn/context-from.md` are the single lineage references; use
  their short happy path before any rejection matrix.
- Foreground/background lifecycle and reports have one [live guide](../smoke/spawn-return-report.md).
- State persistence/recovery is automated in `tests/integration/state/`, with
  expensive cancellation/history checks in `tests/extended/state/`. Obsolete
  v2 manual fixtures were retired rather than preserving a second test suite.
- Cross-transport projection parity is a default automated contract in
  `tests/contract/harness/test_launch_spec_parity.py`; the stale manual duplicate
  and its dead test paths were retired.
- `hooks/git-autosync.md` must use a disposable local bare remote; it must not
  push to a developer or public remote.
- `opencode-orphan-cleanup.md` may kill a worker by design; use a disposable
  process and bounded timeout.
- `models-cache-auto-refresh.md` is network-sensitive and documents explicit
  fixture prerequisites; mtime alone is not proof of a fetch.
