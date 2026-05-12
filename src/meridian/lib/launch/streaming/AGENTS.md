# launch/streaming/ — Streaming Support Utilities

Two focused utilities for streaming spawn execution: terminal trigger arbitration
and spawn heartbeat. These support `execute_with_streaming()` in `streaming_runner.py`
— they are not entry points themselves.

## What's Here

**`terminal_arbitrator.py`** — `arbitrate_terminal()` races multiple async termination
triggers (harness terminal event, timeout, cancel signal, user interrupt) and returns
a single `ArbitrationDecision`. The decision carries which trigger won and the
resulting outcome. The streaming runner acts on this decision to finalize the spawn.

Terminal triggers have a priority order — if multiple fire simultaneously, the
highest-priority trigger wins. Trigger priority is documented in `.context/CONTEXT.md`.

**`heartbeat.py`** — `FileHeartbeat` touches a file on a fixed interval while a
spawn is alive. The reaper in `lib/state/reaper.py` reads heartbeat age to determine
if a spawn is orphaned (heartbeat age > 120s means the runner may be dead).

## Key Rules

**`arbitrate_terminal()` is called once per spawn execution.** It races until one
trigger fires, then cancels the others. Do not call it multiple times for the same
spawn.

**`FileHeartbeat` must be started and stopped as a context manager** (or via
`start()`/`stop()`). A heartbeat that is never stopped continues touching the file
after the spawn finishes, delaying orphan detection.

**The grace window matters.** After the harness sends a terminal event, there is a
brief window to receive any late terminal frames before the arbitrator cuts off.
Do not remove the grace window — late frames contain finalization data.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — trigger priority order, arbitration
   semantics, grace window timing, heartbeat file location.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — launch/ layer context; where
  streaming fits in the execution model.
- [../process/.context/CONTEXT.md](../process/.context/CONTEXT.md) — primary process
  execution path (sibling); uses different finalization flow.
