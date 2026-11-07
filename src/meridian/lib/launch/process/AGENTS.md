# launch/process — primary process execution

This layer runs primary harnesses, either by attaching a TUI to a managed backend
or by running the harness directly. Harness-specific behavior belongs in adapter
hooks, not harness-ID branches in the runner.

- Bind assigned native identity before exec and conclude identity once, after
  observation has drained and teardown has joined. Identity validation precedes
  lifecycle completion and invocation attribution.
- Give the TUI exclusive terminal ownership. Check terminal capabilities before
  entering the ownership window, and attach a native TUI to the window's saved
  descriptors. The shell owns native job control; never signal its shared group.
- Signal callbacks run between Python bytecodes: taking a non-reentrant lock can
  deadlock against the interrupted main thread. Latch and act outside the handler;
  see [context](.context/CONTEXT.md) for cancellation and terminal contracts.
- A primary control socket lives inside the launch lock and is removed before
  finalization. Socket bind failures must not fail the launch: an exception in an
  on-running callback can kill the child.

Parent: [launch](../AGENTS.md).
