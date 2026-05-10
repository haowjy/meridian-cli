# lib/extensions — Unified Command System

One `ExtensionCommandSpec` per command. All three surfaces (CLI, MCP, HTTP)
read from the same registry. Adding a command = writing one spec; it appears
everywhere automatically based on `surfaces`.

## Key Types

- `ExtensionCommandSpec` — single command definition (id, surfaces, handler, schemas) in `types.py`
- `ExtensionCommandRegistry` — indexed store; `register()` + `get(fqid)` in `registry.py`
- `ExtensionCommandDispatcher` — validation pipeline + handler invocation in `dispatcher.py`
- `ExtensionInvocationContext` + `ExtensionCapabilities` — per-call caller identity in `context.py`
- `ExtensionCommandServices` — runtime dependencies available to handlers in `context.py`

## Entry Points for Adding Commands

1. Define Pydantic args and result models
2. Write async handler `(args: dict, context, services) → ExtensionResult`
3. Create `ExtensionCommandSpec` or use `ExtensionCommandSpec.from_op()` for op-style handlers
4. Register in `ops/manifest.py` (most commands) or `first_party.py` (extension-specific)

## Submodule

- `commands/` — command handler implementations grouped by domain (`mermaid.py`, `sessions.py`, etc.)

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — dispatcher pipeline, surface rules, capability model
→ [KB: concepts/extension-system.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/concepts/extension-system.md)
