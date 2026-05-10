# src/meridian/

Root of the `meridian` Python package. Five top-level subpackages with distinct
audiences and stability contracts.

## Package Map

```
meridian/
  cli/          — CLI entry point and command groups (user-facing)
  server/       — FastMCP stdio server, exposes extension commands to MCP clients
  lib/          — internal library: all implementation lives here
  plugin_api/   — stable external surface for hooks and plugins
  dev/          — dev-only utilities, not shipped in prod builds
```

## Internal vs External Split

**`lib/`** is internal — callers inside `meridian` are welcome, external plugins
are not. Anything in `lib/` may change without notice.

**`plugin_api/`** is a stable boundary. Built-in hooks, external plugins, and the
`git-autosync` built-in import only from here. If a hook needs something not in
the plugin API, extend the API rather than reaching into `lib/`.

**`cli/` and `server/`** are external entry points. `cli/` is the user-facing CLI.
`server/` is the MCP stdio server launched by `meridian serve`.

## lib/ Subdirectory Index

| Directory | Purpose |
|---|---|
| `lib/core/` | Domain types, spawn lifecycle state machine, business logic |
| `lib/state/` | All disk I/O: spawns, sessions, work items, atomic writes |
| `lib/platform/` | OS abstractions: locking, process scope, signal behavior |
| `lib/harness/` | Per-harness adapters (Claude, Codex, OpenCode) |
| `lib/launch/` | Spawn launch pipeline |
| `lib/ops/` | High-level spawn, session, and work operations |
| `lib/extensions/` | Extension command registry and dispatcher |
| `lib/hooks/` | Hook dispatch, config layering, built-in hooks |
| `lib/mermaid/` | Mermaid diagram validation and style checks |
| `lib/kg/` | Knowledge graph: markdown link checking |
| `lib/catalog/` | Agent and skill catalog, profile resolution |
| `lib/config/` | Config loading and layering |
| `lib/context/` | Context resolution for KB, strategy, work directories |
| `lib/streaming/` | Harness output streaming and event parsing |
| `lib/safety/` | Prompt injection detection |
| `lib/spawn/` | High-level spawn coordination (SpawnManager) |
| `lib/chat/` | Chat session management |
| `lib/observability/` | Metrics, structured logging |
| `lib/bootstrap/` | Startup preparation stages |
| `lib/markdown/` | Markdown parsing utilities |

## Entry Points

- `__main__.py` — `python -m meridian` entry
- `cli/entrypoint.py` — `main()` for the installed CLI binary
- `server/main.py` — `run_server()` for `meridian serve`

## Related

- `lib/AGENTS.md` is not present — navigate directly to subpackage AGENTS.md files
- CLAUDE.md (repo root) — architecture philosophy, design principles
