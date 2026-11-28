from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtensionSurface(StrEnum):
    """Surfaces where an extension command can be exposed."""

    CLI = "cli"
    MCP = "mcp"


class ExtensionCommandSpec(BaseModel):
    """Specification for an extension command."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    extension_id: str = Field(
        description="Extension namespace, e.g. 'meridian.session'",
    )
    command_id: str = Field(
        description="Command name within extension, e.g. 'check'",
    )
    summary: str = Field(description="One-line description for CLI/MCP help")
    args_schema: type[BaseModel] = Field(
        description="Pydantic model for input validation",
    )
    result_schema: type[BaseModel] = Field(
        description="Pydantic model for output. "
        "Intentionally not validated at runtime — used as documentation metadata "
        "for manifest hashing and command discovery.",
    )
    handler: Callable[[Any], Any] = Field(
        description="The operation: takes a validated args_schema instance and returns its "
        "output, or an ExtensionErrorResult. Synchronous; the dispatcher runs it in a worker "
        "thread.",
    )
    surfaces: frozenset[ExtensionSurface] = Field(
        default=frozenset(
            {
                ExtensionSurface.CLI,
                ExtensionSurface.MCP,
            }
        ),
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

    @model_validator(mode="after")
    def _validate_cli_metadata(self) -> Self:
        has_group = self.cli_group is not None
        has_name = self.cli_name is not None
        if has_group != has_name:
            raise ValueError(
                f"Command '{self.fqid}': cli_group and cli_name must both be set or both be None"
            )
        return self

    @property
    def fqid(self) -> str:

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


type ExtensionResult = ExtensionJSONResult | ExtensionErrorResult


__all__ = [
    "ExtensionCommandSpec",
    "ExtensionErrorResult",
    "ExtensionJSONResult",
    "ExtensionResult",
    "ExtensionSurface",
]
