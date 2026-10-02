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
    ├── types.ts              # shared TS types (ExtensionAPI, ToolRegistration)
    ├── shared/               # ids, json files, panels, pi state paths, meridian CLI helpers
    ├── managed-bash/
    │   └── src/index.ts      # bash/bash_manage override, b-* records, /ps* UI
    ├── meridian-spawn-watch/
    │   └── src/index.ts      # spawn disk watcher, implicit-wait notifications, /spawn* UI
    └── session-boundary/
        └── src/index.ts      # native lifecycle observations, no journal writes
```

### Extension Responsibilities

`session-boundary` consumes launch path/nonce handles, writes at most 16 KiB to
`spawns/<run>/pi-session-boundary.json`, and emits nothing on stdout. Python reads
once after process exit, validating nonce and actual child PID.

| Extension | Owns | Writes / observes |
|---|---|---|
| `managed-bash` | `bash` / `bash_manage`, tracked vs detached bash records, `/ps*` slash commands, `_MERIDIAN_PI_BASH_ID` injection into child processes | `runtime_root/pi-bash/<spawn-id>/bash-records.json` and bash logs; terminal waits mark their record's notification as consumed |
| `meridian-spawn-watch` | canonical direct-child discovery, `/spawn*` slash commands, idle-turn completion notifications | observes scoped child rows, task consumption and wait leases; writes exact admission receipts and delivery faults |

`managed-bash` is the mechanism extension. `meridian-spawn-watch` is the policy extension.
Keep that split: shell task execution and task record persistence belong in managed-bash;
child-spawn observation and notification behavior belong in spawn-watch.
Terminal tracked Bash rows are durable result obligations. Their persisted
`notification_consumed_at_ms` consumes a terminal wait; exact custom-message
admission consumes an unattended notice. Sending or queueing is not consumption.
Managed-bash serializes record snapshots; terminal state is persisted before
waiters are released, and consumption is persisted before the terminal wait
result returns. If consumption persistence fails, the marker is rolled back and
the wait returns an error, leaving the completion eligible for notification.
Spawn-watch publishes only while native `ctx.isIdle()`, and rereads consumption
and live wait reservations after formatting. A changed child selection rebuilds
the batch. One launch-scoped owner reserves exact message membership until its
native `message_start` admission; reload rebinds that owner and preserves its queue
claims. Cold restart retries unreceipted work and keeps admitted/consumed work.
Scan, formatting and receipt failures are supervised in `delivery-fault.json`.
Notification formatting uses `meridian spawn wait --no-observe`: fetching a
result must not consume the parent's notification before delivery succeeds.
Final shutdown stops publication; reload preserves the owner. Polling plus one
non-resetting scheduled scan guarantees progress under continuous writes.
See [delivery-contract.md](delivery-contract.md) for schemas, public-event fencing
and the bounded receipt-to-public-observation crash window.

### Build Pipeline

`npm run build:extensions` runs four scripts in sequence:

1. `build:extensions:clean` — removes `./dist/extensions`
2. `build:extensions:managed-bash` — `tsup` bundles `managed-bash/src/index.ts` → ESM, Node 20, single-file output
3. `build:extensions:meridian-spawn-watch` — bundles `meridian-spawn-watch/src/index.ts` the same way
4. `build:extensions:session-boundary` — bundles the native session observer

`npm run verify:extensions` rebuilds and runs Vitest coverage for the extension sources.

Output goes to `dist/extensions/`. Python launch projection resolves entrypoints with
`pi_extension_projection.py`, preferring the repo build output during local development
and falling back to the installed bundle root from `pi_paths.resolve_meridian_pi_extension_root()`.
A missing bundle raises `PiExtensionProjectionError` with the build command.

### Extension Loading

Pi loads extensions via explicit `-e <path>` CLI flags. Meridian launches with
`--no-extensions` and then adds only the selected Meridian bundles, so ambient user
extensions do not change spawn behavior.

- **spawned RPC mode**: `managed-bash` + `meridian-spawn-watch` + `session-boundary`
- **primary native TUI mode**: `meridian-spawn-watch` + `session-boundary`; no bash override and no spawned-session auto-stop

Role-specific behavior is gated by environment, including `_MERIDIAN_PI_SESSION_ROLE` and
`_MERIDIAN_PI_STATE_DIR`.

## Contracts

### Disk-State Coordination

The Python streaming layer separates persisted-descendant and Pi-private
quiescence inputs:

- valid rows under `runtime_root/spawns/` — read through the shared reconciled
  transitive descendant evidence
- `runtime_root/pi-bash/<parent>/bash-records.json` — tracked/detached bash records
- exact admission and public observation files — causal notification membership,
  fenced by the matching public message event
- explicit result consumption/dismissal, process-owned wait leases and delivery faults

Writes must use the shared JSON-file helpers so readers never observe half-written JSON.
Missing authority is empty. Present malformed, truncated, wrong-parent or invalid
schema evidence is unknown; readers recheck disk before final quiescence. Retired
`last-notification.json` is ignored.

`PiDiskWatcher` reads only private task/delivery coordination files. It does not scan spawn
directories or infer descendants from newer IDs. Both Pi and resident drains use
the shared reconciled transitive tree; keep extension notification and bash state
independent of persisted descendant state.

### ExtensionAPI (`types.ts`)

Shared TypeScript interface between Pi and extensions:

- `registerTool(definition)` — register a tool with name, description, input schema, and call handler
- `registerHook(name, handler)` — register lifecycle hooks where Pi exposes them
- `session.on(event, handler)` — subscribe to session events
- `sendMessage(message, options)` — void queue/prompt operation, never admission acknowledgement
- native `message_start` — exact custom-message admission; receipt hook runs before public RPC event
- `ctx.isIdle()` — publication capability; `ctx.ui.notify()` frames slash output through RPC

### Spawn Correlation

`managed-bash` injects `_MERIDIAN_PI_BASH_ID=b-*` into every child process. If that
process runs `meridian spawn` (directly, through `uv run meridian`, or through a wrapper),
Meridian's spawn store persists the value as `originating_bash_id` on the child spawn
record. The watcher scopes `/spawn` and notifications by canonical direct
`parent_id`. A matching `originating_bash_id` transfers the launcher obligation
to those children. Logs, remembered discovery, timers and origin sidecars do not
establish authority, including during atomic publication races.

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

Session boundary consumes launch path/nonce handles and atomically publishes a bounded record. Initial entry never changes on switches; only a final shutdown/quit with readable identity supplies exit identity. No itinerary or native journal writes.

Pi lifecycle handlers use their invocation ctx, never a retained ctx. Pi 0.87.1
can race RPC stdin EOF against session replacement: a freshly supplied shutdown
ctx can still belong to an invalidated runner. Boundary v2 records that shutdown
without identity, clearing any earlier quit rather than poisoning the observer.
Do not infer an exit from current/targetSessionFile or synthesize a quit for the
replacement. EOF after a completed RPC switch does emit quit for the new session.
