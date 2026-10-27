# server/ — MCP Stdio Server

FastMCP stdio server for `meridian serve`: two tools (`extension_list_commands`,
`extension_invoke`) routed through the same `ExtensionCommandDispatcher` as the CLI, so
the dispatcher's surface checks decide what MCP can call.

- **Stdout is the MCP transport.** Logging runs `json_mode=True`; telemetry runs in
  `STDERR` mode (rootless, no Segment storage). Nothing else may write to stdout.
- **Bootstrap is read-only** (`prepare_for_runtime_read`) except for the identity-writing
  commands in `_IDENTITY_WRITING_TOOLS` (`meridian.spawn.create`, `meridian.spawn.continue`), which use
  `prepare_for_runtime_write`. Add to that set only a command that must mint identity.
