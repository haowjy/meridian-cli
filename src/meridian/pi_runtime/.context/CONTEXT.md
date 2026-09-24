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
│       ├── meridian-spawn-watch/index.js
│       └── session-boundary/index.js
└── extensions/
    ├── shared/               # ids, json files, panels, pi state paths, meridian CLI helpers
    ├── managed-bash/
    │   └── src/index.ts      # bash/bash_manage override, b-* records, /ps* UI
    ├── meridian-spawn-watch/
    │   └── src/index.ts     # spawn disk watcher, implicit-wait notifications, /spawn* UI
    └── session-boundary/
        └── src/index.ts     # bounded native session-boundary observer
```

### Extension Responsibilities

| Extension | Owns | Writes / observes |
|---|---|---|
| `managed-bash` | `bash` / `bash_manage`, tracked vs detached bash records, `/ps*` slash commands, `_MERIDIAN_PI_BASH_ID` injection into child processes | `runtime_root/pi-bash/<spawn-id>/bash-records.json` and bash logs |
| `meridian-spawn-watch` | correlated spawn discovery, `/spawn*` slash commands, implicit-wait `sendMessage({triggerTurn: true})` notifications | watches `runtime_root/spawns/<child>/state.json`, reads `originating_bash_id`, writes `runtime_root/pi-bash/<spawn-id>/last-notification.json` |
| `session-boundary` | process-lifetime Pi lifecycle observation for the identity-qualified RPC close path | one atomic `runtime_root/pi-session-boundaries/<launch-nonce>/state.json` snapshot (`ready`, `quit_candidate`, `invalid`) |

`managed-bash` is the mechanism extension. `meridian-spawn-watch` is the policy extension.
Keep that split: shell task execution and task record persistence belong in managed-bash;
child-spawn observation and notification behavior belong in spawn-watch.
The session-boundary observer remains independent of both concerns and does not write
their files or participate in `PiDiskWatcher`.

Tracked exact-resume RPC launches must pass an explicit owner-supplied notification-gate
capability (`_MERIDIAN_PI_NOTIFICATION_GATE_*`) with version, attempt, and random nonce.
The extensions consume and erase it at registration; it identifies tracked mode only,
never a chat or admitted entry. The process-scoped gate starts closed, releases on the
first owner-initiated `agent_start`, and revokes queued/in-flight notification work on
session replacement, reload, or close. B3c launch projection must require and pass this
capability for every tracked RPC and reject a missing capability before child exec; do
not infer tracked mode from cN or ambient environment inheritance. Unqualified native
TUI/RPC continues with ordinary extension behavior. While tracked and closed, spawn-watch
does not startup-scan; it resumes discovery after each admitted run. Every managed
extension capable of `sendMessage({triggerTurn:true})` must share this same admission gate.

### Build Pipeline

`npm run build:extensions` runs clean and builds all three bundles:

1. `build:extensions:clean` — removes `./dist/extensions`
2. `build:extensions:managed-bash` — `tsup` bundles `managed-bash/src/index.ts` → ESM, Node 20, single-file output
3. `build:extensions:meridian-spawn-watch` — bundles `meridian-spawn-watch/src/index.ts` the same way
4. `build:extensions:session-boundary` — bundles the process-scoped native lifecycle observer

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

Role-specific behavior is gated by environment, including `_MERIDIAN_PI_SESSION_ROLE` and
`_MERIDIAN_PI_STATE_DIR`.

## Contracts

### Disk-State Coordination

The Python streaming layer separates persisted-descendant and Pi-private
quiescence inputs:

- valid rows under `runtime_root/spawns/` — read through the shared reconciled
  transitive descendant evidence
- `runtime_root/pi-bash/<parent>/bash-records.json` — tracked/detached bash records
- `runtime_root/pi-bash/<parent>/last-notification.json` — last implicit-wait notification marker

Writes must use the shared JSON-file helpers so readers never observe half-written JSON.
Readers tolerate truncation/missing files and re-check disk before final quiescence.

`PiDiskWatcher` reads only bash and notification files. It does not scan spawn
directories or infer descendants from newer IDs. Both Pi and resident drains use
the shared reconciled transitive tree; keep extension notification and bash state
independent of persisted descendant state.

### Pi Extension API

Extensions import `ExtensionAPI` from the package root and subscribe with
`pi.on(...)`; there is no local `types.ts` or `registerHook` shim.

- `registerTool(definition)` — register a tool with name, description, input schema, and call handler
- `session.sendMessage(message, options)` — send an agent follow-up message; spawn-watch uses this for implicit-wait notifications

The separate `session-boundary` extension samples the quit context's native
session ID and file path synchronously and publishes a closed, <=16 KiB,
process/attempt-correlated snapshot. The only phases are `ready`,
`quit_candidate`, and sticky `invalid`. It stores no chat IDs, attempt decisions,
transcript contents, or callback history. Its process-lifetime publisher survives
session replacement; writes use a narrow synchronous fsync-file/rename/fsync-dir
path without changing existing helper durability semantics. `PiDiskWatcher` does
not watch this file; the Pi RPC connection owner is the only reader/qualification
authority. That owner must drain stdout through EOF and treat any native
`extension_error` as a qualification veto, since failed replacement can leave an
older candidate on disk. Before quit, native switch notifications are nonterminal;
the process-lifetime observer ignores supported transitions and samples the active
native ID/path from the quit context. Reload invalidates qualification.

### Spawn Correlation

`managed-bash` injects `_MERIDIAN_PI_BASH_ID=b-*` into every child process. If that
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

Extensions must be built before Pi launch. Identity-qualified projection requires
`npm run build:extensions:verify-source`, rejects missing or digest-mismatched source bundles,
and never accepts an installed bundle as a fallback. General extension projection
raises `PiExtensionProjectionError` when required artifacts are unavailable.
The bounded `artifact.json` binds a fixed input allowlist (source, manifest, lockfile,
and build flags) plus emitted bundle SHA-256; the verified artifact ID is exposed to
the session-boundary owner integration for launch correlation.

## Rationale

### Why In-Process Extensions

Pi's RPC protocol gives Meridian a bidirectional JSON-RPC session, but not enough native
surface for background task tracking, child-spawn correlation, or follow-up notification
policy. In-process extensions can override tools, observe session events, and call
`sendMessage()` without wrapping Pi in a fake terminal or scraping stdout.

### Why Disk Instead of Sidecar Events

Current Pi coordination is state-based:

- spawn creation atomically publishes complete persisted rows;
- extensions write durable private bash/notification state;
- `PiDiskWatcher` wakes the Python drain loop on private-file changes;
- bounded polling reassesses the reconciled tree before finalization.

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
