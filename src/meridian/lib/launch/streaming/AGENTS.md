# launch/streaming/

Support utilities for streaming spawn execution. Two concerns: terminal trigger
arbitration and spawn heartbeat.

## Key Files

- `terminal_arbitrator.py` — `arbitrate_terminal()`: races multiple async termination triggers
  and returns a single `ArbitrationDecision`; used by the streaming runner
- `heartbeat.py` — `FileHeartbeat`: file-touch heartbeat for streaming spawn liveness

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — terminal trigger priority order, arbitration
  semantics, grace window for late terminal frames

## Related

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — launch/ layer context
→ [../process/.context/CONTEXT.md](../process/.context/CONTEXT.md) — primary process execution (sibling)
