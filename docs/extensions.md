# Extensions

Extensions expose registered Meridian operations through the CLI and MCP from a
single `ExtensionCommandSpec`. Both adapters invoke commands in process through
the shared dispatcher.

## `meridian ext` CLI

Discovery and invocation do not require a separate app server.

### Discover commands

```bash
meridian ext list
meridian ext show meridian.mermaid
meridian ext commands
meridian ext commands --json
```

`ext list` groups commands by extension namespace. `ext show` describes one
namespace. The JSON form of `ext commands` returns `schema_version`, a
`manifest_hash`, and command metadata. The hash changes when a command schema,
summary, or surface changes, so clients can use it for cache invalidation.

### Run a command

```bash
meridian ext run FQID [--args JSON] [--work-id ID] [--spawn-id ID] [--request-id ID]
meridian ext run meridian.mermaid.check --args '{"path": "docs"}'
```

`FQID` is `extension_id.command_id`. Output is the command payload in text mode.
With `--json` or `--format json`, success is wrapped as `{"result": ...}` and
errors use `{"status": "error", "code": ..., "message": ...}`.

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Command missing, unavailable on CLI, or returned an error |
| 7 | `--args` is not a JSON object |

## MCP extension tools

`meridian serve` exposes two FastMCP tools over stdio.

### `extension_list_commands`

Takes no arguments. It returns the same discovery fields as
`meridian ext commands --json`, filtered to the MCP surface:

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

### `extension_invoke`

```json
{
  "fqid": "meridian.mermaid.check",
  "args": {"path": "docs"},
  "request_id": "req-abc123",
  "work_id": "docs-cleanup",
  "spawn_id": "p42"
}
```

Only `fqid` is required. `args` defaults to `{}`; the remaining fields provide
optional tracing context. Commands run in process through the same registry,
validation, capability, and error pipeline used by the CLI.

Success returns `{"status": "ok", "result": ...}`. Errors return
`{"status": "error", "code": "...", "message": "..."}`. Common error codes
are `not_found`, `surface_not_allowed`, `args_invalid`, `capability_missing`, and
`handler_error`.
