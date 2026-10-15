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
│       ├── session-boundary/index.js
│       └── meridian-idle/index.js
└── extensions/
    ├── shared/               # ids, json files, panels, pi state paths, meridian CLI helpers
    ├── managed-bash/
    │   └── src/index.ts      # bash/bash_manage override, b-* records, /ps* UI
    ├── meridian-spawn-watch/
    │   └── src/index.ts      # spawn disk watcher, implicit-wait notifications, /spawn* UI
    ├── session-boundary/
    │   └── src/index.ts      # native lifecycle observations, no journal writes
    └── meridian-idle/
        └── src/index.ts      # primary idle/input sensor and compact actuator
```

### Extension Responsibilities

`session-boundary` consumes launch path/nonce handles and atomically publishes a
v2 record of at most 16 KiB to `spawns/<run>/pi-session-boundary.json`. Python reads
once after process exit, validating nonce and actual Pi PID. Initial entry stays
fixed across switches; only a final readable `session_shutdown(reason=quit)`
supplies exit identity. No itinerary or native journal writes.

Lifecycle handlers use their invocation context, never a retained context. A
shutdown context may already be invalidated by Pi session replacement or reload.
The extension recognizes only the exact error-text prefix
`This extension ctx is stale after session replacement or reload.` and publishes
that shutdown without identity, clearing any earlier quit. Other identity-read
faults poison the record and rethrow. Requalify this error-text dependency when
upgrading Pi; never infer exit from current/target session or the last-seen ID.

| Extension | Owns | Writes / observes |
|---|---|---|
| `managed-bash` | `bash` / `bash_manage`, tracked vs detached bash records, `/ps*` slash commands, `_MERIDIAN_PI_BASH_ID` injection into child processes | `runtime_root/pi-bash/<spawn-id>/bash-records.json` and bash logs; terminal waits and terminal output reads mark their record's notification as consumed |
| `meridian-spawn-watch` | canonical direct-child discovery, `/spawn*` slash commands, boundary/idle completion notifications | observes scoped child rows, task consumption and wait leases; writes exact admission receipts and delivery faults |
| `meridian-idle` | native idle and interactive-return sensing in primary TUIs | shells out to the hidden `meridian idle` API; owns only in-memory timers and calls `ctx.compact` after an `act` decision |

`managed-bash` is the mechanism extension. `meridian-spawn-watch` is the policy extension.
Keep that split: shell task execution and task record persistence belong in managed-bash;
child-spawn observation and notification behavior belong in spawn-watch.

The managed Bash owner is launch-scoped in `globalThis`, keyed by its record
path, so extension reload preserves live process handles and rebinds current
hooks. `session_shutdown(reason=reload)` only clears UI hints. Normal shutdown
terminates all owned groups, including tasks detached from quiescence. A task
owns the POSIX group created for it, uses bounded TERM→KILL escalation, and
publishes terminal after group exit and its queued log writes. Its output queue
applies stream backpressure; failures close owned pipes before cleanup and
release outstanding operations with an error instead of an unhandled rejection.
Output progress is not persisted: `log_bytes` in the records file is the size at the
last lifecycle write (start, background, ping, terminal, consumption), live panels
read the in-memory size, and the logs are the output authority. A log write fault
still fails the task at the chunk; a records-store fault surfaces at the next
lifecycle write, at the latest terminal publication.
Ping delivery is advisory: a stale notification callback releases its durable
ping claim and warns, preserving the live shell owner. It does not retry the
same broken capability in a timer loop; hook rebind or later task activity can
schedule another attempt. Publication failure remains a task/storage failure.

Cold recovery validates and retains the complete record store before accepting
commands. Historical terminal/wait-consumed rows remain intact. Running rows
have no recoverable process handle and retain `status=running` plus
`ownership_lost` in `execution_error` (appended once after any durable fault, never
replacing it); a recorded PID never authorizes signalling.
`bash_manage(kill)` explains the lost ownership, `wait` reports it, and explicit
`detach` durably releases tracking. `runtime_error` records failed publication
when storage permits; a successful later command can repair publication. A
corrupt store is preserved and refused until repaired. Log readers bound bytes
with seek/read, and panels load preview snapshots before rendering.
Wait propagates supervised execution failures even after group cleanup; it does
not consume a failed result. Clearing history prunes returned foreground,
untracked, durably wait-consumed, or causally admitted terminal rows, preserving
unattended background completion obligations. Read admission receipts through the shared strict
reader at the owner's frozen path; invalid evidence refuses pruning.
Native `ctx.mode` determines custom-panel availability: Pi 1.0 has `hasUI=true`
in RPC, but its custom-panel API is a no-op. RPC task output uses required
`ctx.ui.notify`; print mode retains text output. Tables and logs must never be
written directly to the RPC stdout transport.
Shell launch propagates its own Bash ID in `_MERIDIAN_PI_BASH_ID`, and its
durable record retains the enclosing originating Bash ID. Canonical child
spawn rows carry parent/origin membership; no origin sidecar is produced.
Terminal tracked background Bash rows are durable result obligations. Their persisted
`notification_consumed_at_ms` consumes a terminal wait or `output` result with
available logs and its outcome included (execution failures excluded); exact custom-message admission consumes an
unattended notice. Sending or queueing is not consumption. Wait blocks up to the
55-minute bash timeout with no model-chosen limit: a short wait let the model
read results around consumption and receive the same completion again. The
tool abort signal (Esc) ends a wait early. Cancellation before acknowledgement
staging leaves the result owed, even if the pending reply includes a known terminal
status. Once acknowledgement has staged, a successful write returns its result;
a later abort cannot roll it back. Timed-out/pending replies do not consume.
Eligibility uses the outcome observed before log I/O, revalidated for new faults,
not a later ordinary completion. This is not atomic native model delivery.
`BashPublication` owns the immutable published snapshot and its serialized
stage → write → adopt path. Each slot stages against the previous adoption plus
current evidence, writes one prospective value, and adopts that same value only
on success. Failed writes leave no tentative metadata to undo. There is no second
patch/commit description or serialization blacklist.

Live `Execution` objects separately own handles, output queues, progress, creation
proposals and first-fault evidence. A snapshot swap cannot replace them. Readers
see published rows with live progress and negative fault evidence overlaid; those
facts can also ride the next lifecycle snapshot. Positive terminal results,
background/tracking changes, ping claims, consumption and removal still require
publication. Launch returns the owned execution and its separately supervised
creation observation: foreground abort attaches to the former immediately, while a
positive start still awaits the latter. Cancellation noticed after preparation I/O
but before spawn removes its empty logs and launches nothing. A missing, unreadable
or non-regular log is unavailable evidence, not valid empty output. UI inspection
stays non-consuming; `/ps` loads metadata only and owns one bounded selected-row
preview read at a time.

Terminal publication is such a transition. Until it lands the record stays
`running` and owned, so list, wait, output, kill, and foreground results never
present, wait on, or consume an unpublished clean exit; a wait interrupted before
publication still reports running. Its linearization point is staging: success is staged only if no
failure has claimed the record, so an earlier failure prevents a clean terminal
snapshot; one claimed afterwards is ordered after it and repaired by the failure's
own write (a crash inside that rename window can still recover the success).
A task-fatal stage/write failure claims its record inside the failing publication
slot, before any successor can stage. Caller promise depth cannot change that
order. Claiming returns no completion promise: cleanup/repair run separately, so
output drain and physical interruption cannot inherit a metadata wait. Detach,
consumption and clear failures instead remain caller-recoverable errors. A failed terminal write
fails the record (`killed` plus `execution_error`), returns a failed wait result,
and leaves the notice owed. A consumption is staged in its own slot and is skipped if the record
has failed meanwhile. `kill` reports the published outcome, so a task that exited by
itself is not "killed". Group-signalling authority ends where physical ownership
does: once `ShellTask` observes its group gone (including ESRCH from a signal and
ps queries already in flight) it never signals or probes it again, however long
publication takes. Interrupting
a foreground command returns after the physical stop and flushed output, not after
creation or terminal publication. A clean interruption publishes as `killed`; a
fault overlay may show negative evidence before its repair write lands. Foreground
hotkey/timeout handoff is positive only when its queued transition finds a published,
still-running row; a creation failure or terminal publication ahead of it declines
the handoff without resurrecting or backgrounding the row.
Consumption is persisted before the terminal wait or output result returns. If it
fails the marker was never applied and the wait returns an error, leaving the
completion eligible for notification.
Spawn-watch serializes publication through one launch-scoped owner. A successful
tool-bearing boundary and the genuinely late idle fallback use that owner rather
than separate delivery machinery. Automatic child notices read `report.md`
directly instead of invoking the general `spawn wait` CLI. Candidate IDs reduce
ordinary idle work but never become authority; canonical state and parent-local
evidence remain authoritative at publication. Two nonrecursive watchers and the
existing timers wake the owner; active runs do no polling I/O.

See the canonical [delivery contract](delivery-contract.md) for discovery and
publication sequencing, fixed report/member/message bounds, subtract-only
revalidation, cancellation generations, exact admission/public observation, and
restart/crash behavior. Scan, report, and receipt failures remain supervised in
`delivery-fault.json`.

### Build Pipeline

`pnpm run build:extensions` cleans once, then builds four extension entrypoints:

1. `build:extensions:clean` — removes `./dist/extensions`
2. `build:extensions:managed-bash` — `tsup` bundles `managed-bash/src/index.ts` → ESM, Node 20, single-file output
3. `build:extensions:meridian-spawn-watch` — bundles `meridian-spawn-watch/src/index.ts` the same way
4. `build:extensions:session-boundary` — bundles the native session observer
5. `build:extensions:meridian-idle` — bundles the primary idle sensor/actuator

`pnpm run verify:extensions` rebuilds and runs Vitest coverage for the extension sources.

Output goes to `dist/extensions/`. Python launch projection resolves entrypoints with
`pi_extension_projection.py`, preferring the repo build output during local development
and falling back to the stable install root from `pi_paths.resolve_meridian_pi_extension_root()`.
A missing bundle raises `PiExtensionProjectionError` with the build command.

### Extension Loading

Pi loads stable bundles via explicit `-e <path>` flags; projection does not copy
extensions into per-launch agent directories. Both roles load managed-bash and
spawn-watch when their `[harness.pi]` toggles are enabled. Session-boundary is
always loaded by the adapter and has no config toggle. Meridian-idle is selected
only when the launch profile is interactive. It passes `--interactive` on every
`meridian idle` call, serializes arm/return transitions without making user input
wait for the CLI, and re-checks live idle and pending-message state after a
compact claim before calling Pi's aborting `ctx.compact()`.

- **Spawned RPC**: suppress ambient extensions by default (`--no-extensions`),
  skills, context files, and prompt templates. `load_all_pi_extensions = true`
  retains native extension discovery and adds configured extra roots.
- **Primary native TUI**: retain native discovery, including ambient extensions;
  managed-bash overrides bash when enabled. No spawned-session quiescence auto-stop.

Both projectors reject passthrough mode and extension-loading selectors.
`MERIDIAN_SESSION_ROLE` gates role-specific behavior;
`_MERIDIAN_PI_STATE_DIR` points extension disk state at the project runtime.

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

### Native ExtensionAPI

Extensions import the native `ExtensionAPI` from the Pi package root:

- `registerTool(definition)` — register a tool with name, description, input schema, and call handler
- `on(event, handler)` — register typed native lifecycle and session hooks
- `sendMessage(message, options)` — void queue/prompt operation, never admission acknowledgement
- native `message_start` — exact custom-message admission; receipt hook runs before public RPC event
- `ctx.isIdle()` — idle-fallback publication capability; `ctx.ui.notify()` frames slash output through RPC

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
output targeting Node 20 syntax; CI builds use Node 24. Extension imports must stay at
package roots (`@earendil-works/pi-tui`, `@earendil-works/pi-coding-agent`) because
subpath imports break under Pi's extension loader.

## Related .context/

- [../../lib/harness/.context/CONTEXT.md](../../lib/harness/.context/CONTEXT.md) — PiAdapter, runtime resolution, quiescence completion model
- [../../lib/harness/projections/.context/CONTEXT.md](../../lib/harness/projections/.context/CONTEXT.md) — extension entrypoint projection
- [../../lib/harness/connections/.context/CONTEXT.md](../../lib/harness/connections/.context/CONTEXT.md) — Pi RPC JSON-RPC transport
- [../../lib/streaming/.context/CONTEXT.md](../../lib/streaming/.context/CONTEXT.md) — Pi drain/quiescence policy consumes disk-backed state
