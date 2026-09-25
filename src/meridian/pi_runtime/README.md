# Meridian Pi extensions

This folder contains Meridian-managed Pi extension sources for spawned RPC and primary native TUI sessions.

Meridian does **not** bundle a Pi runtime. Install/update Pi separately and run it directly.

## Build extensions

```bash
cd src/meridian/pi_runtime
pnpm install --frozen-lockfile
pnpm run build:extensions
```

This writes a stable entrypoint under `dist/extensions/`:

- `dist/extensions/session-boundary/index.js` — bounded native entry/exit observations
- `dist/extensions/managed-bash/index.js` — managed shell tasks, `/ps`
- `dist/extensions/meridian-spawn-watch/index.js` — correlated spawn discovery, `/spawn`, `/spawn:wait`

Launch projection resolves these stable bundle paths for each managed launch.

## Verify (every implementation pass)

```bash
npm run verify:extensions          # build + vitest + bundle smoke
npm run verify:extensions:loop     # repeat on interval (local)
```

The boundary bundle smoke runs the built extension in an isolated Node process,
using Pi's lifecycle registration API. Python's
`tests/integration/launch/test_pi_run_boundary.py` consumes those actual records;
run `verify:extensions` before that test (or the full Python suite).

Then delegate **smoke-tester** for runtime verification (`meridian pi`, spawn flows). UX reference: `~/gitrepos/ref/pi-processes`. Work-item map: `pi-generic-background-tasks/pi-processes-parity-map.md` in the meridian-cli work dir.

**Pi extension imports:** only package-root `@earendil-works/pi-tui` / `pi-coding-agent` — subpaths break under Pi's extension loader.

## Native boundary contract

The session-boundary extension publishes v2 records correlated by launch nonce
and Pi PID. Only a final owned shutdown/quit with a readable native identity
qualifies an exit. Switch, restart, missing/corrupt records, and stale-context
shutdowns remain unresolved; they never substitute the last-seen identity.

Pi 0.87.1's invalidated context is recognized by the error-text prefix
`This extension ctx is stale after session replacement or reload.`
Only shutdown catches that specific condition and clears quit without poisoning.
If Pi rewords it (or another identity read fails), the extension poisons the
record and rethrows: exit resolution fails closed. Requalify this dependency
when upgrading Pi; do not broaden the catch to guess an identity.

## Spawn rows and wait

- **Meridian spawns in `/spawn`** — ids confirmed via `meridian spawn list` / `spawn show` (same store as `meridian spawn wait`), not shell-command regex.
- **Blocking wait** — `meridian spawn wait` in the terminal, or **`/spawn:wait <p-id>`** in Pi (30m subprocess cap; CLI may checkpoint earlier).
- **`/ps`** — observability only (`ps:kill`, `ps:logs`); no wait subcommand. `/spawns*` removed.
- **Task pings** — tracked background bash sends one follow-up ping after `_MERIDIAN_PI_TASK_PING_INTERVAL_MS` (project config/CLI resolve to this env var). Pings are one-shot per task and reset on log activity unless `_MERIDIAN_PI_TASK_PING_RESET_ON_ACTIVITY=false`.
