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

## Release caveat

Generated Bun binaries are intentionally **not committed**.  
`src/meridian/pi_runtime/bin/meridian-pi` is OS/arch specific, while the default
`meridian-cli` wheel is universal Python. A future release step can package
platform-specific binaries (or lazy-download them), but the current universal wheel
must not blindly ship a single prebuilt binary.
