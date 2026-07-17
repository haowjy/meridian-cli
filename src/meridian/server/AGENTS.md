# server/ — MCP Stdio Server

FastMCP stdio server that exposes Meridian extension commands to MCP clients (Claude Desktop, etc.). Runs as a standalone process via `meridian serve`. Thin wrapper: two tools, one module.

## Mental Model

The server exposes two MCP tools:
- `extension_list_commands` — lists available extension commands
- `extension_invoke` — invokes a command by fqid

Invocations route through the same `ExtensionCommandDispatcher` used by CLI.
Surface checks in the dispatcher enforce what's callable via MCP.

## Key Behaviors

- Logging runs in `json_mode=True` — no TTY noise on stdio (stdio is the MCP transport)
- Telemetry runs in `STDERR` mode — rootless, no Segment storage
- Commands with `requires_app_server=True` return `app_server_archived` — the app server is archived; only in-process commands are supported
- Bootstrap uses `prepare_for_runtime_read` — read-only, no directory creation

## Entry Points

- `main.py` — `run_server()` starts the server; `mcp` is the FastMCP app object

## Related

- `../lib/extensions/` — extension registry and dispatcher that back both tools
- `../plugin_api/` — stable types used by hooks (separate concern from extensions)
