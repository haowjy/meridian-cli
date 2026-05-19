# extensions/managed-bash/ — Context

## Architecture

Replaces Pi's default bash tool with a Meridian-managed implementation that supports
foreground (blocking), background (tracked), and detached execution modes. Every bash
command becomes a job with durable state and observable lifecycle.

A single `ManagedBashRegistry` owns all jobs for a Pi session. The registry writes
per-job JSON metadata and log files to a state directory under `get_user_home()`.

### Job Lifecycle

```
startJob() → running
                ├─ completes → finishJob() → exited (exitCode 0)
                │                              or killed (signal)
                ├─ killJob() → killed
                └─ orphanScan/poller → exited/killed (recovery)
```

### Wait Policies

| Policy | Behavior |
|---|---|
| `tracked` | Meridian-lifecycle tracks this job as a subspawn; completion may gate quiescence |
| `detached` | Job runs independent of lifecycle tracking; completion is fire-and-forget |

The `detachJob()` call is the boundary: when `emitted_start` flips to `true`, the
lifecycle extension receives `meridian:subspawn:start` (and potentially `:end` if
already completed) via the internal event channel.

### Foreground vs Background

The `bash` tool has two modes controlled by the `background` parameter:

- **Foreground** (`background=false`, default): blocks until completion or timeout.
  On timeout, automatically detaches to background and returns a job ID.
  On abort signal, kills the job and returns cancelled state.
- **Background** (`background=true`): starts the job, calls `detachJob()` immediately,
  returns the job ID. The caller uses `bash_bg_read`/`bash_bg_wait`/`bash_bg_kill`
  to interact.

### Output Capture

Both stdout and stderr are piped. The last 32 chunks of each are kept in-memory for
foreground result formatting. All output is appended to a per-job log file. Log files
are capped at `MAX_LOG_BYTES` (10 MiB) — when exceeded, the most recent 10 MiB are kept
via atomic tail truncation.

### Process Cleanup

`killProcessTree` sends `SIGTERM` to the process group (`kill(-pid)`) on POSIX, falling
back to per-process `kill(pid)`. `killProcessTreeHard` sends `SIGKILL`. Windows uses
per-process `SIGTERM`/`SIGKILL` only — process-tree kill is deferred.

## Contracts

### Internal Events

Managed-bash emits two internal events consumed by the meridian-lifecycle extension:
- `meridian:subspawn:start` — when a job is detached (or starts as background)
- `meridian:subspawn:end` — when a job completes (natural exit, kill, or orphan recovery)

These are emitted via both `pi.events.emit()` (in-process event bus) and
`lifecycleWriter.append()` (sidecar file). The lifecycle extension consumes the
in-process events; the sidecar file is consumed by the Python drain loop.

### Job Record (JobRecord)

Persisted to `<jobsDir>/<job_id>.json`. Key fields:
- `job_id`, `command`, `wait_policy`, `status` (running/exited/killed)
- `pid`, `started_at_ms`, `ended_at_ms`, `duration_ms`
- `exit_code`, `signal`, `success`
- `log_path`, `log_bytes`, `log_truncated`
- `emitted_start` — boolean guard: has this job's start been emitted to lifecycle?

### Meridian Spawn Detection

`isMeridianSpawnCommand()` checks `/\bmeridian\s+spawn\b/` against the command string.
When true, the `command_is_meridian_spawn` field in lifecycle events is set to `true`,
which the lifecycle extension uses to classify the subspawn kind as `meridian_spawn`
rather than `bash`.

### Crash-Only Design

Every write is atomic (temp file + rename). The registry performs an `orphanScan()` at
startup that:
- Loads all persisted `.json` records
- For records with `status=running`, checks process liveness via `process.kill(pid, 0)`
- Finalizes dead processes with `signal="orphan-exited"`
- A 2-second poller catches processes that were alive during orphan scan but die later

### Concurrency

Log writes are serialized via `logWriteChain` — a promise chain that ensures ordered
appends without a mutex. `closeLogHandle()` awaits the chain before closing so no
writes are lost.

### Size Limits

| Constant | Value |
|---|---|
| `MAX_COMMAND_LENGTH` | 512 bytes |
| `MAX_LOG_BYTES` | 10 MiB |
| `MAX_FOREGROUND_TAIL_BYTES` | 16 KiB (split between stdout/stderr) |
| `MAX_BG_READ_BYTES` | 64 KiB |
| `DEFAULT_BG_READ_BYTES` | 8 KiB |
| `DEFAULT_TIMEOUT_MS` | 120,000 (2 min) |
| `MAX_BG_WAIT_TIMEOUT_MS` | 600,000 (10 min) |

## Tools Exposed

| Tool | Purpose |
|---|---|
| `bash` | Execute a command (foreground or background) |
| `bash_bg_list` | List managed background jobs |
| `bash_bg_read` | Read job log output (with offset support) |
| `bash_bg_wait` | Block until a background job completes |
| `bash_bg_kill` | Terminate a background job |

## Rationale

### Why Override bash Instead of Wrapping

Pi's native bash tool has no concept of job tracking, background execution, or lifecycle
events. Wrapping it would require intercepting tool calls at the protocol level — complex
and fragile. Replacing it via the extension system gives full control over execution
lifecycle, output capture, and event emission.

### Detached Wait Policy

Detached jobs exist for commands like `meridian spawn` where the parent shouldn't gate
quiescence on the child's completion. The child will be tracked independently through
spawn ID extraction from output text.

### Async Log Chain vs Mutex

The `logWriteChain` promise pattern avoids the complexity of a mutex while ensuring
ordered writes. Since each write is an async `appendFile` to the same fd, ordering
matters — out-of-order writes would produce garbled log output.

## Related .context/

- [../../meridian-lifecycle/.context/CONTEXT.md](../../meridian-lifecycle/.context/CONTEXT.md) — consumer of `meridian:subspawn:start`/`end` events
- [../../../.context/CONTEXT.md](../../../.context/CONTEXT.md) — build pipeline, shared lifecycle sidecar
- [../../../../lib/harness/connections/.context/CONTEXT.md](../../../../lib/harness/connections/.context/CONTEXT.md) — `PiLifecycleEventTailer` reads sidecar output
