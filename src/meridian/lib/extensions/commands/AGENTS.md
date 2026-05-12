# extensions/commands/ — First-Party Handler Implementations

This directory holds handler logic for first-party extension commands. Each file defines one or more `ExtensionCommandSpec` instances. The files here are command implementations, not registration — specs are registered in `lib/extensions/first_party.py`.

## What Lives Here

- `mermaid.py` — `MERMAID_CHECK_SPEC`: validates Mermaid diagram syntax in a file or directory
- `sessions.py` — `ARCHIVE_SPAWN_SPEC`, `GET_SPAWN_STATS_SPEC`: archive a spawn; retrieve token stats
- `workbench.py` — `PING_SPEC`: health check for the extension system itself
- `biomed.py` — placeholder for biomed runtime bridge commands (currently empty)

## Adding a Command

1. Write an async handler: `async def my_handler(args: dict, context, services) → ExtensionResult`
2. Or use `ExtensionCommandSpec.from_op()` if you have a single Pydantic input model
3. Construct `ExtensionCommandSpec(first_party=True, ...)`
4. Register the spec in `../first_party.py`

Handler must return `ExtensionJSONResult` or `ExtensionErrorResult` — never raise. Return `ExtensionErrorResult` with a meaningful error code instead of letting exceptions propagate (the dispatcher catches unhandled exceptions as `handler_error`, but that loses context).

## Key Rules

- `first_party=True` is required for all specs here
- Commands requiring the app server: set `requires_app_server=True`. These return `app_server_archived` in the MCP server (app server is archived)
- Import from `meridian.lib.*` freely — these are internal implementations, not plugin boundary code

## Related

→ [../.context/CONTEXT.md](../.context/CONTEXT.md) — dispatcher pipeline, handler signature contract, surface rules, capability model
