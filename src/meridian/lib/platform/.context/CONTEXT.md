# Platform

The launch gate persists the blocked child's scope before allowing exec. Its
guardian owns no harness stdio: otherwise the runner can wait for EOF while the
guardian waits for runner death. Closing the gate without `stop` means runner
death and triggers cleanup.

[process_scope/.context/CONTEXT.md](../process_scope/.context/CONTEXT.md)
describes the cleanup tradeoffs.
