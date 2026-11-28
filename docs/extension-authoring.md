# Extension Command Authoring

Extension commands are the canonical way to expose one operation through the
CLI and MCP without duplicating routing logic.

## Architecture

```text
OpDeclaration (src/meridian/lib/ops/commands.py)
        │
        ▼
ExtensionCommandRegistry ──▶ direct CLI routes (cli_group / cli_name)
        ├──▶ CLI: meridian ext run / ext list / ext commands
        └──▶ MCP: extension_invoke / extension_list_commands
```

Both adapters call `dispatch()` (`lib/extensions/dispatcher.py`), which validates
the arguments against the input model, checks capabilities, and runs the
handler in a worker thread. A direct CLI route calls the same function itself.

## Declaring an operation

An operation is one synchronous function that takes its Pydantic input model and
returns its output model:

```python
from pydantic import BaseModel, Field


class CheckInput(BaseModel):
    path: str = Field(description="Path to check")


class CheckOutput(BaseModel):
    valid: bool


def check_sync(payload: CheckInput) -> CheckOutput:
    return CheckOutput(valid=True)
```

Register it with an `OpDeclaration` in `src/meridian/lib/ops/commands.py`. The
declaration names the function and models as `module:attribute` strings, so CLI
startup does not import the implementation:

```python
OpDeclaration(
    extension_id="meridian.example",
    command_id="check",
    summary="Check an example path",
    handler="meridian.lib.ops.example:check_sync",
    input_type="meridian.lib.ops.example:CheckInput",
    output_type="meridian.lib.ops.example:CheckOutput",
    surfaces=frozenset({ExtensionSurface.CLI, ExtensionSurface.MCP}),
)
```

The dispatcher returns the output's `to_wire()` when it has one, otherwise its
`model_dump()`. A handler may return an `ExtensionErrorResult` to report a
specific error code; prefer that over raising, since an unhandled exception
becomes `handler_error`. Async work stays inside the handler: run it with
`asyncio.run`, which is safe because the handler is never on the server's event
loop.

## Surfaces and capabilities

`ExtensionSurface` has two values: `CLI` and `MCP`. Omit `surfaces` to expose an
operation on both, or supply a subset such as MCP-only:

```python
surfaces=frozenset({ExtensionSurface.MCP})
```

CLI and MCP invocations grant no capabilities. A command that sets
`required_capabilities` fails with `capability_missing` unless its caller passes
the capability to `dispatch()`.

## Registration

Registration rejects duplicate fqids, duplicate direct CLI routes, and
incomplete `cli_group`/`cli_name` pairs.

Set both `cli_group` and `cli_name` to graft an operation onto an existing CLI
group. Leave both unset for commands reached only through `ext run` and MCP.
The matching `CommandDescriptor` controls agent-mode output for direct CLI
routes.

Use reverse-domain dotted extension IDs and stable command IDs, for example
`meridian.mermaid.check`. Keep summaries short because CLI and MCP discovery
both display them.
