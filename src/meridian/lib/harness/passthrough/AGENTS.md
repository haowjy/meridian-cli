# harness/passthrough/ — Managed-Primary TUI Attach

Builds the two artifacts needed for managed-primary sessions: the `ConnectionConfig`
for Meridian's backend connection to the harness, and the attach command for the
user-facing TUI process. Nothing else.

## Mental Model

In a managed-primary session, Meridian connects to the harness first (as turn owner),
then launches a TUI process for the user to observe/interact. This package builds
both sides of that setup:
1. `build_config()` — produces `ConnectionConfig` for Meridian's connection.
2. `build_tui_command()` — produces the command the user's TUI will run to attach.

**Claude does not support managed-primary.** `ClaudePassthrough` always raises
`PassthroughError`. Claude primary sessions use the PTY path instead.

**Codex pre-reserves a port.** Port reservation happens inside `build_config()`
before the harness process starts. This creates a race window: if the harness binds
a different port, the pre-reserved one is wrong. The connection layer handles retries.

## Key Rules

**Access via `get_passthrough(harness_id)`** — not by instantiating classes directly.
Raises `PassthroughError` for unsupported harnesses (Claude).

**The two build steps are ordered.** Call `build_config()` first (creates
the server endpoint), then `build_tui_command()` (uses the endpoint URL from the config).
Reversing the order will produce a TUI command pointing at nothing.

## Entry Points

- `registry.py` — `get_passthrough(harness_id)`.
- `base.py` — `TuiPassthrough` Protocol, `PassthroughError`, `TuiCommandBuilder`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — two-phase protocol detail, port
   pre-reservation race, attach command shapes per harness.

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — managed-primary vs subprocess
  launch paths; where passthrough fits in the adapter lifecycle.
- [../connections/AGENTS.md](../connections/AGENTS.md) — `ConnectionConfig` and
  `HarnessConnection.observer_endpoint` used by both build steps.
