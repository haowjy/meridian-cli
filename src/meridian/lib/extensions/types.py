"""Core extension system types and handler protocol contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.lib.extensions.context import (
    ExtensionCommandServices,
    ExtensionInvocationContext,
)


@runtime_checkable
class ExtensionHandler(Protocol):
    """3-arg contract all extension handlers must implement."""

    async def __call__(
        self,
        args: dict[str, Any],
        context: ExtensionInvocationContext,
        services: ExtensionCommandServices,
    ) -> ExtensionResult: ...


class ExtensionSurface(StrEnum):
    """Surfaces where an extension command can be exposed."""

    HTTP = "http"
    CLI = "cli"
    MCP = "mcp"


def _coerce_to_json_result(result: Any) -> ExtensionJSONResult:
    """Convert op-style result to ExtensionJSONResult."""
    if hasattr(result, "to_wire"):
        return ExtensionJSONResult(payload=result.to_wire())
    if isinstance(result, BaseModel):
        return ExtensionJSONResult(payload=result.model_dump())
    return ExtensionJSONResult(payload={"result": result})


class ExtensionCommandSpec(BaseModel):
    """Specification for an extension command."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    extension_id: str = Field(
        description="Extension namespace, e.g. 'meridian.sessions'",
    )
    command_id: str = Field(
        description="Command name within extension, e.g. 'archiveSpawn'",
    )
    summary: str = Field(description="One-line description for CLI/MCP help")
    args_schema: type[BaseModel] = Field(
        description="Pydantic model for input validation",
    )
    result_schema: type[BaseModel] = Field(
        description="Pydantic model for output. "
        "Intentionally not validated at runtime — used as documentation metadata "
        "for manifest hashing (registry) and HTTP discovery (extension_routes).",
    )
    handler: ExtensionHandler
    surfaces: frozenset[ExtensionSurface] = Field(
        default=frozenset(
            {
                ExtensionSurface.HTTP,
                ExtensionSurface.CLI,
                ExtensionSurface.MCP,
            }
        ),
    )
    first_party: bool = Field(default=False)
    requires_app_server: bool = Field(
        default=True,
        description="If True, command only runs when app server is available",
    )
    required_capabilities: frozenset[str] = Field(default=frozenset())
    cli_group: str | None = Field(
        default=None,
        description="Cyclopts command group, e.g. 'work', 'spawn'. None = not CLI-routed.",
    )
    cli_name: str | None = Field(
        default=None,
        description="Cyclopts command name within group, e.g. 'start', 'list'.",
    )
    sync_handler: Callable[[dict[str, Any]], Any] | None = Field(
        default=None,
        description="Sync version of the handler. Takes dict args, returns raw output.",
    )

    @classmethod
    def from_op(
        cls,
        *,
        handler: Callable[[Any], Awaitable[Any]],
        sync_handler: Callable[[Any], Any] | None,
        input_type: type[BaseModel],
        output_type: type[BaseModel],
        extension_id: str,
        command_id: str,
        summary: str,
        cli_group: str | None = None,
        cli_name: str | None = None,
        surfaces: frozenset[ExtensionSurface] | None = None,
        requires_app_server: bool = False,
        first_party: bool = True,
    ) -> ExtensionCommandSpec:
        """Wrap an op-style (InputModel → OutputModel) handler pair as an ExtensionCommandSpec.

        Op-style handlers take a single Pydantic model and return a result.
        This factory wraps them into the 3-arg extension handler protocol.
        """

        async def wrapped_handler(
            args: dict[str, Any],
            context: ExtensionInvocationContext,
            services: ExtensionCommandServices,
        ) -> ExtensionResult:
            _ = (context, services)  # op-style handlers manage their own state access
            input_obj = input_type(**args)
            result = await handler(input_obj)
            return _coerce_to_json_result(result)

        wrapped_sync: Callable[[dict[str, Any]], Any] | None = None
        if sync_handler is not None:

            def _wrapped_sync(args: dict[str, Any]) -> Any:
                return sync_handler(input_type(**args))

            wrapped_sync = _wrapped_sync

        resolved_surfaces = surfaces
        if resolved_surfaces is None:
            resolved_surfaces = frozenset(
                {
                    ExtensionSurface.CLI,
                    ExtensionSurface.MCP,
                    ExtensionSurface.HTTP,
                }
            )

        return cls(
            extension_id=extension_id,
            command_id=command_id,
            summary=summary,
            args_schema=input_type,
            result_schema=output_type,
            handler=wrapped_handler,
            sync_handler=wrapped_sync,
            cli_group=cli_group,
            cli_name=cli_name,
            surfaces=resolved_surfaces,
            first_party=first_party,
            requires_app_server=requires_app_server,
        )

    @model_validator(mode="after")
    def _validate_cli_metadata(self) -> Self:
        """Ensure cli_group and cli_name are both set or both None."""
        has_group = self.cli_group is not None
        has_name = self.cli_name is not None
        if has_group != has_name:
            raise ValueError(
                f"Command '{self.fqid}': cli_group and cli_name must both be set or both be None"
            )
        return self

    @property
    def fqid(self) -> str:
        """Fully qualified command ID: extension_id.command_id."""

        return f"{self.extension_id}.{self.command_id}"


class ExtensionJSONResult(BaseModel):
    """Successful command result with JSON-serializable payload."""

    model_config = ConfigDict(frozen=True)

    payload: dict[str, Any]


class ExtensionErrorResult(BaseModel):
    """Command error with machine-readable code and human message."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    details: dict[str, Any] | None = None


type ExtensionResult = ExtensionJSONResult | ExtensionErrorResult


__all__ = [
    "ExtensionCommandServices",
    "ExtensionCommandSpec",
    "ExtensionErrorResult",
    "ExtensionHandler",
    "ExtensionInvocationContext",
    "ExtensionJSONResult",
    "ExtensionResult",
    "ExtensionSurface",
]
