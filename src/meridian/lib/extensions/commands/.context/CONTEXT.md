# extensions/commands — Context

Command-specific contracts. The parent `.context/` covers dispatcher pipeline,
handler signatures, and surface rules — this file covers each command's app-server
requirement, error codes, and behavioral notes.

## Command Inventory

### `meridian.mermaid.check` (`mermaid.py`)

`requires_app_server=False` — runs in-process with no runtime state.

Error codes returned by the handler:
- `node_not_found` — Node.js is absent from PATH
- `bundle_not_found` — the bundled mermaid validator is missing
- `not_found` — the target path does not exist
- `args_invalid` — the provided path cannot be resolved

Success with zero mermaid blocks is not an error — returns `ExtensionJSONResult`
with `total_blocks=0`. Only path-resolution and tool-availability failures produce
`ExtensionErrorResult`.

No `cli_group` / `cli_name` — D26 decision: extension-only, no direct CLI routing.
The `meridian mermaid check` CLI command bypasses extension dispatch (D27).

### `meridian.sessions.archiveSpawn` (`sessions.py`)

`requires_app_server=True` — needs `services.require_runtime_root()` to locate the
spawn store. In the MCP server (no app server), this returns `app_server_archived`.

Routes through `SpawnApplicationService.archive()` for lifecycle policy coordination
(SEAM-5). Does not write to the spawn's `state.json` directly.

Error codes:
- `service_unavailable` — runtime root unavailable
- `invalid_state` — spawn is not in an archivable state

### `meridian.sessions.getSpawnStats` (`sessions.py`)

`requires_app_server=True` — needs `services.require_project_root()` for path
resolution. Delegates to `spawn_stats_sync()` from `ops.spawn.api`.

Error codes:
- `service_unavailable` — project root unavailable

### `meridian.workbench.ping` (`workbench.py`)

`requires_app_server=True` — declared as requiring app server but the handler
ignores all inputs and returns `{"ok": True}`. Used as a round-trip health check.

## Patterns

**`requires_app_server` in the MCP server:** The server passes `project_uuid=None`
to the invocation context when no app server is present. The dispatcher returns
`app_server_required` for these commands rather than calling the handler at all.

**Services access pattern:** Handlers that need spawn or session services call
`services.require_runtime_root()` or `services.require_project_root()` at the
start and return `ExtensionErrorResult(code="service_unavailable", ...)` if they
raise — not exceptions. Never let service-layer exceptions propagate from handlers.

## Related

- [`../AGENTS.md`](../AGENTS.md) — file list and registration instructions
- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — dispatcher pipeline and spec contracts
