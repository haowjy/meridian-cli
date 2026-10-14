# lib/extensions — Contracts and Pipeline

## Dispatcher Validation Pipeline

Every call to `ExtensionCommandDispatcher.dispatch(fqid, args, context, services)` runs in order:

1. **Registry lookup** — `not_found` if fqid unknown
2. **Trust check** — `trust_violation` if `spec.first_party` is False (third-party commands not yet supported)
3. **Surface check** — `surface_not_allowed` if `context.caller_surface` not in `spec.surfaces`
4. **Args validation** — `args_invalid` if Pydantic validation fails on `spec.args_schema`
5. **Capability check** — `capability_missing` if any `spec.required_capabilities` absent from context
6. **Handler execution** — `handler_error` if handler raises
7. **Observability** — writes `ExtensionInvocationSummary` to `extension-invocations.jsonl` in `finally` block (always, success or failure)

Errors at any stage return `ExtensionErrorResult(code=..., message=...)` — no exceptions propagate to callers.

## Handler Signature Contract

All handlers must implement `ExtensionHandler`:

```python
async def handler(
    args: dict[str, Any],        # validated dict from args_schema
    context: ExtensionInvocationContext,
    services: ExtensionCommandServices,
) -> ExtensionResult:            # ExtensionJSONResult | ExtensionErrorResult
```

Op-style handlers (single Pydantic input model) use `ExtensionCommandSpec.from_op()` which wraps them. Don't implement the 3-arg signature manually for op-style handlers — the factory handles wrapping.

## Surface Rules

`ExtensionSurface` declares the two active adapters: `CLI` and `MCP`.

**Non-first-party commands cannot expose `CLI` or `MCP`** — enforced at `registry.register()`. This is a hard constraint, not a default. Attempting to register a non-first-party command with CLI or MCP surfaces raises `ValueError`.

`cli_group` and `cli_name` must **both be set or both be None** — enforced by `@model_validator`. A command with `cli_group` alone or `cli_name` alone will fail spec construction.

## Capabilities

`ExtensionCapabilities` carries three flags: `subprocess`, `kernel`, `hitl`.

`ExtensionInvocationContextBuilder.build()` uses `ExtensionCapabilities.denied()`
unless the caller explicitly supplies capabilities with `with_capabilities()`.

## Registry Singleton

`get_first_party_registry()` returns a module-level singleton built on first access. Use `build_first_party_registry()` in tests to get a fresh instance.

`compute_manifest_hash(registry)` produces a SHA-256 over all command specs (fqid, schemas, surfaces). Use this to detect registry changes between processes.

## Rationale

One spec keeps surface implementations from diverging. The full design lives in:

→ `$MERIDIAN_CONTEXT_KB_DIR/concepts/extension-system.md` (see `meridian context kb`)
