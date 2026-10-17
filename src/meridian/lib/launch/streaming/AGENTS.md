# launch/streaming/ — Streaming Terminal Arbitration

Terminal trigger arbitration for streaming spawn execution. It supports
`execute_with_streaming()` in `streaming_runner.py` and is not an entry point itself.

## What's Here

**`terminal_arbitrator.py`** — `arbitrate_terminal()` races multiple async termination
triggers (harness terminal event, timeout, cancel signal, user interrupt) and returns
a single `ArbitrationDecision`. The decision carries which trigger won and the
resulting outcome. The streaming runner acts on this decision to finalize the spawn.

Terminal triggers have a priority order — if multiple fire simultaneously, the
highest-priority trigger wins. Trigger priority is documented in `.context/CONTEXT.md`.

## Key Rules

**`arbitrate_terminal()` is called once per spawn execution.** It races until one
trigger fires, then cancels the others. Do not call it multiple times for the same
spawn.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — trigger priority order, arbitration
   semantics, and optional triggers.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — launch/ layer context; where
  streaming fits in the execution model.
- [../process/.context/CONTEXT.md](../process/.context/CONTEXT.md) — primary process
  execution path (sibling); uses different finalization flow.
