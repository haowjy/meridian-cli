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
codex resume <session_id> --remote <ws_url> [prompt]
```

The attach command must not include permission overrides such as `--add-dir`; Codex
0.154 rejects them with `resume --remote`. Workspace roots are projected independently
to the managed app-server configuration. The WebSocket URL comes from
`connection.observer_endpoint.url` — the connection must be in a state where
`observer_endpoint` is set (not None) before this is called. `build_tui_command()`
calls `_require_observer_endpoint_url(connection, transport="ws")`, which raises
`PassthroughError` if the endpoint is absent or has the wrong transport type. A
non-blank `spec.user_turn_content` remains the final optional argument.

### OpenCode: Version-Aware TUI Command Shape

The attach command depends on the resolved OpenCode major version. The
connection's `observer_endpoint.attach_style` selects the dialect:

- **V1** (`attach_style="attach"`, frozen):
  ```
  opencode attach <http_url> --session <session_id>
  ```
- **V2** (`attach_style="server"`, verified against 2.0.6):
  ```
  opencode --server <http_url> --session <session_id>
  ```
  V2 removed the `attach` subcommand; `opencode attach ...` falls through to the
  top-level help and attaches nothing. The bare TUI is the attach surface.

The HTTP URL comes from `connection.observer_endpoint` (transport must be `"http"`).
OpenCode does not use `--add-dir` — workspace roots are injected through the env
override in `ConnectionConfig.env_overrides`, not via CLI flags.

### OpenCode V2: Attach Authentication

V2's server requires HTTP basic auth (`opencode:<password>`), where the password is
printed on the server's stdout at startup. The client does **not** accept URL
userinfo (`http://opencode:pw@host:port`) or a `--password` flag; it reads the
password from `OPENCODE_PASSWORD` (fallback `OPENCODE_SERVER_PASSWORD`) when pointed
at an explicit `--server`.

`OpenCodeV2Connection.observer_endpoint` therefore sets `attach_style="server"` and
carries `client_env={"OPENCODE_PASSWORD": <printed secret>}`. The attach launcher
merges `observer_endpoint.client_env` into the TUI subprocess environment only
(`PrimaryAttachLauncher._tui_env`); it is never written to spawn artifacts. V1 and
Codex endpoints leave `attach_style="attach"` and `client_env={}`, so their path is
unchanged.

Alternative (not used): `opencode serve --service` writes
`$XDG_STATE_HOME/opencode/service.json` (`url`, `pid`, `password`) and clients
auto-discover it under the same `XDG_STATE_HOME`. The private-serve + stdout-password
path is already Meridian's server lifecycle, so the passthrough threads the secret
via env instead of introducing per-spawn state-dir isolation.

### TuiCommandBuilder Lifetime

The callable returned by `build_tui_command()` closes over the connection. It
resolves `observer_endpoint` at invocation time (after the managed backend has
started) — do not capture the endpoint at build time, when it is still `None`.
Endpoint coordinates are stable once the connection is ready; do not retain the
builder past connection teardown.

## Related .context/

- [../../connections/.context/CONTEXT.md](../../connections/.context/CONTEXT.md) —
  `ConnectionConfig`, `ObserverEndpoint`, `PortBindError` definitions and semantics
- [../../.context/CONTEXT.md](../../.context/CONTEXT.md) — managed-primary vs subprocess
  bootstrap paths; which harnesses support observer mode
