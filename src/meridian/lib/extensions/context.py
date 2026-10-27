"""Invocation context and service contracts for extension command execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.service_context import ExtensionEntryPoint

if TYPE_CHECKING:
    from meridian.lib.extensions.types import ExtensionSurface
    from meridian.lib.ops.runtime import RuntimeAuthoritySnapshot


class ExtensionCapability(StrEnum):
    """Capabilities that can be granted to extension command execution."""

    SUBPROCESS = "subprocess"
    KERNEL = "kernel"
    HITL = "hitl"


@dataclass(frozen=True)
class ExtensionCapabilities:
    """Granted capabilities for a command invocation."""

    subprocess: bool = False
    kernel: bool = False
    hitl: bool = False

    @classmethod
    def denied(cls) -> ExtensionCapabilities:
        """No capabilities for CLI/MCP contexts."""

        return cls()

    def has(self, capability: str) -> bool:
        """Check if a capability is granted."""

        return bool(getattr(self, capability, False))


@dataclass(frozen=True)
class ExtensionInvocationContext:
    """Context available to extension command handlers."""

    caller_surface: ExtensionSurface
    authority: RuntimeAuthoritySnapshot | None
    work_id: str | None
    work_path: Path | None
    spawn_id: str | None
    capabilities: ExtensionCapabilities
    request_id: str | None = None


class ExtensionInvocationContextBuilder:
    """Builder for ExtensionInvocationContext with surface-aware defaults."""

    def __init__(self, surface: ExtensionSurface) -> None:
        self._surface = surface
        self._authority: RuntimeAuthoritySnapshot | None = None
        self._work_id: str | None = None
        self._work_path: Path | None = None
        self._spawn_id: str | None = None
        self._capabilities: ExtensionCapabilities | None = None
        self._request_id: str | None = None

    def with_authority(
        self,
        authority: RuntimeAuthoritySnapshot | None,
    ) -> ExtensionInvocationContextBuilder:
        self._authority = authority
        return self

    def with_work_id(self, work_id: str | None) -> ExtensionInvocationContextBuilder:
        self._work_id = work_id
        return self

    def with_spawn_id(self, spawn_id: str | None) -> ExtensionInvocationContextBuilder:
        self._spawn_id = spawn_id
        return self

    def with_capabilities(
        self,
        caps: ExtensionCapabilities,
    ) -> ExtensionInvocationContextBuilder:
        self._capabilities = caps
        return self

    def with_request_id(self, request_id: str) -> ExtensionInvocationContextBuilder:
        self._request_id = request_id
        return self

    def build(self) -> ExtensionInvocationContext:
        """Build context with surface-appropriate capability defaults."""

        caps = self._capabilities or ExtensionCapabilities.denied()

        resolved_work_path: Path | None = None
        if self._work_id is not None and self._work_path is not None and self._work_path.exists():
            resolved_work_path = self._work_path

        return ExtensionInvocationContext(
            caller_surface=self._surface,
            authority=self._authority,
            work_id=self._work_id,
            work_path=resolved_work_path,
            spawn_id=self._spawn_id,
            capabilities=caps,
            request_id=self._request_id,
        )


@dataclass
class ExtensionCommandServices:
    """Services available to extension command handlers."""

    application: ExtensionEntryPoint = field(default_factory=ExtensionEntryPoint)

    def resolved_project_root(self) -> Path | None:
        """Project root of the shared application context, if known."""

        return self.application.context.project_root


__all__ = [
    "ExtensionCapabilities",
    "ExtensionCapability",
    "ExtensionCommandServices",
    "ExtensionInvocationContext",
    "ExtensionInvocationContextBuilder",
]
