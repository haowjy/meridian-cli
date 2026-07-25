# streaming — Terminal Arbitration and Heartbeat

## Terminal Arbitration

`arbitrate_terminal()` races concurrent async triggers and returns one `ArbitrationDecision`.
The streaming runner (`run_streaming_spawn`) calls this to determine how to finalize a spawn.

Priority order (highest to lowest):
1. **Terminal frame** — explicit `result`/terminal event from the harness stream
2. **Budget exceeded** — token or cost budget exhausted → `status: failed`, `error: budget_exceeded`
3. **Timeout** — wall-clock timeout → `status: failed`, `exit_code: 3`, `error: timeout`
4. **Watchdog** — watchdog reports spawn stopped → `stop_required: False`, no synthetic status
5. **Inactivity** — inactivity watchdog reports spawn stopped → `stop_required: False`, no synthetic status
6. **Completion** — drain loop completed
7. **Signal** — SIGINT/SIGTERM; lowest precedence — if completion already resolved, completion wins

When completion wins, `_completion_decision()` in `terminal_arbitrator.py` immediately
uses an already-finished terminal frame or returns a completion decision. It does not
wait for a later frame.

### Watchdog Noop

When the watchdog task fires with `watchdog_stopped_spawn=False`, `ArbitrationDecision.watchdog_noop=True`.
The caller must not treat this as a termination — the watchdog observed nothing changed.

### Optional Triggers

`timeout_task`, `budget_task`, `watchdog_task`, and `inactivity_task` are optional
(pass `None` to exclude from the race). Their absence does not affect the priority
ordering of the remaining triggers.

## FileHeartbeat

`FileHeartbeat.touch()` updates a file's mtime to the current time. The reaper reads this
file to detect live streaming spawns. Touch it periodically from the streaming loop — the
reaper's liveness window is configured separately in the state system. It never creates
missing parent directories; once the published spawn directory is deleted, later touches
are no-ops.

## Lateral Links

→ [../../process/.context/CONTEXT.md](../../process/.context/CONTEXT.md) — primary process execution (sibling path)
→ [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — launch/ layer context
