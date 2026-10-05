# Meridian Pi extensions

Meridian-owned TypeScript extensions run inside the separately installed Pi
runtime, in both spawned RPC and primary native TUI sessions. Meridian does not
bundle or control Pi itself.

| Bundle | Responsibility |
|---|---|
| `managed-bash` | `bash` / `bash_manage`, owned shell tasks, logs, `/ps*` |
| `meridian-spawn-watch` | Canonical child-spawn observation, result notifications, `/spawn*` |
| `session-boundary` | Bounded launch-correlated native entry/exit observations |

Both launch roles load managed-bash and spawn-watch when enabled in `[harness.pi]`.
Session-boundary is always loaded. Primary retains native ambient discovery;
spawned RPC suppresses ambient extensions unless `load_all_pi_extensions = true`.
See the [integration contract](../lib/harness/.context/pi-integration.md).

## Build and verify

From the repository root:

```bash
cd src/meridian/pi_runtime
pnpm install --frozen-lockfile
pnpm run verify:extensions          # build + Vitest, including bundle smoke
pnpm run verify:extensions:loop     # repeat locally
```

`pnpm run build:extensions` builds without running tests. It writes stable bundles
under `dist/extensions/<name>/index.js` for all three extensions. Change source,
never generated output. Projection prefers the package's built bundles, then the
stable install root `~/.meridian/pi/extensions/`; missing artifacts fail launch.

Import Pi packages only from their package roots (`@earendil-works/pi-tui` and
`@earendil-works/pi-coding-agent`), not loader-sensitive subpaths.

The boundary bundle smoke uses an isolated Node process with Pi's native lifecycle
registration API. Build before running
`tests/integration/launch/test_pi_run_boundary.py`, which consumes those records.
For real-runtime verification use the [manual gate](../../../tests/smoke/pi-manual.md)
and relevant [quiescence scenarios](../../../tests/smoke/pi-rpc-quiescence.md).
Use a cheap model and distinguish live smoke from automated fixture coverage.

## Task and spawn controls

- **`/ps`** lists managed Bash tasks; `/ps:logs`, `/ps:kill`, `/ps:clear`, and
  `/ps:b` (alias `/ps:background`) inspect/control them. Waiting is available
  through `bash_manage(action='wait', bash_id='b-…')`, not a `/ps` wait command.
- **`/spawn`** lists canonical direct child spawns; `/spawn:show`, `/spawn:log`,
  `/spawn:cancel`, and `/spawn:clear` inspect/control them.
- **`/spawn:wait <p-id>`** waits through the same store as `meridian spawn wait`
  (30-minute subprocess cap; CLI checkpoints can return earlier).
- Tracked background Bash sends an advisory one-shot ping after
  `_MERIDIAN_PI_TASK_PING_INTERVAL_MS`. Log activity rearms it unless
  `_MERIDIAN_PI_TASK_PING_RESET_ON_ACTIVITY=false`.

`/reload` preserves live shell ownership and queued result-delivery claims.
A cold process restart preserves records but cannot recover process handles:
formerly running Bash rows show `ownership_lost`. Inspect those processes manually
or explicitly detach the row to release tracking. Persisted PIDs never authorize
signalling after restart.

Detach releases quiescence tracking, not process ownership. A live Pi owner still
terminates detached groups on normal shutdown. Kill/abort wait for owned work and
queued output to finish, escalating TERM-resistant groups to SIGKILL.

Unattended tracked background results remain owed until explicit consumption or
exact native custom-message admission. Follow-ups wait for native idle and recheck
consumption before publication; queueing is not proof of delivery. See the
[delivery contract](.context/delivery-contract.md) for restart/crash limits and the
[extension architecture](.context/CONTEXT.md) for native boundary validation and its
stale-context error-text dependency.
