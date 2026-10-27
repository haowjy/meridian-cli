# lib/extensions — Unified Command System

Operation metadata has one owner: lightweight `ops/commands.py` declarations
(`OP_DECLARATIONS`). The registry holds them unresolved and imports an operation only
when a caller needs its full spec (`get`, `list_all`, `list_for_surface`); CLI
registration reads metadata alone. CLI and MCP share the same specs: surfaces are
access policies on one `ExtensionCommandSpec`, not separate implementations.

The dispatcher validates before any handler runs and never raises; every failure is an
`ExtensionErrorResult`. Pipeline, capability model, manifest hash:
`.context/CONTEXT.md`.

## Rules

- **Add a command = one spec.** Op-style handlers with a single Pydantic input go
  through `ExtensionCommandSpec.from_op()` (declare in `ops/commands.py`); custom
  specs go in `commands/` and register in `first_party.py`.
- **Non-first-party commands cannot expose CLI or MCP**; `registry.register()` raises
  `ValueError`.
- **`cli_group` and `cli_name` are both set or both None**; spec construction fails otherwise.
- **CLI/MCP callers start with denied capabilities**; grant them explicitly through
  `ExtensionInvocationContextBuilder`.
- **Handlers return `ExtensionErrorResult` rather than swallowing or raising**; a raised
  exception becomes `handler_error` and loses context.
- **Use `get_first_party_registry()`** (singleton); `build_first_party_registry()` makes a
  fresh instance and is for tests.

KB: `concepts/extension-system.md`.
