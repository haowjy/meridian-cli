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
