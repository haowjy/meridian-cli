# meridian-pi runtime

This folder contains the Node/Bun runtime wrapper used by `meridian-pi`.

## Build

The compiled Bun binary is built locally:

```bash
scripts/build-meridian-pi-runtime.sh
```

or directly:

```bash
cd src/meridian/pi_runtime
bun run build:binary
```

`build:binary` also copies required Pi sidecar assets next to the executable:

- `bin/package.json`
- `bin/theme/`
- `bin/assets/`
- `bin/export-html/`
- `bin/docs/`
- `bin/examples/`
- `bin/photon_rs_bg.wasm`

The Bun-compiled runtime resolves these assets relative to the executable.

## Release caveat

Generated Bun binaries are intentionally **not committed**.  
`src/meridian/pi_runtime/bin/meridian-pi` is OS/arch specific, while the default
`meridian-cli` wheel is universal Python. A future release step can package
platform-specific binaries (or lazy-download them), but the current universal wheel
must not blindly ship a single prebuilt binary.

When a compiled binary is absent, `meridian-pi` only falls back to the Node runner
for source/dev layouts where both `runner.mjs` and
`node_modules/@earendil-works/pi-coding-agent` are present. Installed artifacts
without those runtime deps now fail fast with a clear build/override error.
