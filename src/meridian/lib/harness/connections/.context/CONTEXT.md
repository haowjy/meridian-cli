# Harness connection contracts

Pi injected prompts use `streamingBehavior: "followUp"`, valid both busy and idle,
and wait for their matching RPC response. A rejected inject is reported to its sender
without terminating the spawn; initial-prompt rejection is fatal. One receive task runs
independently of event consumption so a completion nudge cannot block its own receipt.
A timeout after a write leaves delivery uncertain; do not replay it.

Codex's owned thread is the one returned by start, resume or fork. App-server helper
thread announcements are not ownership changes: treating one as the TUI's thread
would send to the wrong thread or disable the route. The connection records helpers
before its own ID is known and latches a TUI move only on evidence of another
non-helper thread. OpenCode similarly detects another parentless session, with V2
fork and inbox events providing additional evidence.

## OpenCode runtime requests

The SSE drain dispatches permission requests in background tasks: a reply POST can
stall, and waiting inline would stop event consumption. Pending requests are registered
before dispatch; unknown IDs fail rather than falling back to a session ID.

A single startup deadline covers backend launch through session creation. Failed
startup cleanup retains the lifecycle gate until publication is settled, even after a
foreground timeout, so stop or another start cannot race a child between owners.

Injection uses the committed native session agent, model and variant, falling back to
the last native user message when fields are absent. Unsent TUI changes are invisible,
and reading then posting is not atomic against simultaneous TUI submission. Continue
preserves native choices. See [OpenCode delivery](../../.context/CONTEXT.md).
