# MCP Tools

`meridian serve` exposes Meridian operations as FastMCP tools over stdio.

## Start the server

```bash
meridian serve
```

Minimal MCP configuration:

```json
{
  "mcpServers": {
    "meridian": {
      "command": "meridian",
      "args": ["serve"]
    }
  }
}
```

## `extension_list_commands`

Takes no arguments. Returns commands registered on the MCP surface with their
fqid, summary, schemas-derived manifest hash, and surfaces.

```json
{
  "schema_version": 1,
  "manifest_hash": "a3f9c2b1d8e74f06",
  "commands": [
    {
      "fqid": "meridian.mermaid.check",
      "extension_id": "meridian.mermaid",
      "command_id": "check",
      "summary": "Validate mermaid diagram syntax in a markdown file or directory",
      "surfaces": ["cli", "mcp"]
    }
  ]
}
```

Clients can cache discovery and compare `manifest_hash` to detect registry
changes.

## `extension_invoke`

Invokes an MCP-surface command in process:

```json
{
  "fqid": "meridian.mermaid.check",
  "args": {"path": "docs"},
  "request_id": "req-abc123",
  "work_id": "docs-cleanup",
  "spawn_id": "p42"
}
```

Only `fqid` is required. `args` defaults to `{}`. The remaining fields are
optional handler and tracing context.

Success returns `{"status": "ok", "result": ...}`. Errors return
`{"status": "error", "code": "...", "message": "..."}`. Common codes are
`not_found`, `surface_not_allowed`, `args_invalid`, `capability_missing`, and
`handler_error`.

See [extensions.md](extensions.md) for the CLI and shared extension contract.
