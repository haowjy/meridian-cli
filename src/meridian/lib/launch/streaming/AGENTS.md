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

**`heartbeat.py`** — `FileHeartbeat.touch()` performs one file timestamp update.
The streaming runner owns periodic scheduling. The reaper in `lib/state/reaper.py`
reads heartbeat age to determine if a spawn is orphaned.

## Key Rules

**`arbitrate_terminal()` is called once per spawn execution.** It races until one
trigger fires, then cancels the others. Do not call it multiple times for the same
spawn.

**`FileHeartbeat` does not schedule itself.** Call `touch()` from the streaming
runner's heartbeat loop; the class has no lifecycle or context-manager API.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — trigger priority order, arbitration
   semantics, and heartbeat file behavior.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — launch/ layer context; where
  streaming fits in the execution model.
- [../process/.context/CONTEXT.md](../process/.context/CONTEXT.md) — primary process
  execution path (sibling); uses different finalization flow.
