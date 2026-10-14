# Extension Command Authoring

Extension commands are the canonical way to expose one operation through the
CLI and MCP without duplicating routing logic.

## Architecture

```text
ExtensionCommandSpec
        │
        ▼
ExtensionCommandRegistry
        ├──▶ CLI: meridian ext run / ext list / ext commands
        └──▶ MCP: extension_invoke / extension_list_commands
```

`ExtensionCommandDispatcher` applies trust, surface, argument, capability, and
error handling before it calls a handler. Handlers receive the same
`(args, context, services)` signature from either adapter.

## Op-style specs

Use `ExtensionCommandSpec.from_op()` when an existing async operation accepts a
Pydantic input model and returns a result model:

```python
from pydantic import BaseModel, Field
from meridian.lib.extensions.types import ExtensionCommandSpec, ExtensionSurface


class CheckInput(BaseModel):
    path: str = Field(description="Path to check")


class CheckOutput(BaseModel):
    valid: bool


async def check(input: CheckInput) -> CheckOutput:
    return CheckOutput(valid=True)


SPEC = ExtensionCommandSpec.from_op(
    handler=check,
    sync_handler=None,
    input_type=CheckInput,
    output_type=CheckOutput,
    extension_id="meridian.example",
    command_id="check",
    summary="Check an example path",
    surfaces=frozenset({ExtensionSurface.CLI, ExtensionSurface.MCP}),
    first_party=True,
)
```

The factory wraps the typed operation in the extension handler protocol. Supply
`sync_handler` only when a direct CLI route needs a synchronous operation.

## Direct specs

Use the constructor when the handler needs invocation context or services:

```python
from typing import Any
from meridian.lib.extensions.context import ExtensionCommandServices, ExtensionInvocationContext
from meridian.lib.extensions.types import (
    ExtensionCommandSpec,
    ExtensionErrorResult,
    ExtensionJSONResult,
    ExtensionResult,
    ExtensionSurface,
)


async def handler(
    args: dict[str, Any],
    context: ExtensionInvocationContext,
    services: ExtensionCommandServices,
) -> ExtensionResult:
    if services.resolved_project_root() is None:
        return ExtensionErrorResult(code="service_unavailable", message="project root missing")
    return ExtensionJSONResult(payload={"request_id": context.request_id})


SPEC = ExtensionCommandSpec(
    extension_id="meridian.example",
    command_id="inspect",
    summary="Inspect example state",
    args_schema=InspectArgs,
    result_schema=InspectResult,
    handler=handler,
    surfaces=frozenset({ExtensionSurface.CLI, ExtensionSurface.MCP}),
    first_party=True,
)
```

Handlers may return `ExtensionJSONResult`, `ExtensionErrorResult`, or a `dict`
(which the dispatcher wraps as JSON). Other return values become
`{"result": value}`. Prefer a specific `ExtensionErrorResult` over raising;
unhandled exceptions become `handler_error`.

## Surfaces, trust, and capabilities

`ExtensionSurface` has two active values: `CLI` and `MCP`. Omit `surfaces` to
expose a spec on both, or supply a subset such as MCP-only:

```python
surfaces=frozenset({ExtensionSurface.MCP})
```

Only first-party commands may use either surface. Third-party commands are not
yet supported by the registry or dispatcher.

CLI and MCP invocations have no capabilities by default. A driving adapter must
explicitly grant any `required_capabilities` through
`ExtensionInvocationContextBuilder.with_capabilities()`.

## Registration

Register first-party specs in `src/meridian/lib/extensions/first_party.py`.
Registration rejects duplicate fqids, duplicate direct CLI routes, incomplete
`cli_group`/`cli_name` pairs, and non-first-party specs on active surfaces.

Set both `cli_group` and `cli_name` to graft an op-style spec onto an existing
CLI group. Leave both unset for commands reached only through `ext run` and MCP.
The matching `CommandDescriptor` controls agent-mode output for direct CLI
routes.

Use reverse-domain dotted extension IDs and stable command IDs, for example
`meridian.mermaid.check`. Keep summaries short because CLI and MCP discovery
both display them.
