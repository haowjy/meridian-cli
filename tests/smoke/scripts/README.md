# Smoke helper scripts

These helpers are POSIX shell entry points for the smoke tiers.  They do not
belong to pytest collection.

## Commands

```bash
# Disposable fixture; source from a fresh shell/subshell.
. tests/smoke/scripts/setup.sh
. tests/smoke/scripts/setup.sh --git

# Cheap no-paid/no-network executable workflow (works from any cwd).
tests/smoke/scripts/cli.sh

# Explicit complete automated regression (never implicit).
tests/smoke/scripts/extended.sh
# Or focus on the expensive recovery contracts:
tests/smoke/scripts/extended.sh tests/extended/state/

# Opt-in real Pi preflight; always isolated, never a model launch.
. tests/smoke/scripts/pi-setup.sh
```

`setup.sh` clears public and private Meridian context, native harness-store
variables, inherited git overrides, and signing.  It creates disposable
`SCRATCH`, `MERIDIAN_HOME`, `HOME`, XDG, Pi, Claude, Codex, OpenCode, and Mars
paths without changing the caller's cwd. Control and task directories both point
to `SCRATCH`; rebind both when creating fixture subprojects. It exports
`SMOKE_ORIGINAL_HOME` only as a deliberate auth-copy reference, never an implicit
credential source. Use a fresh subshell to contain environment changes, and
always call `smoke_cleanup` after draining all runs; subshell exit alone does
not remove files.

`smoke_add_agent NAME` creates a minimal `.mars/agents/NAME.md` in `SCRATCH`.
`cli.sh` uses a local `test` profile for config CRUD and asserts current CLI output;
it is the maintained executable smoke index.

`pi-setup.sh` composes the shared setup, checks a real `pi` install, points
session roots at disposable paths and reads the package's extension bundles. Managed
prelaunch pins task state to the isolated project's runtime. It uses `pnpm` (matching
`src/meridian/pi_runtime/package.json`) for `--build-extensions`.  To exercise
a live Pi flow, copy only selected credentials into the isolated
`PI_CODING_AGENT_DIR`; do not point it at `SMOKE_ORIGINAL_HOME/.pi/agent`.
No default smoke command launches Pi or any other harness.
