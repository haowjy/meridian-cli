# streaming — Terminal Arbitration and Heartbeat

## Terminal Arbitration

`arbitrate_terminal()` races concurrent async triggers and returns one `ArbitrationDecision`.
The streaming runner (`run_streaming_spawn`) calls this to determine how to finalize a spawn.

Priority order (highest to lowest):
1. **Terminal frame** — explicit `result`/terminal event from the harness stream
2. **Budget exceeded** — token or cost budget exhausted → `status: failed`, `error: budget_exceeded`
3. **Timeout** — wall-clock timeout → `status: failed`, `exit_code: 3`, `error: timeout`
4. **Watchdog** — watchdog reports spawn stopped → `stop_required: False`, no synthetic status
5. **Completion** — drain loop completed; opens a grace window for a late terminal frame
6. **Signal** — SIGINT/SIGTERM; lowest precedence — if completion already resolved, completion wins

### Completion Grace Window

When `completion_task` wins the race and no terminal frame has arrived, `_completion_grace()`
waits up to `grace_seconds` (default 0.5s) for a late `terminal_event_future`. This handles
the common case where the harness emits a terminal event just after the drain loop signals
completion. If the grace window expires without a terminal frame, `ArbitrationDecision` has
`stop_required=False` and no synthetic status — the caller determines final state from the
drain outcome.

### Watchdog Noop

When the watchdog task fires with `watchdog_stopped_spawn=False`, `ArbitrationDecision.watchdog_noop=True`.
The caller must not treat this as a termination — the watchdog observed nothing changed.

### Optional Triggers

`timeout_task`, `budget_task`, and `watchdog_task` are optional (pass `None` to exclude from race).
Their absence does not affect the priority ordering of the remaining triggers.

## FileHeartbeat

`FileHeartbeat.touch()` updates a file's mtime to the current time. The reaper reads this
file to detect live streaming spawns. Touch it periodically from the streaming loop — the
reaper's liveness window is configured separately in the state system.

## Lateral Links

→ [../../process/.context/CONTEXT.md](../../process/.context/CONTEXT.md) — primary process execution (sibling path)
→ [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — launch/ layer context
