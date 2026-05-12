# harness/passthrough/ — Context

## Architecture

Managed-primary launch uses a two-phase protocol. Passthrough implements both phases:

```
Phase 1: build_config() → ConnectionConfig
  → caller starts the backend connection (harness process + bidirectional link)
  → connection exposes observer_endpoint once ready

Phase 2: build_tui_command(connection, spec) → TuiCommandBuilder
  → TuiCommandBuilder is a callable: session_id → tuple[str, ...]
  → caller invokes it once the session ID is known
  → result is the command to exec for the user-facing TUI
```

The two phases are separate because the session ID is not available until the backend
connection is live. The caller drives the lifecycle; passthrough only produces config
and command shapes.

## Contracts

### Claude: Managed-Primary Is Not Supported

`ClaudePassthrough` raises `PassthroughError` on both `build_config()` and
`build_tui_command()`. Claude primary launches use subprocess passthrough (the user's
terminal runs `claude` directly). There is no managed-primary path for Claude — the
harness does not expose an attach mechanism equivalent to `codex app-server` or
`opencode serve`.

Do not attempt to add managed-primary attach for Claude without first confirming that
the harness exposes a stable attach API.

### Codex: Port Pre-Reservation

`CodexPassthrough.build_config()` calls `_reserve_local_port()` — binds a socket to
`127.0.0.1:0` to let the OS assign an ephemeral port, captures it, then closes the
socket. The port number is passed to `CodexConnection` via `ConnectionConfig.ws_port`.

This is a TOCTOU race: the OS may reassign the port between `close()` and `codex
app-server`'s bind. If that happens, `connections/errors.py:PortBindError` is raised
and the caller retries with a new port. This is the intended path — do not catch
`PortBindError` inside passthrough.

### Codex: TUI Command Shape

`_build_codex_attach_command` produces:
```
codex resume <session_id> --remote <ws_url> [--add-dir <root> ...]
```

`--add-dir` entries come from `spec.projected_roots`. The WebSocket URL comes from
`connection.observer_endpoint.url` — the connection must be in a state where
`observer_endpoint` is set (not None) before this is called. `build_tui_command()`
calls `_require_observer_endpoint_url(connection, transport="ws")`, which raises
`PassthroughError` if the endpoint is absent or has the wrong transport type.

### OpenCode: TUI Command Shape

`_build_opencode_attach_command` produces:
```
opencode attach <http_url> --session <session_id>
```

The HTTP URL comes from `connection.observer_endpoint` (transport must be `"http"`).
OpenCode does not use `--add-dir` — workspace roots are injected through the env
override in `ConnectionConfig.env_overrides`, not via CLI flags.

### TuiCommandBuilder Lifetime

The lambda returned by `build_tui_command()` closes over the connection's
`observer_endpoint`. The endpoint URL is stable once the connection is ready, so the
closure is safe to call later. Do not retain the builder past connection teardown.

## Related .context/

- [../../connections/.context/CONTEXT.md](../../connections/.context/CONTEXT.md) —
  `ConnectionConfig`, `ObserverEndpoint`, `PortBindError` definitions and semantics
- [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — managed-primary vs subprocess
  bootstrap paths; which harnesses support observer mode
