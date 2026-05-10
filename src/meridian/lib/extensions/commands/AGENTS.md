# extensions/commands/

First-party extension command implementations. Each file contains one or more
`ExtensionCommandSpec` instances that register into the extension registry.

## Files

- `mermaid.py` — `MERMAID_CHECK_SPEC`: validate mermaid diagram syntax in a file/directory
- `sessions.py` — `ARCHIVE_SPAWN_SPEC`, `GET_SPAWN_STATS_SPEC`: archive a spawn; get token stats
- `workbench.py` — `PING_SPEC`: health check for the extension system
- `biomed.py` — placeholder for biomed runtime bridge commands (empty)

## Adding a Command

1. Create a handler function matching `ExtensionHandler` (see parent `.context/`)
2. Construct an `ExtensionCommandSpec` with `first_party=True`
3. Register the spec in `lib/extensions/registry.py` alongside the others

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Which commands require `app_server` vs. which are in-process only
- Error codes returned by each handler
- `requires_app_server` behavior in the MCP server (commands return `app_server_archived`)

## Related

- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — dispatcher pipeline, handler
  signature contract, surface rules
