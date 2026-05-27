# Smoke test helper scripts

Setup-only shell helpers for manual smoke guides in `tests/smoke/*.md`. Source them
before working through a guide; they do **not** run or assert test scenarios.

## Quick start

```bash
# Plain isolated Meridian scratch env
. tests/smoke/scripts/setup.sh

# With a git repo in SCRATCH
. tests/smoke/scripts/setup.sh --git

# Pi harness: PATH/node check + optional extension build
. tests/smoke/scripts/pi-setup.sh --build-extensions
```

Pi-specific manual gate: `tests/smoke/pi-manual.md`. Deep RPC scenarios:
`tests/smoke/pi-rpc-quiescence.md`.

---

## setup.sh

Sets three environment variables, then stays out of the way:

| Variable | Value |
|---|---|
| `SCRATCH` | fresh `mktemp -d` directory |
| `MERIDIAN_HOME` | fresh `mktemp -d` directory |
| `MERIDIAN_PROJECT_DIR` | same as `SCRATCH` |

**Options**

- `--git` — runs `git init --quiet` in `SCRATCH` before returning. Required
  by guides that test git-aware features (workspace, hooks, config set/reset).

**Helper function**

```bash
smoke_add_agent NAME
```

Creates `$SCRATCH/.mars/agents/NAME.md` containing `# NAME`. Use wherever
a guide's setup block creates a minimal agent profile.

```bash
. tests/smoke/scripts/setup.sh
smoke_add_agent reviewer
smoke_add_agent test
```

---

## pi-setup.sh

Prepares a **real** Pi install for manual smoke (no fake binaries, no test runners).

| What it does | Notes |
|---|---|
| Verifies `pi` on `PATH` | Fails fast if missing or `pi --version` errors |
| Warns if Node is below 24 | Extension builds expect Node 24+ |
| Sets `PI_CODING_AGENT_DIR` | Defaults to `~/.pi/agent` if unset |
| `--build-extensions` | Runs `npm run build:extensions` in `src/meridian/pi_runtime` |
| `--isolated-state` | Sets `MERIDIAN_PI_STATE_DIR` to a temp dir (extension task state only) |

Does not invoke `meridian spawn`, assert outcomes, or patch `pi`.
