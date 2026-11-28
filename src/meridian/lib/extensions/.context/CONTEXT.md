# Extension commands

The CLI and the MCP server are Meridian's two command surfaces. They use one spec per
command so their validation cannot diverge, and each command declares which surfaces
expose it. There is no HTTP server surface.

The capability check is the seam for future third-party extensions; current commands
require no capabilities and neither surface grants any. Keep the seam: it is planned
work, not dead code. Trust is not part of it yet: every registered command is
Meridian's own, so there is no first-party flag to check. A third-party extension adds
its trust rule with the loader that registers it.

Handlers run in a worker thread so a slow operation does not stall the MCP server's
event loop. `asyncio.to_thread` copies the caller's context, so a handler runs inside
the caller's `warning_cache_scope()`: the CLI's invocation scope for `ext run`, a
per-call scope in the MCP server's `extension_invoke`.
