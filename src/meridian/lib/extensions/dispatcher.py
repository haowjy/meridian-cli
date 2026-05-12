"""Extension command dispatcher with validation gates and observability."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from meridian.lib.extensions.context import (
    ExtensionCommandServices,
    ExtensionInvocationContext,
)
from meridian.lib.extensions.observability import (
    ExtensionInvocationSummary,
    InvocationTimer,
    ObservabilityWriter,
    RedactionPipeline,
)
from meridian.lib.extensions.registry import ExtensionCommandRegistry
from meridian.lib.extensions.types import (
    ExtensionCommandSpec,
    ExtensionErrorResult,
    ExtensionJSONResult,
    ExtensionResult,
    ExtensionSurface,
)


class ExtensionCommandDispatcher:
    """Dispatch extension commands with validation and observability."""

    def __init__(
        self,
        registry: ExtensionCommandRegistry,
        observability_log: Path | None = None,
    ) -> None:
        self._registry = registry
        self._obs_writer = ObservabilityWriter(observability_log) if observability_log else None

    async def dispatch(
        self,
        fqid: str,
        args: dict[str, Any],
        context: ExtensionInvocationContext,
        services: ExtensionCommandServices,
    ) -> ExtensionResult:
        """Dispatch a command by fqid and always emit one summary when enabled."""

        timer = InvocationTimer()
        error_code: str | None = None
        result: ExtensionResult | None = None

        try:
            spec = self._registry.get(fqid)
            if spec is None:
                error_code = "not_found"
                result = ExtensionErrorResult(
                    code=error_code,
                    message=f"Extension command not found: {fqid}",
                )
                return result

            if not spec.first_party:
                error_code = "trust_violation"
                result = ExtensionErrorResult(
                    code=error_code,
                    message="Third-party commands not yet supported",
                )
                return result

            if not self._surface_allowed(spec, context.caller_surface):
                error_code = "surface_not_allowed"
                result = ExtensionErrorResult(
                    code=error_code,
                    message=f"Command {fqid} not available on {context.caller_surface.value}",
                )
                return result

            if spec.requires_app_server and context.project_uuid is None:
                error_code = "app_server_required"
                result = ExtensionErrorResult(
                    code=error_code,
                    message=f"Command {fqid} requires app server",
                )
                return result

            try:
                validated_args = spec.args_schema(**args)
            except ValidationError as error:
                error_code = "args_invalid"
                result = ExtensionErrorResult(
                    code=error_code,
                    message=str(error),
                    details={"validation_errors": error.errors()},
                )
                return result

            for capability in spec.required_capabilities:
                if not context.capabilities.has(capability):
                    error_code = "capability_missing"
                    result = ExtensionErrorResult(
                        code=error_code,
                        message=f"Command {fqid} requires capability: {capability}",
                    )
                    return result

            handler_result: Any = await spec.handler(
                validated_args.model_dump(),
                context,
                services,
            )

            if isinstance(handler_result, dict):
                result = ExtensionJSONResult(payload=cast("dict[str, Any]", handler_result))
            elif isinstance(handler_result, ExtensionJSONResult):
                result = handler_result
            elif isinstance(handler_result, ExtensionErrorResult):
                result = handler_result
                error_code = handler_result.code
            else:
                result = ExtensionJSONResult(payload={"result": handler_result})

            return result
        except Exception as error:
            error_code = "handler_error"
            result = ExtensionErrorResult(
                code=error_code,
                message=str(error),
                details={"traceback": traceback.format_exc()},
            )
            return result
        finally:
            if self._obs_writer:
                summary = ExtensionInvocationSummary(
                    fqid=fqid,
                    caller_surface=context.caller_surface.value,
                    request_id=context.request_id,
                    started_at=timer.started_at,
                    duration_ms=timer.duration_ms,
                    success=error_code is None,
                    error_code=error_code,
                    args_redacted=RedactionPipeline.redact(args),
                    result_redacted=self._redact_result(result),
                )
                self._obs_writer.write_summary(summary)

    def _surface_allowed(
        self,
        spec: ExtensionCommandSpec,
        surface: ExtensionSurface,
    ) -> bool:
        """Check if command can run on the invocation surface."""

        return surface in spec.surfaces

    def _redact_result(self, result: ExtensionResult | None) -> dict[str, Any]:
        """Redact result for observability output."""

        if isinstance(result, ExtensionJSONResult):
            return RedactionPipeline.redact(result.payload)
        if isinstance(result, ExtensionErrorResult):
            return {"code": result.code, "message": result.message}
        return {}


__all__ = ["ExtensionCommandDispatcher"]
