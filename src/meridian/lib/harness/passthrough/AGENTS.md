# harness/passthrough/

TUI passthrough for managed-primary sessions — builds the `ConnectionConfig` for
the backend connection and the attach command for the user-facing TUI process.

## Files

- `base.py` — `TuiPassthrough` Protocol; `PassthroughError`; `TuiCommandBuilder` type
- `claude.py` — `ClaudePassthrough` — always raises; Claude does not support managed-primary attach
- `codex.py` — `CodexPassthrough` — pre-reserves a loopback port; builds `codex resume <id> --remote <ws_url>`
- `opencode.py` — `OpenCodePassthrough` — builds `opencode attach <http_url> --session <id>`
- `registry.py` — `get_passthrough(harness_id)` dispatch

## Entry Points

`get_passthrough(harness_id)` from `__init__.py` (re-exports from `registry.py`).
Returns a `TuiPassthrough` instance. Raises `PassthroughError` for unsupported harnesses.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — two-phase protocol, port pre-reservation
  race, Claude exclusion, attach command shapes

## Related

- [../.context/CONTEXT.md](../.context/CONTEXT.md) — parent architecture; managed-primary
  vs subprocess launch paths
- [../connections/AGENTS.md](../connections/AGENTS.md) — `ConnectionConfig` and
  `HarnessConnection.observer_endpoint` used by both build steps
