# server/

FastMCP stdio server — exposes meridian extension commands to MCP clients
(Claude Desktop, etc.). Two tools: `extension_list_commands` and
`extension_invoke`. Runs as a standalone process via `meridian serve`.

## Entry Points

- `main.py` — `run_server()` starts the stdio server; `mcp` is the FastMCP app

## Key Behaviors

- Logging configured to `json_mode=True` (no TTY noise on stdio)
- Telemetry installed with `mode=STDERR` (rootless, no segment storage)
- Commands with `requires_app_server=True` return `app_server_archived` —
  the app server is archived; only in-process (`requires_app_server=False`)
  commands are supported
- Bootstrap uses `prepare_for_runtime_read` — read-only, no directory creation

## Related

- `../lib/extensions/` — extension registry and dispatcher
- `../plugin_api/` — stable types used by hooks (separate from extensions)
