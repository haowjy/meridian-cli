# Process scope

Capture the scope snapshot before `connection.stop()` clears it, or cleanup
loses its containment boundary. Use group termination where available: a
single-PID kill misses reparented descendants.

[.context/CONTEXT.md](.context/CONTEXT.md) describes containment tradeoffs.
