# extensions/meridian-lifecycle/ — Context

## Architecture

The coordination brain inside the Pi process. Consumes internal events from managed-bash
and session events from the Pi runtime, then emits canonical lifecycle events to both
the in-process event bus and the sidecar JSONL file. The Python drain loop in
`SpawnManager` reads these events to decide when a spawned Pi session is quiescent.

### Event Flow

```
managed-bash          meridian-lifecycle            Python drain loop
────────────          ──────────────────            ─────────────────
meridian:subspawn:start ──→ track child state ──→ meridian.subspawn.start (sidecar)
meridian:subspawn:end   ──→ resolve outcome   ──→ meridian.subspawn.end   (sidecar)

Pi session events:
  tool_result (bash)  ──→ extract spawn IDs   ──→ (child tracking)
  tool_result (bg_*)  ──→ extract spawn IDs   ──→ (child tracking)
  agent_end            ──→ parent_idle=true    ──→ (gate wave start)
  session_shutdown     ──→ shutdown cleanup

meridian-lifecycle internal:
  wave started         ──→                    ──→ meridian.lifecycle.wave.started
  wave completed       ──→ sendMessage()      ──→ meridian.notification.queued/delivered
  notification done    ──→                    ──→ meridian.notification.completed
  quiescence ready     ──→                    ──→ meridian.quiescence.ready
```

### Session Roles

| Role | Behavior |
|---|---|
| `primary` | Observes but never gates quiescence. No wave timeouts, no child status polling. |
| `spawned` | Full tracking: child waves, notification delivery, quiescence readiness, process cleanup on timeout. |

### Child Tracking

Three sources of child tracking:

1. **Internal subspawn events** — managed-bash emits `meridian:subspawn:start`/`end`.
   The lifecycle extension classifies each by `kind`:
   - `meridian_spawn` — the command is a `meridian spawn` invocation or the event
     explicitly declares `kind=meridian_spawn`
   - `bash` — all other commands
2. **Tool result inspection** — `tool_result` events for `bash`, `bash_bg_wait`,
   `bash_bg_kill`, `bash_bg_read`, and `bash_bg_list` are scanned for spawn IDs
   matching `/\bp\d+\b/g` in their content and details.
3. **Wrapper log tail reading** — when a `meridian:subspawn:end` event arrives for a
   meridian-spawn wrapper (a bash job whose command contains `meridian spawn`), the
   extension reads the wrapper's log tail to discover child spawn IDs. This is the
   "wrapper handoff" mechanism: the wrapper bash job is replaced by its discovered
   children.

### Meridian Spawn Status Poller

For child spawns tracked as `meridian_spawn` kind, a poller runs `meridian --json spawn show <id> --no-report` every 2.5 seconds. It parses the JSON output for terminal statuses (`succeeded`, `failed`, `cancelled`) and finalizes children that have completed. The poller stops when no tracked meridian spawn IDs remain.

If the `meridian` CLI is unavailable (ENOENT), the poller backs off for 30 seconds before retrying.

### Wave / Notification System

When the parent agent becomes idle (`agent_end` event) and tracked children exist:

1. **Wave starts** — `trackedChildIds` populated, deadline timer set (`childWaveTimeoutMs`)
2. **Children drain** — as each child's terminal outcome arrives, it's recorded in
   `wave.outcomes`. The child is removed from `trackedChildIds` and `trackedChildren`.
3. **Wave completes** — when `trackedChildIds` is empty, `sendWaveNotification()` fires:
   - Formats outcomes into a summary string
   - Calls `pi.sendMessage()` with `deliverAs: "followUp"`, `triggerTurn: true`
   - Emits `meridian.notification.queued` then `meridian.notification.delivered`
4. **Notification completes** — on next `agent_end`, emits `meridian.notification.completed`
5. **Wave deadline** — if children don't drain before the deadline, timed-out children
   get `status=timed_out` outcomes, then `cancelTrackedPid()` and `cancelTrackedMeridianSpawn()`
   are called to clean up, followed by the wave notification.

### Quiescence

Quiescence is reached when:
- `parentIdle` is true (agent_end has fired)
- No tracked children (all have finished)
- No pending notifications (all delivered and completed)
- No active wave timer
- `role === "spawned"`

When quiescent, the extension emits `meridian.quiescence.ready` to the sidecar. The
Python drain loop reads this and terminates the Pi process.

## Contracts

### Canonical Lifecycle Events

All emitted to both the internal event bus (`pi.events.emit`) and the sidecar file:

| Event Type | When Emitted |
|---|---|
| `meridian.subspawn.start` | Child process starts (kind, subspawn_id, wait_policy) |
| `meridian.subspawn.end` | Child process ends (kind, subspawn_id, status, success, reason) |
| `meridian.notification.queued` | Wave notification created |
| `meridian.notification.delivered` | Wave notification sent via sendMessage |
| `meridian.notification.completed` | Notification acknowledged (next agent_end) |
| `meridian.notification.failed` | sendMessage threw an error |
| `meridian.quiescence.ready` | All children done, all notifications complete |

### Envelope Fields

Every canonical event carries:
```json
{
  "schema_version": 1,
  "session_id": "<session-id>",
  "parent_spawn_id": "<spawn-id>",
  "correlation_id": "<unique-id>",
  "emitted_at_ms": <timestamp>
}
```

### Child Outcome Statuses

| Status | Meaning |
|---|---|
| `succeeded` | Child completed with exit code 0 |
| `failed` | Child completed with non-zero exit or error |
| `cancelled` | Child was explicitly cancelled |
| `timed_out` | Wave deadline expired before child completed |

### Env Configuration

| Env Var | Default | Purpose |
|---|---|---|
| `MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS` | 300,000 (5 min) | Wave deadline in ms |
| `MERIDIAN_PI_CHILD_WAVE_TIMEOUT_SECONDS` | — | Alternative to MS form |
| `MERIDIAN_PI_CHILD_WAVE_KILL_GRACE_MS` | 2,000 | Grace period between SIGTERM and SIGKILL |

## Rationale

### Wave Pattern vs Immediate Notification

Batching child outcomes into a wave notification avoids notification spam. If 10
children finish in rapid succession, the agent receives one follow-up message
summarizing all outcomes rather than 10 separate messages. The wave deadline ensures
the agent isn't left waiting indefinitely for a slow child.

### Wrapper Handoff

When `meridian spawn` is invoked inside a bash job, the bash job itself is a wrapper —
the real work happens in the spawned child. The wrapper's completion is irrelevant
(generally exit 0), but the child's status matters for quiescence. The handoff
mechanism reads the wrapper's log tail to discover child spawn IDs and replaces the
wrapper with its children in the tracking set.

### Cancellation During Wave Deadline

When the wave deadline fires, the extension sends SIGTERM then SIGKILL to tracked
children with known PIDs. For children tracked as `meridian_spawn` kind, it also runs
`meridian spawn cancel <id>`. The `MERIDIAN_PI_CHILD_WAVE_KILL_GRACE_MS` env var
controls the grace period between signals.

## Related .context/

- [../../managed-bash/.context/CONTEXT.md](../../managed-bash/.context/CONTEXT.md) — producer of `meridian:subspawn:start`/`end` events
- [../../../.context/CONTEXT.md](../../../.context/CONTEXT.md) — build pipeline, shared lifecycle sidecar writer
- [../../../../lib/streaming/.context/CONTEXT.md](../../../../lib/streaming/.context/CONTEXT.md) — `_drain_loop` consumes lifecycle events for quiescence gating
- [../../../../lib/harness/connections/.context/CONTEXT.md](../../../../lib/harness/connections/.context/CONTEXT.md) — `PiLifecycleEventTailer` reads the sidecar; `PiRpcConnection` merges lifecycle events into the event stream
