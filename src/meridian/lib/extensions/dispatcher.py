from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ValidationError

from meridian.lib.extensions.capabilities import ExtensionCapabilities
from meridian.lib.extensions.types import (
    ExtensionCommandSpec,
    ExtensionErrorResult,
    ExtensionJSONResult,
    ExtensionResult,
)


def _payload(output: Any) -> dict[str, Any]:
    if hasattr(output, "to_wire"):
        return output.to_wire()
    if isinstance(output, BaseModel):
        return output.model_dump()
    return {"result": output}


async def dispatch(
    spec: ExtensionCommandSpec,
    args: dict[str, Any],
    *,
    capabilities: ExtensionCapabilities | None = None,
) -> ExtensionResult:
    """Validate *args*, check capabilities, and run the command's handler in a worker thread.

    The caller has already found *spec* and checked that its surface exposes it.
    """

    try:
        try:
            validated_args = spec.args_schema(**args)
        except ValidationError as error:
            return ExtensionErrorResult(code="args_invalid", message=str(error))

        granted = capabilities or ExtensionCapabilities()
        for capability in spec.required_capabilities:
            if not granted.has(capability):
                return ExtensionErrorResult(
                    code="capability_missing",
                    message=f"Command {spec.fqid} requires capability: {capability}",
                )

        # The worker thread copies the context, so it runs in the caller's warning scope.
        output: Any = await asyncio.to_thread(spec.handler, validated_args)
        if isinstance(output, ExtensionErrorResult):
            return output
        return ExtensionJSONResult(payload=_payload(output))
    except Exception as error:
        return ExtensionErrorResult(code="handler_error", message=str(error))


__all__ = ["dispatch"]
