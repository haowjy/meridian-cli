# Smoke workflows

This directory contains CLI-visible checks that are intentionally separate from
pytest collection.  The default safety boundary is **disposable state, no
network, no harness process, and no paid model**.

## Tiers

| Tier | How to run | Cost and purpose |
|---|---|---|
| Default gate | `uv run pytest-llm` (see `tests/AGENTS.md`) | Fast automated contracts; no smoke/e2e markdown is collected. |
| Cheap executable smoke | `tests/smoke/scripts/cli.sh` | One local workflow: help/version, the 11 rootless helper commands, config CRUD, explicit `-C` project targeting, and empty-state JSON. Uses isolated HOME/XDG/Meridian/native stores; no harness/network/model call. |
| Complete automated regression | `tests/smoke/scripts/extended.sh` | Explicit `tests/` path includes `tests/extended/` and slower integrity/concurrency/workflow contracts. Pass a focused path to narrow it. |
| Local opt-in manual | `sanity.md`, `config.md`, `spawn-dry-run.md`, `workspace.md`, `work-items.md`, `plain-directory-roots.md`, and the local e2e guides | Useful exploratory or seam checks. Source `scripts/setup.sh` in a fresh shell and clean up afterward. |
| Live/manual (credentials or real subprocess) | `pi-manual.md`, `pi-rpc-quiescence.md`, `spawn-cursor-subprocess.md`, `spawn-continue-fork.md` | Never run by default. Requires explicit disposable state, an eligible cheap model, and deliberate auth/network approval. |

The executable smoke is the maintained happy-path index. Markdown guides retain
checks that are difficult to reproduce automatically (Pi RPC quiescence, native
session boundaries, subprocess protocol failures, and human-visible reports).
Expected-output checklists are kept when they protect a real observable; they
are not a substitute for the executable smoke.

## Safety contract

- Start in a fresh shell/subshell with `. tests/smoke/scripts/setup.sh`; it clears
  inherited `MERIDIAN_*`/`_MERIDIAN_*` context and native-store overrides,
  isolates `HOME`, XDG stores, Pi, Claude, Codex, OpenCode, and Meridian state,
  and leaves the caller's cwd unchanged.
- The setup prints only disposable paths. `SMOKE_ORIGINAL_HOME` is retained for
  humans who deliberately copy selected auth into the isolated store; it is
  never used implicitly. Call `smoke_cleanup` before leaving the shell.
- Use `--git` only for a disposable fixture. It disables system/global config,
  signing, and inherited git config overrides while setting a local identity.
- No command in the default tiers launches a native harness. Do not add a real
  model, remote, push, cache refresh, or credential path to an automatic check.

See [`scripts/README.md`](scripts/README.md) for setup details and the opt-in
Pi preflight.  The complete manual inventory is in [`../e2e/README.md`](../e2e/README.md).
