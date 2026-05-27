# Meridian Pi extensions

This folder contains Meridian-managed Pi extension sources for spawned RPC sessions.

Meridian does **not** bundle a Pi runtime. Install/update Pi separately and run it directly.

## Build extensions

```bash
cd src/meridian/pi_runtime
npm run build:extensions
```

This writes a stable entrypoint under `dist/extensions/`:

- `dist/extensions/managed-bash/index.js` — managed shell tasks, `/ps`
- `dist/extensions/meridian-spawn-watch/index.js` — correlated spawn discovery, `/spawn`, `/spawn:wait`

Launch projection copies that extension entrypoint into Meridian-owned state for each spawned run.

## Verify (every implementation pass)

```bash
npm run verify:extensions          # build + vitest + bundle smoke
npm run verify:extensions:loop     # repeat on interval (local)
```

Then delegate **smoke-tester** for runtime verification (`meridian pi`, spawn flows). UX reference: `~/gitrepos/ref/pi-processes`. Work-item map: `pi-generic-background-tasks/pi-processes-parity-map.md` in the meridian-cli work dir.

**Pi extension imports:** only package-root `@earendil-works/pi-tui` / `pi-coding-agent` — subpaths break under Pi's extension loader.

## Spawn rows and wait

- **Meridian spawns in `/spawn`** — ids confirmed via `meridian spawn list` / `spawn show` (same store as `meridian spawn wait`), not shell-command regex.
- **Blocking wait** — `meridian spawn wait` in the terminal, or **`/spawn:wait <p-id>`** in Pi (30m subprocess cap; CLI may checkpoint earlier).
- **`/ps`** — observability only (`ps:kill`, `ps:logs`); no wait subcommand. `/spawns*` removed.
