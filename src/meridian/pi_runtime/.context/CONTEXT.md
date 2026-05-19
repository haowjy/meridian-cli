# pi_runtime/ — Context

## Architecture

Meridian-owned TypeScript extensions that run inside the Pi harness process. Pi is the
first harness with an in-process extension architecture — other harnesses (Claude,
Codex, OpenCode) are opaque subprocesses. Extensions give Meridian a seam for
observability and coordination that doesn't exist in the Pi CLI natively.

### Directory Layout

```
pi_runtime/
├── package.json              # pnpm workspace root, build scripts, "meridian-pi-extensions"
├── pnpm-workspace.yaml       # declares packages=[], allows esbuild builds
├── pnpm-lock.yaml            # exact dependency tree
├── bin/                      # (reserved for future Pi runtime binaries)
├── dist/                     # build output: splatted entrypoints
│   └── extensions/
│       ├── managed-bash/index.js
│       └── meridian-lifecycle/index.js
└── extensions/
    ├── types.ts              # shared TS types (ExtensionAPI, ToolRegistration)
    ├── shared/
    │   └── lifecycle_sidecar.ts  # JSONL file writer for lifecycle events
    ├── managed-bash/
    │   └── src/index.ts      # bash tool override, tracked/detached jobs
    └── meridian-lifecycle/
        └── src/index.ts      # lifecycle event bus consumer, wave/notification logic
```

### Build Pipeline

`npm run build:extensions` runs three scripts in sequence:
1. `build:extensions:clean` — `rmSync('./dist/extensions', { recursive: true, force: true })`
2. `build:extensions:managed-bash` — `tsup` bundles `managed-bash/src/index.ts` → ESM, Node 20, single file output
3. `build:extensions:meridian-lifecycle` — same for `meridian-lifecycle/src/index.ts`

`tsup` is the bundler (esbuild under the hood). No splitting — each extension is a
self-contained `.js` file. The `--clean` flag in each `tsup` invocation cleans only that
extension's output directory.

Output goes to `dist/extensions/`. The Python-side `pi_extension_projection.py` copies
these built artifacts to a user-level state directory per launch (under
`~/.meridian/meridian-pi/agent/extensions/<launch-id>/`). This prevents stale cached
extensions across launches.

### Extension Loading

Pi loads extensions via `-e <path>` CLI flags. Meridian passes per-launch materialized
paths from `pi_extension_projection.py`:
- **spawned mode**: both extensions loaded (managed-bash + meridian-lifecycle)
- **primary mode**: only meridian-lifecycle loaded (no bash tool override for primary)

## Contracts

### Lifecycle Sidecar (shared/lifecycle_sidecar.ts)

`createLifecycleSidecarWriter(role)` opens a JSONL file for append. The file path comes
from `MERIDIAN_PI_LIFECYCLE_EVENT_FILE` env var — set by `prepare_pi_lifecycle_event_file()`
in the Python layer before spawn.

- **role=spawned**: env var is required — throws if missing or unopenable
- **role=primary**: env var is optional — returns a noop writer if missing
- Writes `JSON.stringify(event) + "\n"` synchronously via `writeSync`. Synchronous write
  is intentional: JSONL integrity requires no interleaving; async writes across extension
  boundaries would require a shared mutex.
- Errors are silently swallowed — no stdout/stderr fallback for machine events

### ExtensionAPI (types.ts)

Shared TypeScript interface between Pi harness and extensions:
- `registerTool(definition)` — register a tool with name, description, input schema, and call handler
- `registerHook(name, handler)` — register a lifecycle hook
- `session.on(event, handler)` — subscribe to session events (used for `tool_result`, `agent_end`, etc.)
- `session.sendMessage(message, options)` — send a message to the parent session (used for wave notifications)

### MERIDIAN_SPAWN_COMMAND_PATTERN

Both extensions share the regex `/\bmeridian\s+spawn\b/` to detect when a bash command
contains a `meridian spawn` invocation. This is the bridge between the bash tool and the
lifecycle extension: managed-bash emits `meridian:subspawn:start`/`end` internal events,
and meridian-lifecycle consumes them to track child spawn lifecycle.

### Build Invariant

Extensions must be built before spawn. The Python projection layer raises
`PiExtensionProjectionError` if `dist/extensions/<name>/index.js` is missing.
The error message directs to `cd src/meridian/pi_runtime && npm run build:extensions`.

## Rationale

### Why In-Process Extensions

The Pi protocol (JSON-RPC over stdio) has no built-in mechanism for tracking child
process lifecycle, notification delivery, or quiescence detection. Instead of wrapping
Pi in an outer subprocess with PTY capture (like Claude), Meridian ships extensions that
run inside the Pi process itself. This gives first-class access to session events
(`tool_result`, `agent_end`, `session_start`, `session_shutdown`) and the ability to
send follow-up messages via `session.sendMessage()`.

### Why TypeScript

Pi's extension system is TypeScript-native. Bundling with tsup/esbuild produces ESM
output targeting Node 20, which is the Pi runtime's Node version. No transpilation to
JavaScript by hand — the bundler handles module resolution and tree-shaking.

### Sidecar vs Inline Events

Lifecycle events are written to a sidecar JSONL file, NOT emitted on stdout. Pi's stdout
is the JSON-RPC transport channel — mixing lifecycle events into it would pollute the
protocol stream. The sidecar file is tailed incrementally by `PiLifecycleEventTailer` in
the Python layer (see [connections/pi_lifecycle_file.py](../../lib/harness/connections/pi_lifecycle_file.py)).

### Synchronous Writes in Sidecar

`writeSync` is used because (a) the sidecar is a low-throughput append-only file and
(b) async writes from different extensions could interleave mid-line, producing broken
JSON. A shared mutex would add complexity with no throughput benefit.

## Related .context/

- [../../lib/harness/.context/CONTEXT.md](../../lib/harness/.context/CONTEXT.md) — PiAdapter, runtime resolution, lifecycle event parsing
- [../../lib/harness/projections/.context/CONTEXT.md](../../lib/harness/projections/.context/CONTEXT.md) — `pi_extension_projection.py` copies built artifacts
- [../../lib/harness/connections/.context/CONTEXT.md](../../lib/harness/connections/.context/CONTEXT.md) — `PiLifecycleEventTailer` reads the sidecar
- [../../lib/streaming/.context/CONTEXT.md](../../lib/streaming/.context/CONTEXT.md) — quiescence drain policy consumes lifecycle events
