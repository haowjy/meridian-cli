# lib/extensions — Unified Command System

CLI and MCP read from one registry. `ExtensionSurface.HTTP` remains declared in
specs, but no HTTP dispatcher or app server exists, so it is a dormant surface.
Surfaces are access policies on one `ExtensionCommandSpec`, not separate implementations.

## Mental Model

Every invokable operation is a spec. The spec declares: which surfaces can call it, what args it accepts (Pydantic model), what capabilities it requires, whether it needs a running app server, and an async handler. The dispatcher runs a validation pipeline before calling the handler — no handler ever receives invalid args or runs on an unauthorized surface.

```
Caller (CLI | MCP; HTTP is declared but dormant)
  └── ExtensionCommandDispatcher.dispatch(fqid, args, context, services)
        1. Registry lookup → not_found
        2. Trust check → trust_violation (third-party blocked)
        3. Surface check → surface_not_allowed
        4. App server check → app_server_required
        5. Pydantic validation → args_invalid
        6. Capability check → capability_missing
        7. Handler execution → handler_error
        8. Observability write (always, in finally)
```

Errors at any stage return `ExtensionErrorResult` — the dispatcher never raises.

## Key Rules

- **Add a command = write one spec.** Define a Pydantic args model, write an async handler `(args: dict, context, services) → ExtensionResult`, construct `ExtensionCommandSpec`, register it. Registration records every listed surface; active adapters expose only CLI and MCP.
- **Non-first-party commands cannot use CLI or MCP surfaces.** Enforced at `registry.register()` — raises `ValueError`. Not a soft policy.
- **`cli_group` and `cli_name` are all-or-nothing.** Both set or both None. A spec with only one will fail construction.
- **CLI/MCP callers get denied capabilities by default.** The dormant HTTP context
  default remains elevated in `ExtensionInvocationContextBuilder.build()`, but no
  active HTTP boundary constructs it.
- **Use `ExtensionCommandSpec.from_op()` for single-model handlers.** The factory wraps the op into the 3-arg signature. Don't implement `(args: dict, context, services)` manually for op-style handlers.

## Entry Points

- `types.py` — `ExtensionCommandSpec`, `ExtensionResult`, `ExtensionSurface`
- `registry.py` — `ExtensionCommandRegistry`, `get_first_party_registry()` (singleton)
- `dispatcher.py` — `ExtensionCommandDispatcher.dispatch()`
- `context.py` — `ExtensionInvocationContext`, `ExtensionCommandServices`, `ExtensionInvocationContextBuilder`
- `first_party.py` — where existing first-party specs are registered
- `commands/` — handler implementations grouped by domain

## Anti-Patterns

- Don't treat `ExtensionSurface.HTTP` membership as a runnable endpoint. The HTTP
  surface is declared for a possible app-server return, not currently dispatched.
- Don't catch and swallow exceptions in handlers — return `ExtensionErrorResult` instead. The dispatcher records the error code; exceptions become `handler_error`.
- Don't call `build_first_party_registry()` in application code — it creates a fresh instance. Use `get_first_party_registry()` for the singleton.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — dispatcher pipeline detail, capability model, surface rules, registry singleton, manifest hash
→ KB: `$MERIDIAN_CONTEXT_KB_DIR/concepts/extension-system.md` (see `meridian context kb`)
