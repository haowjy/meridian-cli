# Meridian Pi extensions

This folder contains Meridian-managed Pi extension sources for spawned RPC sessions.

Meridian does **not** bundle a Pi runtime. Install/update Pi separately and run it directly.

## Build extensions

```bash
cd src/meridian/pi_runtime
npm run build:extensions
```

This writes stable entrypoints under `dist/extensions/`:

- `dist/extensions/background-tasks/index.js`
- `dist/extensions/meridian-spawn-watch/index.js`

Launch projection copies those extension entrypoints into Meridian-owned state for each spawned run.

## Verify (every implementation pass)

```bash
npm run verify:extensions          # build + vitest + bundle smoke
npm run verify:extensions:loop     # repeat on interval (local)
```

Then delegate **smoke-tester** for runtime verification (`meridian pi`, spawn flows). UX reference: `~/gitrepos/ref/pi-processes`. Work-item map: `pi-generic-background-tasks/pi-processes-parity-map.md` in the meridian-cli work dir.

**Pi extension imports:** only package-root `@earendil-works/pi-tui` / `pi-coding-agent` — subpaths break under Pi's extension loader.
