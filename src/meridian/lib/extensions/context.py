"""Invocation context and service contracts for extension command execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.service_context import (
    ApplicationContext,
    ApplicationServices,
    ExtensionEntryPoint,
)

if TYPE_CHECKING:
    from meridian.lib.config.settings import MeridianConfig
    from meridian.lib.core.lifecycle import SpawnLifecycleService
    from meridian.lib.extensions.types import ExtensionSurface


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
    def elevated(cls) -> ExtensionCapabilities:
        """Full capabilities for UI/HTTP contexts."""

        return cls(subprocess=True, kernel=True, hitl=True)

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
    project_uuid: str | None
    work_id: str | None
    work_path: Path | None
    spawn_id: str | None
    capabilities: ExtensionCapabilities
    request_id: str | None = None


class ExtensionInvocationContextBuilder:
    """Builder for ExtensionInvocationContext with surface-aware defaults."""

    def __init__(self, surface: ExtensionSurface) -> None:
        self._surface = surface
        self._project_uuid: str | None = None
        self._work_id: str | None = None
        self._work_path: Path | None = None
        self._spawn_id: str | None = None
        self._capabilities: ExtensionCapabilities | None = None
        self._request_id: str | None = None

    def with_project_uuid(self, uuid: str | None) -> ExtensionInvocationContextBuilder:
        self._project_uuid = uuid
        return self

    def with_work_id(self, work_id: str | None) -> ExtensionInvocationContextBuilder:
        self._work_id = work_id
        return self

    def with_work_path(self, path: Path | None) -> ExtensionInvocationContextBuilder:
        self._work_path = path
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

        from meridian.lib.extensions.types import ExtensionSurface

        if self._capabilities is None:
            if self._surface in (ExtensionSurface.CLI, ExtensionSurface.MCP):
                caps = ExtensionCapabilities.denied()
            else:
                caps = ExtensionCapabilities.elevated()
        else:
            caps = self._capabilities

        resolved_work_path: Path | None = None
        if (
            self._work_id is not None
            and self._work_path is not None
            and self._work_path.exists()
        ):
            resolved_work_path = self._work_path

        return ExtensionInvocationContext(
            caller_surface=self._surface,
            project_uuid=self._project_uuid,
            work_id=self._work_id,
            work_path=resolved_work_path,
            spawn_id=self._spawn_id,
            capabilities=caps,
            request_id=self._request_id,
        )


@dataclass
class ExtensionCommandServices:
    """Services available to extension command handlers."""

    runtime_root: Path | None = None
    meridian_dir: Path | None = None
    application: ExtensionEntryPoint = field(default_factory=ExtensionEntryPoint)


def build_extension_command_services(
    *,
    application: ExtensionEntryPoint | None = None,
    project_root: Path | None = None,
    runtime_root: Path | None = None,
    config: MeridianConfig | None = None,
    lifecycle: SpawnLifecycleService | None = None,
    meridian_dir: Path | None = None,
) -> ExtensionCommandServices:
    """Build extension services with the shared application seam attached."""

    resolved_application = application or ExtensionEntryPoint(
        context=ApplicationContext(
            project_root=project_root,
            runtime_root=runtime_root,
            config=config,
        ),
        services=ApplicationServices(lifecycle=lifecycle),
    )

    return ExtensionCommandServices(
        runtime_root=runtime_root or resolved_application.context.runtime_root,
        meridian_dir=meridian_dir,
        application=resolved_application,
    )


__all__ = [
    "ExtensionCapabilities",
    "ExtensionCapability",
    "ExtensionCommandServices",
    "ExtensionInvocationContext",
    "ExtensionInvocationContextBuilder",
    "build_extension_command_services",
]
