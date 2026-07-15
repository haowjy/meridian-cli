# pi_runtime/ — Context

## Architecture

Meridian-owned TypeScript extensions that run inside the Pi harness process. Pi is the
first harness with an in-process extension architecture — other harnesses are opaque
subprocesses. Extensions give Meridian a seam for observability and coordination that
Pi's native CLI does not expose.

The coordination boundary is **disk state the extensions write and Python observes**.
Pi stdout remains the JSON-RPC transport; background-work and child-spawn authority does
not travel over stdout and does not use a separate JSONL event tailer.

### Directory Layout

```
pi_runtime/
├── package.json              # extension build/test scripts, "meridian-pi-extensions"
├── pnpm-workspace.yaml       # declares packages=[], allows esbuild builds
├── pnpm-lock.yaml            # exact dependency tree
├── dist/                     # build output: splatted entrypoints
│   └── extensions/
│       ├── managed-bash/index.js
│       └── meridian-spawn-watch/index.js
└── extensions/
    ├── types.ts              # shared TS types (ExtensionAPI, ToolRegistration)
    ├── shared/               # ids, json files, panels, pi state paths, meridian CLI helpers
    ├── managed-bash/
    │   └── src/index.ts      # bash/bash_manage override, b-* records, /ps* UI
    └── meridian-spawn-watch/
        └── src/index.ts      # spawn disk watcher, implicit-wait notifications, /spawn* UI
```

### Extension Responsibilities

| Extension | Owns | Writes / observes |
|---|---|---|
| `managed-bash` | `bash` / `bash_manage`, tracked vs detached bash records, `/ps*` slash commands, `MERIDIAN_PI_BASH_ID` injection into child processes | `runtime_root/pi-bash/<spawn-id>/bash-records.json` and bash logs |
| `meridian-spawn-watch` | correlated spawn discovery, `/spawn*` slash commands, implicit-wait `sendMessage({triggerTurn: true})` notifications | watches `runtime_root/spawns/<child>/state.json`, reads `originating_bash_id`, writes `runtime_root/pi-bash/<spawn-id>/last-notification.json` |

`managed-bash` is the mechanism extension. `meridian-spawn-watch` is the policy extension.
Keep that split: shell task execution and task record persistence belong in managed-bash;
child-spawn observation and notification behavior belong in spawn-watch.

### Build Pipeline

`npm run build:extensions` runs three scripts in sequence:

1. `build:extensions:clean` — removes `./dist/extensions`
2. `build:extensions:managed-bash` — `tsup` bundles `managed-bash/src/index.ts` → ESM, Node 20, single-file output
3. `build:extensions:meridian-spawn-watch` — bundles `meridian-spawn-watch/src/index.ts` the same way

`npm run verify:extensions` rebuilds and runs Vitest coverage for the extension sources.

Output goes to `dist/extensions/`. Python launch projection resolves entrypoints with
`pi_extension_projection.py`, preferring the repo build output during local development
and falling back to the installed bundle root from `pi_paths.resolve_meridian_pi_extension_root()`.
A missing bundle raises `PiExtensionProjectionError` with the build command.

### Extension Loading

Pi loads extensions via explicit `-e <path>` CLI flags. Meridian launches with
`--no-extensions` and then adds only the selected Meridian bundles, so ambient user
extensions do not change spawn behavior.

- **spawned RPC mode**: `managed-bash` + `meridian-spawn-watch`
- **primary native TUI mode**: `meridian-spawn-watch` only; no bash override and no spawned-session auto-stop

Role-specific behavior is gated by environment, including `MERIDIAN_PI_SESSION_ROLE` and
`MERIDIAN_PI_STATE_DIR`.

## Contracts

### Disk-State Coordination

The Python streaming layer treats these files as quiescence inputs:

- `runtime_root/spawns/<child>/state.json` — child spawn status and `parent_id`
- `runtime_root/pi-bash/<parent>/bash-records.json` — tracked/detached bash records
- `runtime_root/pi-bash/<parent>/last-notification.json` — last implicit-wait notification marker

Writes must use the shared JSON-file helpers so readers never observe half-written JSON.
Readers tolerate truncation/missing files and re-check disk before final quiescence.

The current Python watcher confirms only direct child rows whose raw `parent_id`
matches the Pi spawn. A newer numeric spawn directory without a readable row is
temporarily allocation uncertainty, not an authoritative child. Resident drain's
reconciled transitive tree is therefore not yet Pi's descendant source. Keep
extension notification and bash state independent of persisted descendant state.

### ExtensionAPI (`types.ts`)

Shared TypeScript interface between Pi and extensions:

- `registerTool(definition)` — register a tool with name, description, input schema, and call handler
- `registerHook(name, handler)` — register lifecycle hooks where Pi exposes them
- `session.on(event, handler)` — subscribe to session events
- `session.sendMessage(message, options)` — send an agent follow-up message; spawn-watch uses this for implicit-wait notifications

### Spawn Correlation

`managed-bash` injects `MERIDIAN_PI_BASH_ID=b-*` into every child process. If that
process runs `meridian spawn` (directly, through `uv run meridian`, or through a wrapper),
Meridian's spawn store persists the value as `originating_bash_id` on the child spawn
record. `meridian-spawn-watch` reads disk state and uses that field to scope `/spawn`
rows and notifications to the current Pi session.

**Sidecar origin tracking (`spawn_origins.ts`).** A separate sidecar file
(`pi-bash/<spawn-id>/spawn-origins.json`) bridges gaps in the env-propagation chain.
`managed-bash` calls `rememberSpawnOriginBashIds()` at process start to record the
bash ID in this sidecar. `meridian-spawn-watch` reads `readSpawnOriginBashIds()` at
startup to discover bash IDs that may not yet appear in `bash-records.json` (due to
atomic write timing) or that were written by concurrent bash processes. The sidecar
serializes concurrent writes through a per-file promise chain so no origin is lost.

This two-channel design (env propagation + sidecar) means spawn correlation works even
when a bash process starts before `bash-records.json` is persisted, or when a spawn
state.json appears on disk before the bash record that launched it.

Do not reintroduce argv parsing as the authority. Env propagation plus sidecar plus
spawn-record writes are the stable bridge.

### Build Invariant

Extensions must be built before Pi launch. The projection layer raises
`PiExtensionProjectionError` if `dist/extensions/<name>/index.js` and the installed bundle
copy are both missing.

## Rationale

### Why In-Process Extensions

Pi's RPC protocol gives Meridian a bidirectional JSON-RPC session, but not enough native
surface for background task tracking, child-spawn correlation, or follow-up notification
policy. In-process extensions can override tools, observe session events, and call
`sendMessage()` without wrapping Pi in a fake terminal or scraping stdout.

### Why Disk Instead of Sidecar Events

Current Pi coordination is state-based:

- extension writes durable task/spawn/notification state;
- `PiDiskWatcher` wakes the Python drain loop on changes;
- `PiQuiescenceTracker` evaluates current state before finalization.

State files survive crashes and work for nested `uv run meridian ... spawn` commands
without the parent needing to parse command strings or receive every event in order.

### Why TypeScript

Pi's extension system is TypeScript-native. Bundling with tsup/esbuild produces ESM
output targeting Node 20, which matches Pi's runtime. Extension imports must stay at
package roots (`@earendil-works/pi-tui`, `@earendil-works/pi-coding-agent`) because
subpath imports break under Pi's extension loader.

## Related .context/

- [../../lib/harness/.context/CONTEXT.md](../../lib/harness/.context/CONTEXT.md) — PiAdapter, runtime resolution, quiescence completion model
- [../../lib/harness/projections/.context/CONTEXT.md](../../lib/harness/projections/.context/CONTEXT.md) — extension entrypoint projection
- [../../lib/harness/connections/.context/CONTEXT.md](../../lib/harness/connections/.context/CONTEXT.md) — Pi RPC JSON-RPC transport
- [../../lib/streaming/.context/CONTEXT.md](../../lib/streaming/.context/CONTEXT.md) — Pi drain/quiescence policy consumes disk-backed state
